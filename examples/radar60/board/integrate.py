"""radar60 Rev A integration: the schematic's board, the RF macro and the floorplan into one
board, placed by yapnr's Monte Carlo placement search, stopped before routing.

Steps (each reads the previous one's files in WORK; nothing is placed by hand):

``source``   the board yapnr ingests (kicad_ops.py source): the floorplan board with every
             footprint of the atopile build except the RF macro placeholder RFM1, which is held
             out of the placement graph (the engine models a part as a box about its origin, and
             the macro is neither a box nor movable: yapnr gap E2); its area is the RF region
             keepout of constraints.yaml.
``prepare``  ingest (pnr.ingest), the placement constraints (constraints.yaml without the
             proposed rf_macro section, whose part is not in the graph), the routing rules under
             the pcbway-adv-6l-rf profile, and the set checks (every radio capacitor in exactly
             one decoupling set).
``place``    pnr.mc.halving stage 0 only (--stop-after place): N seeded global starts
             (stratified and Latin-hypercube, initial_pool), each legalized and scored by the
             engine's width-aware routability proxy. Run it through the shared heavy-run queue.
``select``   the winner, mechanically: legal candidates that pass the placement audit
             (audit.py), ranked by the engine's own stage-0 key (proxy score, then cheap score),
             then HPWL, then id. Every candidate's audit and rank go to WORK/selection.json.
``finish``   pnr.writeback of the winner (placement only, no routes), then kicad_ops.py finish
             (outline, macro merge, zone fill), the project and custom rules beside it, KiCad's
             DRC, the audit of the final board and its report.
``render``   kicad-cli pcb render: top and an angled view (labelled with --label-python, a
             Python with Pillow).

Interpreters: this script runs under a numeric Python with PyYAML (the engine's
pnr.mc.halving needs torch and numpy); ``--kicad-python`` (or $PNR_KICAD_PYTHON) is KiCad's
own Python with pcbnew; ``--engine`` is the yapnr checkout whose hardware/pnr runs (the region,
side and stackup work is on the integrated gap branch); ``--kicad-cli`` (or $PNR_KICAD_CLI).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROFILE = "pcbway-adv-6l-rf"
MACRO = HERE.parent / "rf" / "generated" / "rfm1-n" / "rfm1-n.kicad_pcb"
BOARD_NAME = "radar60-reva"


def _env(engine, extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(engine) / "hardware/pnr"), str(engine)])
    env["PNR_FAB_PROFILE"] = PROFILE
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        env.setdefault(k, "1")
    env.update(extra or {})
    return env


def _run(cmd, env=None, cwd=None, log=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    res = subprocess.run(
        [str(c) for c in cmd], env=env, cwd=cwd, capture_output=True, text=True, check=False
    )
    text = res.stdout + res.stderr
    noise = ("Debug: Adding duplicate image handler", "stdpbase.cpp", "memory leak of type")
    text = "\n".join(line for line in text.splitlines() if not any(n in line for n in noise))
    if log:
        Path(log).write_text(text)
    if res.returncode:
        print(text[-4000:], file=sys.stderr)
        raise SystemExit("failed (%d): %s" % (res.returncode, cmd[1] if len(cmd) > 1 else cmd))
    return text


# ---------------------------------------------------------------- source / prepare


def step_source(a):
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    out = _run(
        [
            a.kicad_python,
            HERE / "kicad_ops.py",
            "source",
            HERE / "radar60.kicad_pcb",
            a.ato_board,
            work / "source.kicad_pcb",
        ]
    )
    print(out.strip().splitlines()[-1])
    result = Path(a.ato_board).parent / "result.json"
    if result.is_file():
        rec = json.loads(result.read_text())
        (work / "atopile-input.json").write_text(
            json.dumps({"input_id": rec.get("input_id"), "atopile": rec.get("atopile")}, indent=1)
        )


def _glob_literal(path):
    return "".join("[%s]" % c if c in "[*?" else c for c in path)


def _compile(engine, graph_path, constraints_doc):
    sys.path[:0] = [str(Path(engine) / "hardware/pnr"), str(engine)]
    os.environ["PNR_FAB_PROFILE"] = PROFILE
    from pnr.constraints import compile_constraints, compile_routing_rules
    from pnr.fab_profile import apply_rules
    from pnr.graph import BoardGraph

    graph = BoardGraph.from_json(Path(graph_path).read_text())
    compiled = compile_constraints(
        constraints_doc,
        graph.refs,
        {c.address: c.ref for c in graph.components if c.address},
        {
            f"{c.address}:{p.name}": p.net
            for c in graph.components
            if c.address
            for p in c.pads
            if p.name
        },
    )
    rules = apply_rules(compile_routing_rules(compiled, [n.name for n in graph.nets]))
    return graph, compiled, rules


def decoupling_sets(floorplan, addresses):
    """Every radio capacitor in exactly one of the HF, bulk and crystal sets."""
    parts = floorplan["parts"]

    def members(role):
        pats = parts[role] if isinstance(parts[role], list) else [parts[role]]
        return {a for a in addresses if any(fnmatch.fnmatchcase(a, p) for p in pats)}

    sets = {r: members(r) for r in ("radio_decoupling_hf", "radio_decoupling_bulk", "crystal_caps")}
    caps = {a for a in addresses if fnmatch.fnmatchcase(a, "radio.c_*")}
    seen = {}
    for role, ms in sets.items():
        for m in ms:
            seen.setdefault(m, []).append(role)
    return {
        "caps": len(caps),
        "sets": {r: len(m) for r, m in sets.items()},
        "uncovered": sorted(caps - set(seen)),
        "in_several": sorted(m for m, r in seen.items() if len(r) > 1),
    }


def step_prepare(a):
    work = Path(a.work)
    inputs = work / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    _run(
        [
            a.kicad_python,
            "-m",
            "pnr.ingest",
            work / "source.kicad_pcb",
            "--name",
            "radar60",
            "--dump-json",
            inputs / "graph.json",
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
    )
    import audit

    doc = yaml.safe_load((HERE / "constraints.yaml").read_text())
    doc.pop("rf_macro", None)  # its part is held out of the graph (see ``source``)
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    # R4 as a placement region (the engine's noise_keepout is only proposed): every movable
    # part with a pad on a digital net stays out of the RF region and its 5 mm guard band.
    graph_doc = json.loads((inputs / "graph.json").read_text())
    fixed = {k for k in doc.get("fixed", {})}
    digital = sorted(
        c["address"]
        for c in graph_doc["components"]
        if c.get("address")
        and "@" + _glob_literal(c["address"]) not in fixed
        and c["ref"] not in fixed
        and any(audit.is_digital(p.get("net")) for p in c["pads"])
    )
    doc.setdefault("region", []).append(
        {
            "name": "r4_digital",
            "refs": ["@" + _glob_literal(x) for x in digital],
            "areas": [{"rect": r} for r in audit.digital_region(floorplan)],
            "hard": True,
            "reason": "R4: no digital copper within 5 mm of the RF region (derived by integrate.py)",
        }
    )
    (work / "constraints-place.yaml").write_text(
        "# constraints.yaml without the proposed rf_macro section, plus the R4 region\n"
        "# (integrate.py prepare; generated)\n" + yaml.safe_dump(doc, sort_keys=False)
    )
    graph, compiled, rules = _compile(a.engine, inputs / "graph.json", doc)
    (inputs / "rules.json").write_text(json.dumps(rules, indent=1, sort_keys=True))
    sets = decoupling_sets(floorplan, [c.address for c in graph.components if c.address])
    report = {
        "components": len(graph.components),
        "nets": len(graph.nets),
        "warnings": compiled.warnings,
        "constraints": sorted({c.kind for c in compiled.constraints}),
        "decoupling_sets": sets,
        "r4_digital_parts": len(digital),
        "r4_region_rects": audit.digital_region(floorplan),
    }
    (work / "prepare.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    if sets["uncovered"] or sets["in_several"]:
        raise SystemExit("decoupling sets do not partition the radio capacitors")


# ---------------------------------------------------------------- place / select


def step_place(a):
    work = Path(a.work)
    out = work / "mc"
    t = time.time()
    _run(
        [
            a.python,
            "-m",
            "pnr.mc.halving",
            "--out",
            out,
            "--inputs",
            work / "inputs",
            "--constraints",
            work / "constraints-place.yaml",
            "--repo",
            a.engine,
            "--seed",
            a.seed,
            "--n0",
            a.n0,
            "--procs",
            a.procs,
            "--iters",
            a.iters,
            "--stop-after",
            "place",
        ],
        env=_env(a.engine),
        cwd=a.engine,
        log=work / "place.log",
    )
    print("placement search: %.0f s" % (time.time() - t))


def _records(work):
    path = Path(work) / "mc" / "dataset.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def step_select(a):
    import audit

    work = Path(a.work)
    recs = [r for r in _records(work) if r.get("stage") == "place"]
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    graph = json.loads((work / "inputs" / "graph.json").read_text())
    rows = []
    for r in recs:
        row = {
            "id": r["id"],
            "seed": r.get("seed"),
            "start_kind": r.get("start_kind"),
            "status": r["status"],
            "seconds": round(r.get("seconds", 0), 1),
        }
        if r["status"] == "legal":
            res = audit.placement_audit(graph, r["poses"], floorplan)
            row.update(
                proxy_score=r.get("proxy_score"),
                cheap_score=r.get("cheap_score"),
                hpwl_mm=round(r.get("hpwl_mm", math.inf), 1),
                audit_pass=res["pass"],
                audit_failures=res["failures"],
            )
        else:
            row["reason"] = (r.get("errors") or r.get("error") or r.get("flat_violations") or "")[
                :300
            ]
        rows.append(row)

    def key(row):
        return (
            0 if row.get("audit_pass") else 1,
            row.get("proxy_score") if row.get("proxy_score") is not None else math.inf,
            row.get("cheap_score") if row.get("cheap_score") is not None else math.inf,
            row.get("hpwl_mm", math.inf),
            row["id"],
        )

    legal = sorted((r for r in rows if r["status"] == "legal"), key=key)
    for i, r in enumerate(legal):
        r["rank"] = i + 1
    winner = legal[0]["id"] if legal and legal[0].get("audit_pass") else None
    sel = {
        "rule": "legal and audit pass; then the engine's stage-0 key (proxy score, cheap score);"
        " then HPWL; then id",
        "starts": len(rows),
        "legal": len(legal),
        "audit_pass": sum(1 for r in legal if r.get("audit_pass")),
        "winner": winner,
        "candidates": sorted(rows, key=lambda r: (r.get("rank", 10**6), r["id"])),
    }
    (work / "selection.json").write_text(json.dumps(sel, indent=1))
    if winner:
        write_placement(a, work, recs, winner, sel)
    print(
        json.dumps({k: v for k, v in sel.items() if k != "candidates"}, indent=1),
        "\n",
        json.dumps(sel["candidates"][:5], indent=1),
    )
    if not winner:
        raise SystemExit("no candidate passes the audit")


def write_placement(a, work, recs, winner, sel):
    """The winner's poses, by atopile address (board-only parts by reference), and where they
    came from: the record ``finish --placement`` rebuilds the board from."""
    graph = json.loads((work / "inputs" / "graph.json").read_text())
    key = {c["ref"]: (c.get("address") or "ref:" + c["ref"]) for c in graph["components"]}
    rec = next(r for r in recs if r["id"] == winner)
    ato = work / "atopile-input.json"
    out = {
        "schema": "radar60-placement/1",
        "note": "yapnr Monte Carlo placement (pnr.mc.halving stage 0), selected by integrate.py",
        "atopile_input_id": json.loads(ato.read_text()).get("input_id") if ato.is_file() else None,
        "engine": _engine_id(a.engine),
        "search": {
            "seed": a.seed,
            "starts": sel["starts"],
            "legal": sel["legal"],
            "audit_pass": sel["audit_pass"],
            "rule": sel["rule"],
        },
        "winner": {
            "id": winner,
            "start_seed": rec.get("seed"),
            "start_kind": rec.get("start_kind"),
            "proxy_score": rec.get("proxy_score"),
            "proxy": {
                k: rec["proxy"].get(k)
                for k in (
                    "unreachable_branches",
                    "overflow_units",
                    "saturation",
                    "wire_mm",
                    "via_demand",
                )
            },
            "cheap_score": rec.get("cheap_score"),
            "hpwl_mm": rec.get("hpwl_mm"),
        },
        "frame": "mm, origin at the board's lower-left corner, +y north, rot CCW; [x, y, rot, side]",
        "poses": {
            key[ref]: pose for ref, pose in sorted(rec["poses"].items(), key=lambda kv: key[kv[0]])
        },
    }
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "placement.json").write_text(json.dumps(out, indent=1) + "\n")


def _engine_id(engine):
    def git(*args):
        res = subprocess.run(["git", "-C", str(engine), *args], capture_output=True, text=True)
        return res.stdout.strip()

    return {
        "head": git("rev-parse", "--short=12", "HEAD"),
        "merging": git("rev-parse", "--short=12", "-q", "--verify", "MERGE_HEAD") or None,
        "uncommitted_files": len([x for x in git("status", "--porcelain").splitlines() if x]),
    }


def placed_from_record(a, work, record):
    """inputs/graph.json posed from a placement record (as pnr.mc.halving's placed.json)."""
    sys.path[:0] = [str(Path(a.engine) / "hardware/pnr"), str(a.engine)]
    from pnr.graph import BoardGraph
    from pnr.place.geometry import set_component_side

    graph = BoardGraph.from_json((work / "inputs" / "graph.json").read_text())
    poses = json.loads(Path(record).read_text())["poses"]
    for c in graph.components:
        x, y, rot, side = poses[c.address or "ref:" + c.ref]
        c.pos, c.rot = (x, y), rot
        set_component_side(c, side)
    out = work / "placed-from-record.json"
    out.write_text(graph.to_json())
    return out


# ---------------------------------------------------------------- finish


def step_finish(a):
    import audit

    work = Path(a.work)
    if a.placement:  # rebuild from a committed placement record
        placed_json = placed_from_record(a, work, a.placement)
        rec = json.loads(Path(a.placement).read_text())
        sel = dict(rec["search"], winner=rec["winner"]["id"])
    else:
        sel = json.loads((work / "selection.json").read_text())
        placed_json = work / "mc" / "cand" / sel["winner"] / "placed.json"
    placed = work / "placed.kicad_pcb"
    _run(
        [
            a.kicad_python,
            "-m",
            "pnr.writeback",
            work / "source.kicad_pcb",
            placed_json,
            "--out",
            placed,
            "--rules",
            work / "inputs" / "rules.json",
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
        log=work / "writeback.log",
    )
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    board = out_dir / (BOARD_NAME + ".kicad_pcb")
    # The project and the custom rules go beside the board first: the zone filler in
    # kicad_ops.py finish reads them (hole clearances of the custom rules).
    shutil.copyfile(HERE / "radar60.kicad_pro", out_dir / (BOARD_NAME + ".kicad_pro"))
    shutil.copyfile(HERE / "radar60.kicad_dru", out_dir / (BOARD_NAME + ".kicad_dru"))
    _run(
        [
            a.kicad_python,
            HERE / "kicad_ops.py",
            "finish",
            placed,
            HERE / "radar60.kicad_pcb",
            MACRO,
            board,
            work / "finish.json",
        ],
        log=work / "finish.log",
    )
    drc = work / "drc.json"
    subprocess.run(
        [
            a.kicad_cli,
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "--units",
            "mm",
            "-o",
            str(drc),
            str(board),
        ],
        check=False,
        capture_output=True,
    )
    poses = work / "final-poses.json"
    _run([a.kicad_python, HERE / "kicad_ops.py", "poses", board, poses])
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    macro_record = json.loads(MACRO.with_suffix(".json").read_text())
    report = audit.board_audit(
        json.loads(poses.read_text()),
        json.loads(drc.read_text()),
        floorplan,
        macro_record,
        json.loads((work / "finish.json").read_text()),
        sel,
    )
    (out_dir / (BOARD_NAME + ".kicad_prl")).unlink(missing_ok=True)  # KiCad's local view state
    report["summary"]["board_sha256"] = hashlib.sha256(board.read_bytes()).hexdigest()
    (out_dir / "placement-report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report["summary"], indent=1))


def step_render(a):
    out = Path(a.renders)
    out.mkdir(parents=True, exist_ok=True)
    board = Path(a.out) / (BOARD_NAME + ".kicad_pcb")
    views = {
        "top": ["--side", "top"],
        "angled": ["--side", "top", "--rotate", "-40,0,-25", "--perspective", "--zoom", "0.9"],
        "bottom": ["--side", "bottom"],
    }
    files = []
    for name, extra in views.items():
        png = out / ("radar60-reva-floorplan-%s.png" % name)
        _run(
            [a.kicad_cli, "pcb", "render", "-o", png, "--width", "2400", "--height", "1800"]
            + ["--quality", "high", "--background", "opaque"]
            + extra
            + [board]
        )
        files.append(png)
    if a.label_python:
        _run([a.label_python, HERE / "label_png.py", "Rev A floorplan, not routed"] + files)
    print("\n".join(str(f) for f in files))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "step", choices=["source", "prepare", "place", "select", "finish", "render", "all"]
    )
    ap.add_argument("--work", required=True, help="scratch directory for the run")
    ap.add_argument("--ato-board", help="the atopile build's board (yapnr atopile build -b rev-a)")
    ap.add_argument("--engine", default=str(REPO), help="yapnr checkout whose hardware/pnr runs")
    ap.add_argument("--python", default=sys.executable, help="numeric Python (torch, numpy)")
    ap.add_argument("--kicad-python", default=os.environ.get("PNR_KICAD_PYTHON", "python3"))
    ap.add_argument("--kicad-cli", default=os.environ.get("PNR_KICAD_CLI", "kicad-cli"))
    ap.add_argument("--out", default=str(HERE / "reva"), help="where the placed board goes")
    ap.add_argument("--renders", help="where the renders go (default: OUT/renders)")
    ap.add_argument("--label-python", help="a Python with Pillow, to label the renders")
    ap.add_argument(
        "--placement", help="finish: rebuild from this placement record (reva/placement.json)"
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n0", type=int, default=24, help="placement starts")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--iters", type=int, default=600)
    a = ap.parse_args(argv)
    a.renders = a.renders or str(Path(a.out) / "renders")
    sys.path.insert(0, str(HERE))
    steps = {
        "source": step_source,
        "prepare": step_prepare,
        "place": step_place,
        "select": step_select,
        "finish": step_finish,
        "render": step_render,
    }
    for name in list(steps) if a.step == "all" else [a.step]:
        steps[name](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
