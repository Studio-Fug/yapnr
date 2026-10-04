"""radar60 Rev A integration: the schematic's board, the RF macro and the floorplan into one
board, placed by yapnr's Monte Carlo placement search, stopped before routing.

Steps (each reads the previous one's files in WORK; nothing is placed by hand):

``source``   the board yapnr ingests (kicad_ops.py source): the floorplan board with every
             footprint of the atopile build, U1 at its fixed pose and the RF macro merged in
             U1's frame (columns, dummy columns, loads RT1-RT4, mask islands, feeds, vias and
             zones), every macro item in the KiCad group RFM1_MACRO (the engine's
             ``fixed_block``), the board's In1 GND plane cut out of the RF region.
``prepare``  ingest (pnr.ingest); the fixed block's copper (pnr.fixed_copper) and its
             footprints held out of the placement graph (pnr.fixed_block.hold_out); U1's fanout
             planned with that copper (pnr.fanout): every surface exit becomes a placement
             keepout (bands.py; a ball-anchored slot that meets another net's band is refused
             by name), and the parts given a bottom site lose their top-side regions; the
             placement constraints (constraints.yaml without the proposed rf_macro section,
             plus the R4 region and the bands); the routing rules under the pcbway-adv-6l-rf
             profile, carrying the fixed copper; the set checks (every radio capacitor in
             exactly one decoupling set).
``place``    pnr.mc.halving stage 0 only (--stop-after place): N seeded global starts
             (stratified and Latin-hypercube, initial_pool), each legalized and scored by the
             engine's width-aware routability proxy. Run it through the shared heavy-run queue.
``select``   the winner, mechanically: legal candidates that pass the placement audit
             (audit.py), ranked by the engine's own stage-0 key (proxy score, then cheap score),
             then HPWL, then id. Every candidate's audit and rank go to WORK/selection.json.
``finish``   pnr.writeback of the winner (placement only, no routes; the copper keepouts as
             rule areas and custom rules), then kicad_ops.py finish (outline, locks, zone fill,
             macro digest R1 v2), the project and custom rules beside it, KiCad's DRC, the
             placement audit and the RF audit (rf_audit.py A1-A6) of the final board, and its
             report.
``render``   kicad-cli pcb render: top, angled and bottom views (labelled with --label-python,
             a Python with Pillow).

Interpreters: this script runs under a numeric Python with PyYAML (the engine's
pnr.mc.halving needs torch and numpy); ``--kicad-python`` (or $PNR_KICAD_PYTHON) is KiCad's
own Python with pcbnew; ``--engine`` is the yapnr checkout whose hardware/pnr runs;
``--kicad-cli`` (or $PNR_KICAD_CLI).
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
MACRO_VARIANTS = {"m": -1, "n": 0, "p": +1}  # D12 bracketing (patch length x0.982 / x1 / x1.018)


def macro_board(variant):
    """The RF track's macro board of one D12 variant (rfm1-n by default)."""
    name = "rfm1-" + variant
    return HERE.parent / "rf" / "generated" / name / (name + ".kicad_pcb")


BOARD_NAME = "radar60-reva"
OFFSET = 30.0  # KiCad page offset of the board origin (gen_board.OFFSET, pnr.writeback)


def kicad_xy(floorplan, x, y):
    """Board frame (mm, origin lower left, +y north) to KiCad page mm (y down)."""
    return (round(OFFSET + x, 6), round(OFFSET + float(floorplan["board"]["height"]) - y, 6))


def kicad_rect(floorplan, r):
    (x0, y1), (x1, y0) = kicad_xy(floorplan, r[0], r[1]), kicad_xy(floorplan, r[2], r[3])
    return [x0, y0, x1, y1]


