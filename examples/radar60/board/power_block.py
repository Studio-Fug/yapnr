"""radar60 stage 3c R1: the power-stage subcells, each synthesized on its own board and handed
to the top placement as one rigid block, then fixed with its copper and pours.

A block is the one ``pnr.hier.blocks.extract_blocks`` finds around a floorplan
``power_stage.blocks[].anchor`` (``--block-anchor``, default the first): U2's hard groups
(inductors, input caps, snubbers, VANA, R_SH1 and its Kelvin TP), or U5's (D3, the in/out
caps). Nothing here picks parts by hand: ``contains`` only checks the extraction.

Steps (``python power_block.py STEP --work WORK --out DIR``; WORK is an ``integrate.py
prepare`` work directory):

``synth``   stage A: (outline, seed) placement trials of the block alone on its own board
            (``pnr.hier.blocks.aspect_sizes`` x seeds, ``pnr.hier.blocks.sub_board`` and the
            engine placer, as ``pnr.hier.synth`` does); stage B: each legal, distinct layout
            on a KiCad board of its own (the block's footprints from WORK/source.kicad_pcb,
            ``pnr.writeback``), routed by ``pnr.staged_signal`` (the production router, with
            the floorplan's outer pours: ``plane_partition`` with a ``region``, whole-land
            terminals, solid connection), refilled, DRC'd and IR-extracted
            (``pnr.ir_extract`` on the block's own rails), and its hot-loop links checked on
            the routed copper (``pnr.hier.power_quality``'s copper graph). Then ``rank``.
``rank``    the evaluated layouts, ranked mechanically: open hot-loop links, then unconnected
            items (the guard ``pnr.hier.synth_native.rank_key`` also puts first), then in-block
            IR rails over budget and their excess, then area. Writes DIR/ranking.json and
            DIR/library.json, a frozen tier map ``{block: [layout, ...]}``
            (``pnr.hier.top.load_library``'s own format, as ``pnr.mc.halving --library``
            reads it) holding the complete layouts in rank order, each with its routed block
            board (``instances[].dir``).
``eval``    stage B for one candidate directory (run by ``synth`` in a subprocess).
``merge``   several blocks' ``library.json`` into one (``--libs A B``, to ``--out``).
``rescore`` the hot-loop links recounted on every evaluated layout's routed board, then rank.
``ki-strip`` (KiCad's Python) the block's footprints alone from a board, no copper.

The library's boards keep only the layout's own copper and its outer pours
(``block.kicad_pcb``: tracks, vias and the F.Cu pour zones; the inner planes are the top
board's), which ``integrate.py block`` draws into the placed board as the fixed block.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import fnmatch
import hashlib
import json
import math
import os
import pickle
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

# DRC types that say nothing about the block's layout (the synthetic Radar60_* footprint
# libraries are not registered beside a block board; integrate.py finish does that for the
# real board).
_DRC_NOT_LAYOUT = {"lib_footprint_issues", "lib_footprint_mismatch"}


def _engine_path(engine):
    for p in (str(Path(engine) / "hardware/pnr"), str(engine), str(HERE)):
        if p not in sys.path:
            sys.path.insert(0, p)


if os.environ.get("RADAR60_ENGINE"):  # spawned workers import this module as their main
    _engine_path(os.environ["RADAR60_ENGINE"])


def load_yaml(path):
    import yaml

    return yaml.safe_load(Path(path).read_text())


def floorplan_doc():
    return load_yaml(HERE / "floorplan.yaml")


def role_patterns(floorplan, role):
    pats = floorplan["parts"][role]
    return pats if isinstance(pats, list) else [pats]


def role_addresses(floorplan, role, addresses):
    pats = role_patterns(floorplan, role)
    return sorted(a for a in addresses if any(fnmatch.fnmatchcase(a, p) for p in pats))


# ------------------------------------------------------------------ the block


def stage_spec(floorplan, anchor=None):
    """The floorplan ``power_stage.blocks`` entry anchored on role ``anchor`` (default: the
    first)."""
    blocks = floorplan["power_stage"]["blocks"]
    if anchor is None:
        return blocks[0]
    for b in blocks:
        if b["anchor"] == anchor:
            return b
    raise SystemExit("no power_stage block anchored on %r" % anchor)


def find_block(graph, compiled, floorplan, spec):
    """The extracted block holding ``spec['anchor']``; every ``contains`` role inside it."""
    from pnr.hier.blocks import extract_blocks

    addr = {c.ref: c.address for c in graph.components}
    by_addr = {c.address: c.ref for c in graph.components if c.address}
    anchor = by_addr[role_patterns(floorplan, spec["anchor"])[0]]
    blocks = [b for b in extract_blocks(graph, compiled) if anchor in b.refs]
    if not blocks:
        raise SystemExit("no extracted block holds the power-stage anchor %s" % anchor)
    block = blocks[0]
    missing = {}
    for role in spec["contains"]:
        want = role_addresses(floorplan, role, by_addr)
        out = [a for a in want if by_addr[a] not in block.refs]
        if out:
            missing[role] = out
    if missing:
        raise SystemExit("power-stage block %s lacks %s" % (block.name, missing))
    return block, anchor, {r: addr[r] for r in block.refs}


def block_doc(top_doc, floorplan, addresses, width, height, stage):
    """The block board's constraint document: the top document's net classes, fab and board
    switches, its groups, sides and orientations restricted to the block, no board-level
    relations (fixed poses, regions, rows, keepouts of other parts, the fixed RF block, U1's
    fanout, pairs), the floorplan's outer pours and the top rails that lie inside the block."""
    addresses = set(addresses)

    def hit(sel):
        return (
            isinstance(sel, str)
            and sel.startswith("@")
            and any(fnmatch.fnmatchcase(a, sel[1:]) for a in addresses)
        )

    def glob_literal(a):
        return a.replace("[", "[[]")

    def inside(a):
        return a in addresses

    out = copy.deepcopy(top_doc)
    out.setdefault("board", {})["outline"] = {"w": float(width), "h": float(height)}
    for key in (
        "row",
        "line_group",
        "region",
        "align",
        "fixed_block",
        "layout_array",
        "fanout",
        "diff_pair",
        "length_match",
        "rf_macro",
        "noise_keepout",
        "height_limit",
        "legalize",
    ):
        out.pop(key, None)
    out["fixed"] = {}
    for key in ("keepout", "copper_keepout"):
        if key in out:
            out[key] = [k for k in out[key] if hit(k.get("ref"))]
            if not out[key]:
                out.pop(key)
    if "side" in out:
        out["side"] = {k: [s for s in v if hit(s)] for k, v in out["side"].items()}
        out["side"] = {k: v for k, v in out["side"].items() if v}
    if "orientation" in out:
        out["orientation"] = {k: v for k, v in out["orientation"].items() if hit(k)}
    groups = []
    for g in out.get("group") or []:
        if g.get("anchor") and not hit(g["anchor"]):
            continue
        members = [m for m in g.get("members", []) if hit(m)]
        if members:
            groups.append(dict(g, members=members))
    out["group"] = groups
    # Rails: the top board's In3 partition is the top board's; the block keeps its plane nets'
    # classes (drops land on the inner layers as on the top board) and pours its outer layer.
    out.pop("plane_partition", None)
    pours = []
    for p in stage.get("pours") or []:
        entry = {k: v for k, v in p.items() if k != "region"}
        region = p["region"]
        refs = sorted(
            {
                "@" + glob_literal(a)
                for role in region["parts"]
                for a in role_addresses(floorplan, role, addresses)
            }
        )
        entry["region"] = {"refs": refs, "margin_mm": region.get("margin_mm", 0.5)}
        pours.append(entry)
    if pours:
        out["plane_partition"] = pours
    rails = []
    power = floorplan.get("power") or {}
    for net, rail in (power.get("rails") or {}).items():
        # only a rail with declared sinks, all inside (without, ir_extract takes every other
        # pad of the net, and in a block that may be only capacitors: no load at all)
        parts = list(rail.get("sources") or {}) + list(rail.get("sinks") or {})
        if not rail.get("sinks") or not all(inside(a) for a in parts):
            continue
        entry = {
            "net": net,
            "sources": {"@" + glob_literal(a): list(p) for a, p in rail["sources"].items()},
        }
        if rail.get("sinks"):
            entry["sinks"] = {"@" + glob_literal(a): list(p) for a, p in rail["sinks"].items()}
        entry.update(
            current_a=rail["current_a"],
            budget_mohm=rail["budget_mohm"],
            temperature_c=power.get("temperature_c", 25),
            two_point=False,
        )
        rails.append(entry)
    out["ir_drop"] = rails
    if not rails:
        out.pop("ir_drop")
    return out


