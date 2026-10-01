"""Monte Carlo synthesis of local block layouts.

For each block template: sample (outline, seed) trials, place the block alone on
its own small board, route it with the production detail router, and keep the
mechanically-ranked best layouts. The ranking is lexicographic on measured
outcomes only: internal missing connections, then port-escape debt, then area,
vias and copper length. Every instance sharing a template is routed with the
same relative placement and must pass individually.

Output per template (``<out>/<template-id>/``): ``trials.jsonl`` and
``library.json`` holding the ranked layouts as relative poses.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import time
import traceback
from pathlib import Path

from pnr.hier.blocks import aspect_sizes, extract_blocks, sub_board


def _template_id(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()[:12]


def _port_debt(placed, block_ports, width, height):
    """Sum over external-net pads of distance to the nearest outline edge (mm).

    Pads that must leave the block should sit where copper can escape; the debt
    is a measured geometric quantity, not a tuned weight.
    """
    ports = set(block_ports)
    debt = 0.0
    for c in placed.components:
        th = math.radians(c.rot)
        cs, sn = math.cos(th), math.sin(th)
        for p in c.pads:
            if p.net not in ports:
                continue
            x = c.pos[0] + p.offset[0] * cs - p.offset[1] * sn
            y = c.pos[1] + p.offset[0] * sn + p.offset[1] * cs
            debt += min(x, width - x, y, height - y)
    return debt


def _trial(args):
    inputs, constraints_path, names, size, seed, iters, route_iters, reuse = args
    from pnr.mc.halving import _load

    t = time.monotonic()
    w, h, u, a = size
    rec = dict(blocks=names, width=w, height=h, utilisation=u, aspect=a, seed=seed, area=w * h)
    try:
        graph, constraints, rules = _load(Path(inputs), Path(constraints_path))
        blocks = {b.name: b for b in extract_blocks(graph, constraints)}
    except Exception as error:
        rec.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2500:])
        rec["seconds"] = time.monotonic() - t
        return rec
    return run_trial(
        graph, constraints, rules, blocks, names, size, seed, iters, route_iters, reuse, started=t
    )


def run_trial(
    graph,
    constraints,
    rules,
    blocks,
    names,
    size,
    seed,
    iters,
    route_iters,
    reuse=None,
    *,
    pitch=None,
    trace_id=None,
    started=None,
):
    """One synthesis trial in memory: place the first block of ``names`` alone on a
    ``size`` = (w, h, utilisation, aspect) board and route every instance of the
    template with that relative layout.

    ``blocks`` maps block names to :class:`pnr.hier.blocks.Block`; ``reuse`` is a
    placed sub-board JSON to route instead of placing; ``pitch`` is the detail grid
    pitch (None: the fab profile's). ``trace_id`` (tracing only) records the placement
    in a ``start`` scope of that name and each instance route in a ``route`` scope
    ``<trace_id>-<block>``. Returns the trial record :func:`_trial` writes.
    """
    import contextlib

    from pnr import trace as _trace
    from pnr.graph import BoardGraph
    from pnr.place.initial_pool import _route_metrics
    from pnr.place.placer import place
    from pnr.route.detail.router import route_board

    t = time.monotonic() if started is None else started
    w, h, u, a = size
    rec = dict(blocks=names, width=w, height=h, utilisation=u, aspect=a, seed=seed, area=w * h)

    def scope(name, scope_type, **meta):
        return _trace.scope(name, scope_type, **meta) if trace_id else contextlib.nullcontext()

    try:
        rep = blocks[names[0]]
        sg, sc, sr = sub_board(graph, constraints, rules, rep, w, h)
        outline = [_trace.um(w), _trace.um(h)]
        with scope(trace_id, "start", kind="block-trial", outline=outline, seed=seed):
            if reuse:
                placed = BoardGraph.from_json(reuse)
                legal = True
            else:
                placed, report = place(
                    sg, sc, seed=seed, iters=iters, orient=True, channel_rules=sr
                )
                legal = report.legal
            if trace_id and not legal:
                _trace.note(status="illegal")
        rec["legal"] = legal
        if not legal:
            rec["status"] = "illegal"
            return rec
        rec["placed"] = placed.to_json(indent=None)
        # Relative poses keyed by local path, so the layout transfers to twins.
        local = {
            local_key(rep, c.address): [c.pos[0], c.pos[1], c.rot, c.side]
            for c in placed.components
        }
        rec["layout"] = local
        results = []
        for name in names:
            blk = blocks[name]
            g2, c2, r2 = instance_board(graph, constraints, rules, blk, local, w, h)
            label = "%s-%s" % (trace_id, name)
            with scope(label, "route", start=trace_id, block=name, outline=outline):
                route = route_board(g2, c2, r2, pitch=pitch, max_iters=route_iters)
            m = _route_metrics(route)
            m["port_debt_mm"] = _port_debt(g2, blk.external_nets, w, h)
            m["instance"] = name
            m["tracks"] = [[n, la, list(p), list(q), wd] for n, la, p, q, wd in route.tracks]
            m["vias"] = [list(v) for v in route.vias]
            results.append(m)
        rec["instances"] = [
            {k: v for k, v in m.items() if k not in ("tracks", "vias")} for m in results
        ]
        rec["routes"] = {m["instance"]: dict(tracks=m["tracks"], vias=m["vias"]) for m in results}
        rec["missing"] = sum(m["missing_connections"] for m in results)
        rec["port_debt_mm"] = sum(m["port_debt_mm"] for m in results)
        rec["n_vias"] = sum(len(m["vias"]) for m in results)
        rec["copper_mm"] = sum(m["copper_length_mm"] for m in results)
        rec["status"] = "ok"
    except Exception as error:
        rec.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2500:])
    finally:
        rec["seconds"] = time.monotonic() - t
    return rec


def local_key(block, address):
    if block.prefix and address.startswith(block.prefix):
        return address[len(block.prefix) :].lstrip(".") or "@"
    return address or "@"


def instance_board(graph, constraints, rules, block, local, w, h):
    """Sub-board for ``block`` posed from a template layout keyed by local path."""
    from pnr.place.geometry import set_component_side

    g2, c2, r2 = sub_board(graph, constraints, rules, block, w, h)
    for c in g2.components:
        x, y, rot, side = local[local_key(block, c.address)]
        c.pos, c.rot = (x, y), rot
        set_component_side(c, side)  # mirrors pad offsets with the side, like the placer
    return g2, c2, r2


def rank_key(r):
    return (
        r.get("missing", math.inf),
        r.get("port_debt_mm", math.inf),
        r.get("area", math.inf),
        r.get("n_vias", math.inf),
        r.get("copper_mm", math.inf),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--constraints", type=Path, required=True)
    ap.add_argument(
        "--block", action="append", default=[], help="restrict to blocks with these names"
    )
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument("--route-iters", type=int, default=10)
    ap.add_argument("--procs", type=int, default=6)
    ap.add_argument("--keep", type=int, default=6)
    a = ap.parse_args(argv)
    from pnr.mc.halving import _load

    graph, constraints, rules = _load(a.inputs, a.constraints)
    blocks = extract_blocks(graph, constraints)
    templates = {}
    for b in blocks:
        templates.setdefault(b.template, []).append(b)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "blocks.json").write_text(json.dumps([b.to_dict() for b in blocks], indent=2))
    jobs = []
    for template, members in templates.items():
        names = [b.name for b in members]
        if a.block and not set(names) & set(a.block):
            continue
        tid = _template_id(template)
        d = a.out / tid
        d.mkdir(exist_ok=True)
        (d / "template.json").write_text(json.dumps(dict(template_id=tid, blocks=names), indent=2))
        for size in aspect_sizes(graph, members[0]):
            for seed in range(a.seeds):
                jobs.append(
                    (
                        tid,
                        (
                            str(a.inputs),
                            str(a.constraints),
                            names,
                            size,
                            seed,
                            a.iters,
                            a.route_iters,
                            None,
                        ),
                    )
                )
    print("trials", len(jobs), flush=True)
    by_t = {}
    with cf.ProcessPoolExecutor(a.procs) as pool:
        futs = {pool.submit(_trial, args): tid for tid, args in jobs}
        for f in cf.as_completed(futs):
            tid = futs[f]
            rec = f.result()
            rec["template_id"] = tid
            with (a.out / tid / "trials.jsonl").open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            by_t.setdefault(tid, []).append(rec)
            print(
                tid,
                rec["blocks"][0],
                rec.get("status"),
                rec.get("missing"),
                "%.1fx%.1f" % (rec["width"], rec["height"]),
                "seed",
                rec["seed"],
                "%.0fs" % rec["seconds"],
                flush=True,
            )
            ok = sorted((r for r in by_t[tid] if r.get("status") == "ok"), key=rank_key)
            (a.out / tid / "library.json").write_text(
                json.dumps(
                    dict(
                        blocks=rec["blocks"],
                        ranked=[{k: r[k] for k in r if k not in ("placed",)} for r in ok[: a.keep]],
                        placed=[r["placed"] for r in ok[: a.keep]],
                        counts=dict(
                            total=len(by_t[tid]),
                            ok=len(ok),
                            complete=sum(1 for r in ok if r["missing"] == 0),
                        ),
                    ),
                    indent=1,
                )
            )


if __name__ == "__main__":
    main()