def macro_refs(variant):
    """The fixed block's footprints: RFM1 and the record's dummy loads."""
    rec = json.loads(macro_board(variant).with_suffix(".json").read_text())
    return ["RFM1"] + sorted(spec["ref"] for spec in (rec.get("loads") or {}).values())


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
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    u1 = floorplan["u1"]
    params = {
        "macro_pcb": str(macro_board(a.macro_variant)),
        "u1": {
            "address": floorplan["parts"]["radio"],
            "at_kicad": list(kicad_xy(floorplan, *u1["at"])),
            # KiCad's orientation of a top-side part equals the engine's (both CCW)
            "orientation_deg": float(u1["rot"]),
        },
        "rf_region_kicad": [kicad_rect(floorplan, r) for r in floorplan["rf"]["region"]],
    }
    (work / "source-params.json").write_text(json.dumps(params, indent=1))
    out = _run(
        [
            a.kicad_python,
            HERE / "kicad_ops.py",
            "source",
            HERE / "radar60.kicad_pcb",
            a.ato_board,
            work / "source.kicad_pcb",
            work / "source-params.json",
        ],
        log=work / "source.log",
    )
    summary = json.loads(out.strip().splitlines()[-1])
    (work / "source.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, sort_keys=True)[:2000])
    # The custom rules and the project beside the source: the fixed-copper export and the
    # writeback read the board's rules from there.
    shutil.copyfile(HERE / "radar60.kicad_pro", work / "source.kicad_pro")
    shutil.copyfile(HERE / "radar60.kicad_dru", work / "source.kicad_dru")
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


def _r4_region(doc, floorplan, graph_doc):
    """R4 as a placement region (the engine's noise_keepout is only proposed): every movable
    part with a guard-restricted pad (a routed net outside the RF, PWR, GND and ANALOG classes:
    the custom rule's own test) stays out of the RF region and its 5 mm guard band."""
    import audit

    fixed = {k for k in doc.get("fixed", {})}
    counts = {}
    for c in graph_doc["components"]:
        for p in c["pads"]:
            if p.get("net"):
                counts[p["net"]] = counts.get(p["net"], 0) + 1
    digital = sorted(
        c["address"]
        for c in graph_doc["components"]
        if c.get("address")
        and "@" + _glob_literal(c["address"]) not in fixed
        and c["ref"] not in fixed
        and any(audit.guard_restricted(p.get("net"), floorplan, counts) for p in c["pads"])
    )
    return digital, {
        "name": "r4_digital",
        "refs": ["@" + _glob_literal(x) for x in digital],
        "areas": [{"rect": r} for r in audit.digital_region(floorplan)],
        "hard": True,
        "reason": "R4: no digital copper within 5 mm of the RF region (derived by integrate.py)",
    }


def plan_fanout(engine, graph, compiled, rules, cache_dir=None):
    """U1's fanout as the router (and the placement's bottom-site derivation) plans it: U1 at
    its fixed pose, the fixed block's copper from the rules. The plan (several minutes) is kept
    in ``cache_dir`` by its inputs' digest (pnr.fanout.planner.inputs_sha256)."""
    sys.path[:0] = [str(Path(engine) / "hardware/pnr"), str(engine)]
    from pnr.fanout.bottom import _posed
    from pnr.fanout.planner import cached_plan, classify, inputs_sha256, plan_layers

    spec = rules["fanouts"][0]
    posed = _posed(graph, compiled, [spec["ref"]])
    layers, drops, signals = classify(posed, rules)
    part = posed.component(spec["ref"])
    fixed = rules.get("fixed_copper")
    key = inputs_sha256(
        posed, rules, spec, plan_layers(spec, layers, part.side), drops, signals, fixed
    )
    cached = Path(cache_dir) / ("fanout-%s.json" % key[:16]) if cache_dir else None
    if cached and cached.is_file():
        hit = json.loads(cached.read_text())
        if hit.get("key") == key:
            return spec, part, hit["plan"]
    plan = cached_plan(
        posed,
        rules,
        spec,
        grid_layers=layers,
        plane_nets=drops,
        signal_nets=signals,
        fixed_copper=fixed,
    )
    if cached:
        cached.write_text(json.dumps({"key": key, "plan": plan}, default=str))
    return spec, part, plan


def _slots(floorplan, graph_doc):
    """The ball-anchored slots (floorplan regions with ``slot: true``) with their parts' nets."""
    nets_of = {
        c.get("address"): {p["net"] for p in c["pads"] if p.get("net")}
        for c in graph_doc["components"]
    }
    out = []
    for name, spec in floorplan["regions"].items():
        if not spec.get("slot"):
            continue
        nets = set()
        for role in spec["parts"]:
            pats = floorplan["parts"][role]
            pats = pats if isinstance(pats, list) else [pats]
            for address, ns in nets_of.items():
                if address and any(fnmatch.fnmatchcase(address, q) for q in pats):
                    nets |= ns
        rects = spec["areas"] if "areas" in spec else [spec["rect"]]
        for k, r in enumerate(rects):
            out.append(
                dict(name=name if not k else "%s.%d" % (name, k), rect=r, nets=nets - {"GND"})
            )
    return out


def step_prepare(a):
    import bands

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
            inputs / "graph-full.json",
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
    )
    doc = yaml.safe_load((HERE / "constraints.yaml").read_text())
    doc.pop("rf_macro", None)  # proposed; the macro is the fixed_block
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    # The fixed block's copper (fixed.json schema 2) as the router exports it.
    _graph, _compiled, rules0 = _compile(a.engine, inputs / "graph-full.json", doc)
    (inputs / "rules0.json").write_text(json.dumps(rules0, indent=1, sort_keys=True))
    _run(
        [
            a.kicad_python,
            "-m",
            "pnr.fixed_copper",
            work / "source.kicad_pcb",
            "--export-dir",
            inputs / "fixed",
            "--rules",
            inputs / "rules0.json",
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
        log=work / "fixed.log",
    )
    fixed_copper = json.loads((inputs / "fixed" / "fixed.json").read_text())
    # The block's footprints (RFM1, RT1-RT4) leave the placement graph.
    sys.path[:0] = [str(Path(a.engine) / "hardware/pnr"), str(a.engine)]
    from pnr.fixed_block import hold_out
    from pnr.graph import BoardGraph

    graph = BoardGraph.from_json((inputs / "graph-full.json").read_text())
    held = hold_out(graph, macro_refs(a.macro_variant))
    (inputs / "graph.json").write_text(graph.to_json())
    graph_doc = json.loads((inputs / "graph.json").read_text())
    digital, r4 = _r4_region(doc, floorplan, graph_doc)
    doc.setdefault("region", []).append(r4)

    # U1's fanout before placement: exit bands and bottom sites.
    graph, compiled, rules = _compile(a.engine, inputs / "graph.json", doc)
    rules["fixed_copper"] = fixed_copper
    spec, u1, plan = plan_fanout(a.engine, graph, compiled, rules, cache_dir=inputs)
    half = float(floorplan["u1"]["courtyard_mm"]) / 2.0
    cx, cy = u1.pos
    courtyard = [cx - half, cy - half, cx + half, cy + half]
    eb = floorplan["fanout"]["exit_bands"]
    strips = bands.exit_strips(
        plan["terminals"], courtyard, float(eb["width_mm"]), float(eb["beyond_courtyard_mm"])
    )
    kept, own, errors = bands.judge_slots(strips, _slots(floorplan, graph_doc))
    merged = bands.merge_strips(kept)
    sites = (plan.get("bottom") or {}).get("sites") or {}
    by_ref = {c["ref"]: c.get("address") for c in graph_doc["components"]}
    site_addresses = sorted(by_ref[r] for r in sites if by_ref.get(r))
    removed = bands.drop_site_parts(doc, site_addresses)
    doc.setdefault("keepout", []).extend(bands.keepout_entries(merged))
    fanout_report = {
        "inputs_sha256": plan.get("inputs_sha256"),
        "diagnostics": {
            k: plan["diagnostics"].get(k)
            for k in ("signals", "signals_escaped", "drops", "drops_placed", "kinds", "failed")
        },
        "exit_strips": strips,
        "own_path_strips": own,
        "bands": merged,
        "slot_errors": errors,
        "bottom_sites": {r: dict(site, address=by_ref.get(r)) for r, site in sorted(sites.items())},
        "bottom_unplaced": (plan.get("bottom") or {}).get("unplaced"),
        "site_parts_removed_from": removed,
    }
    (work / "fanout-plan.json").write_text(json.dumps(fanout_report, indent=1, default=str))
    if errors:
        raise SystemExit("ball-anchored slots meet exit bands:\n  " + "\n  ".join(errors))

    (work / "constraints-place.yaml").write_text(
        "# constraints.yaml without the proposed rf_macro section, plus the R4 region, the fanout\n"
        "# exit bands and the bottom-site edits (integrate.py prepare; generated)\n"
        + yaml.safe_dump(doc, sort_keys=False)
    )
    graph, compiled, rules = _compile(a.engine, inputs / "graph.json", doc)
    rules["fixed_copper"] = fixed_copper
    (inputs / "rules.json").write_text(json.dumps(rules, indent=1, sort_keys=True))
    sets = decoupling_sets(floorplan, [c.address for c in graph.components if c.address])
    # Every PMIC part is in the block region but the damping options (a list, not pmic.*)
    pmic_all = {c.address for c in graph.components if (c.address or "").startswith("pmic.")}

    def matched(role):
        pats = floorplan["parts"][role]
        pats = pats if isinstance(pats, list) else [pats]
        return {a for a in pmic_all if any(fnmatch.fnmatchcase(a, q) for q in pats)}

    sets["pmic_unassigned"] = sorted(
        pmic_all - matched("pmic_parts") - matched("pmic_filters") - matched("supply_damping")
    )
    report = {
        "components": len(graph.components),
        "nets": len(graph.nets),
        "held_out": held,
        "fixed_blocks": [
            {k: b.get(k) for k in ("name", "group", "anchor", "sha256", "refs")}
            for b in fixed_copper.get("blocks") or []
        ],
        "warnings": compiled.warnings,
        "constraints": sorted({c.kind for c in compiled.constraints}),
        "decoupling_sets": sets,
        "r4_digital_parts": len(digital),
        "r4_region_rects": r4["areas"],
        "fanout": {
            k: fanout_report[k] for k in ("diagnostics", "bands", "bottom_sites", "bottom_unplaced")
        },
        "own_path_strips": [(s["ball"], s["net"], s["slots"]) for s in own],
    }
    (work / "prepare.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str)[:6000])
    if sets["uncovered"] or sets["in_several"]:
        raise SystemExit("decoupling sets do not partition the radio capacitors")
    if sets["pmic_unassigned"]:
        raise SystemExit("PMIC parts in no block role: %s" % sets["pmic_unassigned"])


# ---------------------------------------------------------------- place / select


def step_place(a):
    work = Path(a.work)
    out = work / "mc"
    t = time.time()
    # pnr.mc.halving, with prepare's fanout plan seeded into each worker (halving_seeded.py)
    _run(
        [
            a.python,
            HERE / "halving_seeded.py",
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
        env=_env(a.engine, {"RADAR60_FANOUT_CACHE": str(work / "inputs")}),
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
        "macro_variant": a.macro_variant,
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
        a.macro_variant = rec.get("macro_variant", a.macro_variant)
    else:
        sel = json.loads((work / "selection.json").read_text())
        placed_json = work / "mc" / "cand" / sel["winner"] / "placed.json"
    placed = work / "placed.kicad_pcb"
    # The board's project and custom rules beside the writeback's output: writeback patches the
    # project's rules and appends the copper keepouts' custom rules to the .kicad_dru (only its
    # own marked block), and both go beside the final board.
    shutil.copyfile(HERE / "radar60.kicad_pro", placed.with_suffix(".kicad_pro"))
    shutil.copyfile(HERE / "radar60.kicad_dru", placed.with_suffix(".kicad_dru"))
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
    shutil.copyfile(placed.with_suffix(".kicad_pro"), out_dir / (BOARD_NAME + ".kicad_pro"))
    shutil.copyfile(placed.with_suffix(".kicad_dru"), out_dir / (BOARD_NAME + ".kicad_dru"))
    _run(
        [
            a.kicad_python,
            HERE / "kicad_ops.py",
            "finish",
            placed,
            HERE / "radar60.kicad_pcb",
            macro_board(a.macro_variant),
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
    macro_record = json.loads(macro_board(a.macro_variant).with_suffix(".json").read_text())
    # The RF audit (A1-A6) of the filled board: the macro as merged, in U1's frame.
    finish = json.loads((work / "finish.json").read_text())
    rf = work / "rf-audit.json"
    subprocess.run(
        [
            a.kicad_python,
            HERE / "rf_audit.py",
            board,
            macro_board(a.macro_variant).with_suffix(".json"),
            "--u1",
            "%s,%s" % tuple(finish["u1"]["at_kicad"]),
            "--net-prefix",
            "RF_",
            "--out",
            rf,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    prepare = json.loads((work / "prepare.json").read_text())
    fanout_plan = json.loads((work / "fanout-plan.json").read_text())
    report = audit.board_audit(
        json.loads(poses.read_text()),
        json.loads(drc.read_text()),
        floorplan,
        macro_record,
        finish,
        sel,
        rf_audit=json.loads(rf.read_text()) if rf.is_file() else None,
        fanout=fanout_plan,
        prepare=prepare,
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
        png = out / ("radar60-reva-placement-%s.png" % name)
        _run(
            [a.kicad_cli, "pcb", "render", "-o", png, "--width", "2400", "--height", "1800"]
            + ["--quality", "high", "--background", "opaque"]
            + extra
            + [board]
        )
        files.append(png)
    if a.label_python:
        _run([a.label_python, HERE / "label_png.py", "Rev A placed, not routed"] + files)
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
    ap.add_argument(
        "--macro-variant",
        choices=sorted(MACRO_VARIANTS),
        default="n",
        help="the RF macro's D12 variant to merge (rfm1-m/-n/-p; one placement fits all three)",
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