def hot_links(floorplan, components, stage):
    """The datasheet hot-loop links of a ``power_stage.blocks`` entry's ``hot_loops``, from the
    netlist:
    ``[(net, [(ref, pad)] one side, [(ref, pad), ...] the other)]``. One link per part pad on
    a listed net that the loop's IC also carries (to any of the IC's pads on it), then one per
    IC pad on such a net (to any of the loop parts' pads on it): every land of the IC's hot
    nets must reach the loop, not only one of them (a floating hot-rod land is an open
    link)."""
    by_addr = {c.address: c for c in components if c.address}
    links = []
    ic_side = {}  # (net, IC pad) -> the loop parts' pads on that net, over every loop
    for loop in stage.get("hot_loops") or []:
        ic = by_addr[role_patterns(floorplan, loop["ic"])[0]]
        ic_pads = {}
        for p in ic.pads:
            if p.net:
                ic_pads.setdefault(p.net, []).append((ic.ref, p.name))
        for role in loop["parts"]:
            for a in role_addresses(floorplan, role, by_addr):
                c = by_addr[a]
                for p in c.pads:
                    if not p.net or p.net not in ic_pads:
                        continue
                    if not any(fnmatch.fnmatchcase(p.net, n) for n in loop["nets"]):
                        continue
                    links.append((p.net, [(c.ref, p.name)], ic_pads[p.net]))
                    for q in ic_pads[p.net]:
                        ic_side.setdefault((p.net, q), []).append((c.ref, p.name))
    for (net, q), parts in sorted(ic_side.items()):
        links.append((net, [q], sorted(set(parts))))
    return links


# ------------------------------------------------------------------ stage A


def _place_worker(args):
    pkl, size, seed, iters = args
    t = time.monotonic()
    w, h, u, a = size
    rec = dict(width=w, height=h, utilisation=u, aspect=a, seed=seed, area=w * h)
    try:
        from pnr.hier.blocks import sub_board
        from pnr.hier.synth import _port_debt, local_key
        from pnr.place.placer import place

        graph, compiled, rules, block = pickle.loads(Path(pkl).read_bytes())
        sg, sc, sr = sub_board(graph, compiled, rules, block, w, h)
        placed, report = place(sg, sc, seed=seed, iters=iters, orient=True, channel_rules=sr)
        rec["legal"] = bool(report.legal)
        rec["status"] = "legal" if report.legal else "illegal"
        if report.legal:
            rec["layout"] = {
                local_key(block, c.address): [c.pos[0], c.pos[1], c.rot, c.side]
                for c in placed.components
            }
            rec["placed"] = placed.to_json(indent=None)
            rec["port_debt_mm"] = _port_debt(placed, block.external_nets, w, h)
    except Exception as error:  # a failed trial is a record, never a crash
        import traceback

        rec.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2000:])
    rec["seconds"] = round(time.monotonic() - t, 2)
    return rec


def _layout_key(rec):
    """Layouts equal up to 0.01 mm and rotation are one candidate."""
    rows = sorted(
        (k, round(v[0], 2), round(v[1], 2), round(v[2]) % 360, v[3])
        for k, v in rec["layout"].items()
    )
    return hashlib.sha256(json.dumps([rec["width"], rec["height"], rows]).encode()).hexdigest()[:12]


def step_synth(a):
    _engine_path(a.engine)
    os.environ["PNR_FAB_PROFILE"] = "pcbway-adv-6l-rf"
    from integrate import _compile

    from pnr.hier.blocks import aspect_sizes

    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    floorplan = floorplan_doc()
    top_doc = load_yaml(work / "constraints-place.yaml")
    t = time.time()
    graph, compiled, rules = _compile(a.engine, work / "inputs" / "graph.json", top_doc)
    stage = stage_spec(floorplan, a.block_anchor)
    block, anchor, addresses = find_block(graph, compiled, floorplan, stage)
    print(
        "block %s: %d parts, anchor %s, %.0f s to compile"
        % (block.name, len(block.refs), anchor, time.time() - t),
        flush=True,
    )
    info = dict(
        block=block.to_dict(),
        anchor=anchor,
        addresses=addresses,
        stage_anchor=stage["anchor"],
        hot_links=[[n, s, d] for n, s, d in hot_links(floorplan, graph.components, stage)],
    )
    (out / "block.json").write_text(json.dumps(info, indent=1))
    pkl = out / "top.pkl"
    pkl.write_bytes(pickle.dumps((graph, compiled, rules, block)))
    sizes = aspect_sizes(
        graph,
        block,
        utilisations=tuple(a.utilisations or stage.get("utilisations") or (0.35, 0.45, 0.55)),
        aspects=tuple(a.aspects or stage.get("aspects") or (1.0, 1.5, 1 / 1.5)),
    )
    jobs = [(str(pkl), s, seed, a.iters) for s in sizes for seed in range(a.seeds)]
    print("stage A: %d placement trials" % len(jobs), flush=True)
    recs = []
    env_engine = os.environ.setdefault("RADAR60_ENGINE", str(a.engine))
    assert env_engine
    with cf.ProcessPoolExecutor(a.procs) as pool:
        for rec in pool.map(_place_worker, jobs):
            recs.append(rec)
            print(
                "  %(width).2fx%(height).2f u%(utilisation).2f seed %(seed)d: %(status)s"
                " (%(seconds).0f s)" % rec,
                flush=True,
            )
    with (out / "trials.jsonl").open("w") as fh:
        for rec in recs:
            fh.write(json.dumps({k: v for k, v in rec.items() if k != "placed"}) + "\n")
    legal, seen = [], set()
    for rec in recs:
        if rec.get("legal"):
            key = _layout_key(rec)
            if key not in seen:
                seen.add(key)
                rec["id"] = key
                legal.append(rec)
    # Stage B order: smallest port debt per area band first, so a capped run still spans sizes.
    legal.sort(key=lambda r: (r["port_debt_mm"], r["area"]))
    legal = legal[: a.evaluate]
    print("stage B: %d distinct legal layouts" % len(legal), flush=True)
    cands = []
    for rec in legal:
        d = out / "cand" / rec["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "trial.json").write_text(json.dumps({k: v for k, v in rec.items() if k != "placed"}))
        (d / "placed-sub.json").write_text(rec["placed"])
        cands.append(d)

    def run(d):
        cmd = [sys.executable, __file__, "eval", "--work", work, "--out", d, "--engine", a.engine]
        cmd += ["--kicad-python", a.kicad_python, "--kicad-cli", a.kicad_cli]
        cmd += ["--route-iters", a.route_iters]
        with (Path(d) / "eval.log").open("w") as log:
            return subprocess.run([str(c) for c in cmd], stdout=log, stderr=subprocess.STDOUT)

    with cf.ThreadPoolExecutor(a.eval_procs) as pool:
        for d, res in zip(cands, pool.map(run, cands)):
            ev = d / "eval.json"
            if ev.is_file():
                e = json.loads(ev.read_text())
                print(
                    "  %s: hot open %s, unconnected %s, IR fails %s, area %.1f"
                    % (
                        d.name,
                        e.get("hot_links_open"),
                        e.get("unconnected"),
                        e.get("ir_fail"),
                        e["area"],
                    ),
                    flush=True,
                )
            else:
                print("  %s: eval failed (exit %d)" % (d.name, res.returncode), flush=True)
    pkl.unlink()
    step_rank(a)


# ------------------------------------------------------------------ stage B


def step_ki_strip(a):
    """KiCad's Python: the block's footprints alone (``--keep`` refs), no tracks, vias, zones or
    groups, saved to ``--dest``; the board's other drawings stay (writeback restamps Edge.Cuts)."""
    import pcbnew

    keep = set(json.loads(Path(a.keep_refs).read_text()))
    board = pcbnew.LoadBoard(str(a.src))
    # Read every item before mutating: this SWIG build returns raw pointers from the board's
    # collections once one has changed (pnr.hier.subpcb does the same).
    footprints = [(fp.GetReference(), fp) for fp in board.GetFootprints()]
    groups, tracks, zones = list(board.Groups()), list(board.GetTracks()), list(board.Zones())
    for g in groups:  # ungroup first: a group's members are deleted below on their own
        g.RemoveAll()
        board.Remove(g)
    for item in tracks + zones:
        board.Delete(item)
    present = set()
    for ref, fp in footprints:
        if ref in keep:
            present.add(ref)
        else:
            board.Delete(fp)
    if keep - present:
        raise SystemExit("missing footprints: %s" % sorted(keep - present))
    board.BuildConnectivity()
    pcbnew.SaveBoard(str(a.dest), board)
    os._exit(0)  # KiCad's Python can crash at interpreter teardown


def step_ki_pours(a):
    """KiCad's Python: ``--src`` with only its copper and the zones on ``--layers`` (the block's
    outer pours; the inner planes are the top board's), filled, saved to ``--dest``."""
    import pcbnew

    layers = set(a.layers.split(","))
    board = pcbnew.LoadBoard(str(a.src))
    for z in list(board.Zones()):
        names = {board.GetLayerName(la) for la in z.GetLayerSet().CuStack()}
        if z.GetIsRuleArea() or not names <= layers:
            board.Remove(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(a.dest), board)
    os._exit(0)


def step_eval(a):
    """Stage B for one candidate directory (``trial.json``, ``placed-sub.json``)."""
    _engine_path(a.engine)
    os.environ["PNR_FAB_PROFILE"] = "pcbway-adv-6l-rf"
    from integrate import _compile, _env, _run

    from pnr.graph import BoardGraph, BoardOutline

    work, d = Path(a.work), Path(a.out)
    trial = json.loads((d / "trial.json").read_text())
    info = json.loads((Path(a.out).parents[1] / "block.json").read_text())
    floorplan = floorplan_doc()
    stage = stage_spec(floorplan, info.get("stage_anchor"))
    t0 = time.time()
    rec = dict(id=trial["id"], area=trial["area"], width=trial["width"], height=trial["height"])
    rec["port_debt_mm"] = trial["port_debt_mm"]
    try:
        g = BoardGraph.from_json((d / "placed-sub.json").read_text())
        # The block rectangle plus the fab edge clearance on every side (pnr.hier.native_block's
        # apron): routed copper stays inside the rectangle the top placement reserves.
        top_doc = load_yaml(work / "constraints-place.yaml")
        top_rules = json.loads((work / "inputs" / "rules.json").read_text())
        apron = float((top_rules.get("fab") or {}).get("edge_clearance_mm", 0.2))
        for c in g.components:
            c.pos = (c.pos[0] + apron, c.pos[1] + apron)
        g.outline = BoardOutline(g.outline.width + 2 * apron, g.outline.height + 2 * apron)
        rec["apron_mm"] = apron
        (d / "placed.json").write_text(g.to_json())
        doc = block_doc(
            top_doc, floorplan, info["addresses"].values(), g.outline.width, g.outline.height, stage
        )
        import yaml

        (d / "constraints.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
        _, compiled, rules = _compile(a.engine, d / "placed.json", doc)
        if compiled.warnings:
            (d / "compile-warnings.json").write_text(json.dumps(compiled.warnings, indent=1))
        (d / "rules.json").write_text(json.dumps(rules, indent=1, sort_keys=True))
        (d / "keep.json").write_text(json.dumps(sorted(info["addresses"])))
        env = _env(a.engine)
        _run(
            [a.kicad_python, __file__, "ki-strip", "--src", work / "source.kicad_pcb"]
            + ["--dest", d / "src.kicad_pcb", "--keep-refs", d / "keep.json"],
            env=env,
            log=d / "strip.log",
        )
        board = d / "board.kicad_pcb"
        for ext in (".kicad_pro", ".kicad_dru"):
            shutil.copyfile(HERE / ("radar60" + ext), board.with_suffix(ext))
        _run(
            [a.kicad_python, "-m", "pnr.writeback", d / "src.kicad_pcb", d / "placed.json"]
            + ["--out", board, "--rules", d / "rules.json"],
            env=env,
            cwd=Path(a.engine) / "hardware/pnr",
            log=d / "writeback.log",
        )
        from pnr.staged_signal import run as route_signals

        route = d / "route"
        if route.exists():
            shutil.rmtree(route)
        try:
            route_signals(
                board,
                d / "rules.json",
                d / "constraints.yaml",
                route,
                a.kicad_python,
                a.kicad_cli,
                iterations=a.route_iters,
            )
        except subprocess.CalledProcessError as error:  # validate rejects; the board stays
            rec["route_status"] = "failed: %s" % error.cmd[2:4]
        cand = route / "candidate.kicad_pcb"
        if not cand.is_file():
            raise RuntimeError("no routed block board")
        res = json.loads((route / "result.json").read_text())
        rec["unrouted"] = res.get("unfinished_signal")
        drc = json.loads((route / "candidate.drc.json").read_text())
        rec["unconnected"] = len(drc.get("unconnected_items") or [])
        viol = [
            v
            for v in drc.get("violations") or []
            if v.get("type") not in _DRC_NOT_LAYOUT and v.get("severity") == "error"
        ]
        rec["violations"] = len(viol)
        rec["violation_types"] = sorted({v.get("type") for v in viol})
        ir = {}
        irp = route / "ir" / "ir.json"
        if irp.is_file():
            for net, r in json.loads(irp.read_text()).items():
                ir[net] = dict(
                    r_eff_mohm=r.get("r_eff_mohm"),
                    budget_mohm=r.get("budget_mohm"),
                    status=r.get("status"),
                    opens=len(r.get("opens") or []),
                )
        rec["ir"] = ir
        fails = [n for n, r in ir.items() if r["status"] != "pass"]
        rec["ir_fail"] = len(fails)
        rec["ir_excess"] = round(
            sum(
                (r["r_eff_mohm"] or 1e3) / r["budget_mohm"] - 1.0
                for n, r in ir.items()
                if n in fails and r["budget_mohm"]
            ),
            4,
        )
        rec.update(score_hot_links(info["hot_links"], cand, a.kicad_python))
        # The library's board: the layout's copper and its outer pours only.
        layers = sorted({p["layer"] for p in stage.get("pours") or []})
        _run(
            [a.kicad_python, __file__, "ki-pours", "--src", cand, "--dest", d / "block.kicad_pcb"]
            + ["--layers", ",".join(layers)],
            env=env,
            log=d / "pours.log",
        )
        rec["status"] = "ok"
    except Exception as error:
        import traceback

        rec.update(status="failed", error=repr(error), traceback=traceback.format_exc()[-2500:])
    rec["seconds"] = round(time.time() - t0, 1)
    (d / "eval.json").write_text(json.dumps(rec, indent=1))


def score_hot_links(links, board, kicad_python):
    """Open hot-loop links on a routed, filled board (``pnr.hier.power_quality``'s copper
    graph: tracks, vias, pads and the zones KiCad's fill puts them in)."""
    from pnr.hier.power_quality import Copper, read_board

    dump = read_board(board, ki_py=kicad_python)
    copper = {}
    opened = []
    for net, src, dst in links:
        if net not in copper:
            copper[net] = Copper(dump, net)
        if copper[net].path([tuple(x) for x in src], [tuple(x) for x in dst]) is None:
            opened.append([net, list(src[0])])
    return dict(hot_links=len(links), hot_links_open=len(opened), hot_open=opened)


def step_merge(a):
    """One frozen tier map from several blocks' ``library.json`` (``--libs``), to ``--out``."""
    merged = {}
    for path in a.libs:
        for name, tier in json.loads(Path(path).read_text()).items():
            if name in merged:
                raise SystemExit("block %s in two libraries" % name)
            merged[name] = tier
    Path(a.out).write_text(json.dumps(merged, indent=1, sort_keys=True))
    print("merged %s" % {k: len(v) for k, v in merged.items()})


def step_rescore(a):
    """Recount every evaluated layout's hot-loop links from its routed board (after the
    floorplan's ``hot_loops`` or :func:`hot_links` changed), then ``rank``."""
    _engine_path(a.engine)
    out = Path(a.out)
    info = json.loads((out / "block.json").read_text())
    from pnr.graph import BoardGraph

    graph = BoardGraph.from_json((Path(a.work) / "inputs" / "graph.json").read_text())
    fp = floorplan_doc()
    links = hot_links(fp, graph.components, stage_spec(fp, info.get("stage_anchor")))
    info["hot_links"] = [[n, s, d] for n, s, d in links]
    (out / "block.json").write_text(json.dumps(info, indent=1))
    for d in sorted((out / "cand").glob("*")):
        ev, board = d / "eval.json", d / "route" / "candidate.kicad_pcb"
        if not ev.is_file() or not board.is_file():
            continue
        e = json.loads(ev.read_text())
        if e.get("status") != "ok":
            continue
        e.update(score_hot_links(info["hot_links"], board, a.kicad_python))
        ev.write_text(json.dumps(e, indent=1))
    step_rank(a)


# ------------------------------------------------------------------ rank


def rank_key(r):
    """Open hot-loop links, then unconnected items and violations (the guard
    pnr.hier.synth_native.rank_key also puts first), then in-block IR rails over budget and
    their summed excess, then area, then port debt."""
    if r.get("status") != "ok":
        return (math.inf,)
    return (
        r["hot_links_open"],
        r["unconnected"],
        r["violations"],
        r["ir_fail"],
        r["ir_excess"],
        r["area"],
        r["port_debt_mm"],
        r["id"],
    )


def step_rank(a):
    out = Path(a.out)
    info = json.loads((out / "block.json").read_text())
    evals = []
    for d in sorted((out / "cand").glob("*")):
        ev = d / "eval.json"
        if not ev.is_file():
            continue
        e = json.loads(ev.read_text())
        trial = json.loads((d / "trial.json").read_text())
        e.update({k: trial[k] for k in ("layout", "seed", "utilisation", "aspect")})
        e["dir"] = str(d)
        evals.append(e)
    evals.sort(key=rank_key)
    rows = [
        {
            k: e.get(k)
            for k in (
                "id",
                "status",
                "hot_links_open",
                "hot_links",
                "unconnected",
                "violations",
                "violation_types",
                "ir_fail",
                "ir_excess",
                "ir",
                "area",
                "width",
                "height",
                "port_debt_mm",
                "seconds",
                "error",
            )
        }
        for e in evals
    ]
    name = info["block"]["name"]
    # The tier: the best ``keep`` layouts in rank order (the placement draws among them with
    # weight 0.5**rank, pnr.hier.top.draw_layout, so a shape that does not fit at top level
    # still leaves the others).
    ok = [e for e in evals if e.get("status") == "ok"]
    tier = ok[: a.keep]
    library = {
        name: [
            dict(
                layout=e["layout"],
                width=e["width"] - 2 * e["apron_mm"],
                height=e["height"] - 2 * e["apron_mm"],
                seed=e["seed"],
                utilisation=e["utilisation"],
                aspect=e["aspect"],
                missing=e["unconnected"],
                port_debt_mm=e["port_debt_mm"],
                hot_links_open=e["hot_links_open"],
                ir=e["ir"],
                area=e["area"],
                id=e["id"],
                instances=[dict(instance=name, dir=e["dir"])],
            )
            for e in tier
        ]
    }
    (out / "ranking.json").write_text(json.dumps(dict(block=name, ranked=rows), indent=1))
    (out / "library.json").write_text(json.dumps(library, indent=1, sort_keys=True))
    print("rank: %d evaluated, %d ok, tier %d" % (len(evals), len(ok), len(tier)))
    for r in rows[:10]:
        print(
            "  %s hot %s/%s unconn %s viol %s IR fail %s (%s) area %.1f"
            % (
                r["id"],
                r["hot_links_open"],
                r["hot_links"],
                r["unconnected"],
                r["violations"],
                r["ir_fail"],
                r["ir_excess"],
                r["area"] or 0,
            )
        )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "step",
        choices=["synth", "eval", "rank", "rescore", "merge", "ki-strip", "ki-pours"],
        help="see the docstring",
    )
    ap.add_argument("--work", help="an integrate.py prepare work directory")
    ap.add_argument("--out", help="the synthesis directory (eval: one candidate directory)")
    ap.add_argument("--engine", default=str(REPO))
    ap.add_argument("--kicad-python", default=os.environ.get("PNR_KICAD_PYTHON", "python3"))
    ap.add_argument("--kicad-cli", default=os.environ.get("PNR_KICAD_CLI", "kicad-cli"))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument(
        "--utilisations", type=float, nargs="+", help="block fill (default: the block's own)"
    )
    ap.add_argument("--aspects", type=float, nargs="+", help="w/h (default: the block's own)")
    ap.add_argument("--block-anchor", help="the power_stage block's anchor role (default: first)")
    ap.add_argument("--libs", nargs="+", help="merge: the library.json files")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--evaluate", type=int, default=12, help="stage B layouts at most")
    ap.add_argument("--eval-procs", type=int, default=2)
    ap.add_argument("--route-iters", type=int, default=12)
    ap.add_argument("--keep", type=int, default=6, help="library tier size at most")
    ap.add_argument("--src")
    ap.add_argument("--dest")
    ap.add_argument("--keep-refs", help="ki-strip: a JSON list of the refs to keep")
    ap.add_argument("--layers")
    a = ap.parse_args(argv)
    if a.step == "ki-strip":
        return step_ki_strip(a)
    if a.step == "ki-pours":
        return step_ki_pours(a)
    steps = dict(synth=step_synth, eval=step_eval, rank=step_rank, rescore=step_rescore)
    steps["merge"] = step_merge
    steps[a.step](a)


if __name__ == "__main__":
    main()
