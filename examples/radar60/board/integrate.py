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
             With ``--library`` (power_block.py's library.json) every start places the
             power-stage block as one rigid macro drawn from that library
             (pnr.hier.top.hierarchical_place) and expands it.
``select``   the winner, mechanically: legal candidates that pass the placement audit
             (audit.py), ranked by the engine's own stage-0 key (proxy score, then cheap score),
             then HPWL, then id. Every candidate's audit and rank go to WORK/selection.json.
``finish``   pnr.writeback of the winner (placement only, no routes; the copper keepouts as
             rule areas and custom rules), then kicad_ops.py finish (outline, locks, zone fill,
             macro digest R1 v2), the project and custom rules beside it, an fp-lib-table
             extracted from the finished board's own footprints (pnr.library_table, so
             independent DRC resolves the synthetic Radar60_* lib nicknames), KiCad's DRC, the
             placement audit and the RF audit (rf_audit.py A1-A6) of the final board, and its
             report.
``block``    (with a power-stage library, plan R1/E2) the chosen power-stage layout made the
             fixed block: its routed copper and outer pours drawn into the finished board as
             the KiCad group PWR_STAGE (pnr.hier.assemble --zones --group --anchor), and the
             ``fixed``/``fixed_block`` entries of pnr.hier.macro.fixed_block_from_macro (with
             the group's digest) added to the routing constraints (WORK/constraints-route.yaml,
             WORK/inputs/rules-route.json), which route and check then use.
``route``    pnr.staged_signal on the finished board (class-clearance maze, DRU routing, exact
             edge, the In3 plane partition and IR-drop report already in the policy): route the
             remaining signal nets, append the copper, refill, IR-drop (pnr.ir_extract) and the
             internal baseline/candidate DRC and fixed-copper validation. Diagnostics (unrouted
             nets, failure sites) to WORK/route-diag.json; the routed board to
             WORK/route/candidate.kicad_pcb.
``check``    independent DRC on every severity, R1 (the macro's copper digest unchanged by
             routing), R4 (no foreign copper next to the RF region), LVDS pair lengths and
             skew, QSPI net lengths (kicad_ops.py measure) and the IR-drop report per rail, all
             against the routed board. Written to OUT/measure.json.
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
import re
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
        ]
        + (["--library", Path(a.library).resolve()] if a.library else []),
        env=_env(a.engine, {"RADAR60_FANOUT_CACHE": str(work / "inputs")}),
        cwd=a.engine,
        log=work / "place.log",
    )
    print("placement search: %.0f s" % (time.time() - t))


def _records(work):
    path = Path(work) / "mc" / "dataset.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _top_candidates(legal, top_n):
    """The legal, audit-passing candidates to write, in the same rank order ``step_select``
    already sorted ``legal`` into, capped at ``top_n`` (stage 3c R7: route more than one
    placement, not just the single stage-0 winner)."""
    return [r for r in legal if r.get("audit_pass")][: max(1, int(top_n or 1))]


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
    # --top-n > 1 (stage 3c R7): every legal, audit-passing candidate up to N, ranked the same
    # way as the winner, each into its own subdirectory so a later wave can route more than one
    # and compare -- not just the single best by this stage-0 proxy.
    top_n = max(1, int(getattr(a, "top_n", 1) or 1))
    passing = _top_candidates(legal, top_n)
    top_dir = Path(a.out) / "candidates"
    written = []
    for r in passing:
        d = top_dir / ("rank%d" % r["rank"])
        write_placement(a, work, recs, r["id"], sel, out_dir=d)
        written.append({"rank": r["rank"], "id": r["id"], "dir": str(d)})
    if written:
        top_dir.mkdir(parents=True, exist_ok=True)
        (top_dir / "index.json").write_text(
            json.dumps(
                {"requested": top_n, "written": written, "available": len(passing)}, indent=1
            )
        )
    print(
        json.dumps({k: v for k, v in sel.items() if k != "candidates"}, indent=1),
        "\n",
        json.dumps(sel["candidates"][:5], indent=1),
    )
    if written:
        print("top-%d candidates: %s" % (top_n, json.dumps(written, indent=1)))
    if not winner:
        raise SystemExit("no candidate passes the audit")


def write_placement(a, work, recs, winner, sel, out_dir=None):
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
        # the block layouts a hierarchical start drew (pnr.hier.top), with their routed boards
        "hier": rec.get("hier"),
        "frame": "mm, origin at the board's lower-left corner, +y north, rot CCW; [x, y, rot, side]",
        "poses": {
            key[ref]: pose for ref, pose in sorted(rec["poses"].items(), key=lambda kv: key[kv[0]])
        },
    }
    out_dir = Path(out_dir) if out_dir is not None else Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "placement.json").write_text(json.dumps(out, indent=1) + "\n")


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
    # fp-lib-table: the board's parts (atopile's generated/cached footprints) carry synthetic
    # lib nicknames (Radar60_*) with no checked-in source library, so independent DRC fails
    # lib_footprint_issues on every one unless the project's fp-lib-table knows them. Extracted
    # from the board's own copper (pnr.library_table.extract_footprints), so it matches exactly.
    _run(
        [
            a.kicad_python,
            "-m",
            "pnr.library_table",
            "--from-board",
            board,
            "--libs-dir",
            work / "libs",
            "--out",
            out_dir / "fp-lib-table",
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
        log=work / "fp-lib-table.log",
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


def _route_inputs(work):
    """The routing rules and constraints: ``block``'s (the power stage as a fixed block) when
    that step ran, else prepare's."""
    work = Path(work)
    if (work / "inputs" / "rules-route.json").is_file():
        return work / "inputs" / "rules-route.json", work / "constraints-route.yaml"
    return work / "inputs" / "rules.json", work / "constraints-place.yaml"


def step_block(a):
    """Plan R1/E2: the power-stage layout the placement drew, made the fixed block PWR_STAGE.

    The finished board gets the layout's routed copper and its outer pours as one KiCad group
    (pnr.hier.assemble --zones --group --anchor; the anchor, U2, stays out of it), and the
    routing constraints get the ``fixed`` pose of the anchor and the ``fixed_block`` entry
    (pnr.hier.macro.fixed_block_from_macro, from the placed graph) pinned to the group's
    digest: WORK/constraints-route.yaml and WORK/inputs/rules-route.json."""
    work = Path(a.work)
    out_dir = Path(a.out)
    board = out_dir / (BOARD_NAME + ".kicad_pcb")
    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    stage = floorplan["power_stage"]
    fb = stage["fixed_block"]
    placement = json.loads((out_dir / "placement.json").read_text())
    hier = placement.get("hier") or {}
    blocks = hier.get("blocks") or {}
    if len(blocks) != 1:
        raise SystemExit("block: the placement drew %d block layouts, not one" % len(blocks))
    ((name, choice),) = blocks.items()
    block_dir = Path(choice["native_dir"])
    sys.path[:0] = [str(Path(a.engine) / "hardware/pnr"), str(a.engine)]
    os.environ["PNR_FAB_PROFILE"] = PROFILE
    from pnr.graph import BoardGraph
    from pnr.hier.macro import MacroPlan, fixed_block_from_macro

    info = json.loads((block_dir.parents[1] / "block.json").read_text())
    refs = info["block"]["refs"]
    anchor = info["anchor"]
    addr = info["addresses"]
    placed_json = work / "placed-from-record.json"  # finish --placement
    if not placed_json.is_file():
        sel = json.loads((work / "selection.json").read_text())
        placed_json = work / "mc" / "cand" / sel["winner"] / "placed.json"
    flat = BoardGraph.from_json(placed_json.read_text())
    by_ref = {c.ref: c for c in flat.components}
    # The macro the placement drew, as pnr.hier.macro records it (members at their poses).
    plan = MacroPlan()
    plan.macros["MB00"] = dict(
        block=name,
        members={r: (*by_ref[r].pos, by_ref[r].rot, by_ref[r].side) for r in refs},
    )
    plan.member_of = {r: "MB00" for r in refs}
    fixed, entry = fixed_block_from_macro(
        flat, plan, "MB00", fb["name"], fb["group"], anchor, fb.get("solid_layers")
    )
    res = _run(
        [
            a.kicad_python,
            "-m",
            "pnr.hier.assemble",
            board,
            "--block",
            block_dir / "block.kicad_pcb",
            "--out",
            board,
            "--zones",
            "--group",
            fb["group"],
            "--anchor",
            anchor,
        ],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
        log=work / "block-assemble.log",
    )
    assembled = json.loads(res.strip().splitlines()[-1])
    doc = yaml.safe_load((work / "constraints-place.yaml").read_text())
    key = "@" + _glob_literal(addr[anchor])
    doc.setdefault("fixed", {})[key] = dict(fixed)
    doc.setdefault("fixed_block", []).append(
        dict(
            name=entry["name"],
            group=entry["group"],
            anchor=key,
            solid_layers=entry["solid_layers"],
            sha256=assembled["sha256"],
        )
    )
    (work / "constraints-route.yaml").write_text(
        "# constraints-place.yaml plus the power stage as a fixed block (integrate.py block;"
        " generated)\n" + yaml.safe_dump(doc, sort_keys=False)
    )
    rules0 = json.loads((work / "inputs" / "rules.json").read_text())
    _graph, _compiled, rules = _compile(a.engine, work / "inputs" / "graph.json", doc)
    rules["fixed_copper"] = rules0.get("fixed_copper")
    (work / "inputs" / "rules-route.json").write_text(json.dumps(rules, indent=1, sort_keys=True))
    report = dict(
        block=name,
        layout=choice,
        board=str(block_dir / "block.kicad_pcb"),
        fixed=fixed,
        fixed_block=entry,
        assembled=assembled,
    )
    (work / "block.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str)[:3000])


def step_route(a):
    """R6: pnr.staged_signal on the finished board (class-clearance maze, DRU routing, exact
    edge: the policy work/inputs/rules.json already carries) — route_board, append, refill, the
    IR-drop report if the policy declares one, baseline/candidate DRC and the fixed-copper
    validation. Diagnostics (unrouted nets, failure sites) go to WORK/route-diag.json; the
    routed board is WORK/route/candidate.kicad_pcb (``check`` reads it).

    A rejected validate (status != "ok" in WORK/route-status.json, e.g. a DRC regression
    against the baseline board) does not raise: the routed board and its diagnostics are still
    on disk, and ``check`` (and a wave loop's ``select``) classify the failure from those, not
    from this step's own exit code."""
    work = Path(a.work)
    out_dir = Path(a.out)
    board = out_dir / (BOARD_NAME + ".kicad_pcb")
    route_dir = work / "route"
    if route_dir.exists():
        shutil.rmtree(route_dir)
    sys.path[:0] = [str(Path(a.engine) / "hardware/pnr"), str(a.engine)]
    os.environ["PNR_FAB_PROFILE"] = PROFILE
    import pnr.route.detail.router as router
    from pnr.staged_signal import run as route_signals

    captured = {}
    original = router.route_board

    def _capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captured["unrouted"] = result.result.unrouted
        captured["partial_open"] = result.partial_open
        captured["failure_sites"] = result.failure_sites
        return result

    router.route_board = _capture
    t = time.time()
    status = "ok"
    rules_path, constraints_path = _route_inputs(work)
    try:
        route_signals(
            board,
            rules_path,
            constraints_path,
            route_dir,
            a.kicad_python,
            a.kicad_cli,
            iterations=a.route_iters,
        )
    except subprocess.CalledProcessError as error:
        status = "failed: %s (exit %s)" % (
            " ".join(str(c) for c in error.cmd),
            error.returncode,
        )
    finally:
        router.route_board = original
    seconds = round(time.time() - t, 1)
    (work / "route-diag.json").write_text(
        json.dumps(captured, indent=1, sort_keys=True, default=str)
    )
    (work / "route-status.json").write_text(json.dumps(dict(status=status, seconds=seconds)))
    print(
        json.dumps(
            dict(status=status, seconds=seconds, unrouted=captured.get("unrouted")), default=str
        )
    )
    if status != "ok":
        # Not fatal: a rejected validate (e.g. a DRC regression against the baseline) still
        # leaves WORK/route/candidate.kicad_pcb and its diagnostics on disk — check (and a wave
        # loop's select) read exactly those to classify the failure. A crash before route_board
        # even started (an earlier internal step's own CalledProcessError) leaves no routed
        # board, and check already refuses cleanly when it finds none.
        print("step_route: %s (continuing to check)" % status, file=sys.stderr)


# DRC check types whose project-file default severity is "ignore": kicad-cli's --severity-all
# does not promote these (they are excluded before severity-all ever sees them, at the
# project's own rule_severities), so a board with real violations of these types still DRCs
# "clean" under --severity-all alone. Wave-1 review finding #3: 9 track_not_centered_on_via,
# 5 missing_courtyard and 1 footprint_type_mismatch were invisible this way. check reports them
# honestly (promoted to "error" in a scratch copy of the project, never the one that ships)
# instead of letting a "0 violations" claim quietly depend on which checks were never run.
_DRC_IGNORED_BY_DEFAULT = (
    "footprint_filters_mismatch",
    "footprint_type_mismatch",
    "missing_courtyard",
    "track_not_centered_on_via",
    "tuning_profile_track_geometries",
)


def _run_drc(kicad_cli, board, out_json, promote=()):
    """kicad-cli pcb drc --severity-all on ``board``, optionally with the severities named in
    ``promote`` forced to "error" first (a scratch copy of the project; ``board`` itself and its
    checked-in project are never modified). Returns the parsed report."""
    if promote:
        scratch = Path(out_json).with_suffix("")
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True)
        board = Path(board)
        scratch_board = scratch / board.name
        shutil.copyfile(board, scratch_board)
        proj = json.loads(board.with_suffix(".kicad_pro").read_text())
        rs = proj["board"]["design_settings"].setdefault("rule_severities", {})
        for t in promote:
            rs[t] = "error"
        scratch_board.with_suffix(".kicad_pro").write_text(json.dumps(proj, indent=2))
        for ext in (".kicad_dru", ".kicad_prl"):
            src = board.with_suffix(ext)
            if src.is_file():
                shutil.copyfile(src, scratch_board.with_suffix(ext))
        for extra in ("fp-lib-table", "libs"):
            src = board.parent / extra
            dst = scratch / extra
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            elif src.is_file():
                shutil.copyfile(src, dst)
        board = scratch_board
    subprocess.run(
        [
            kicad_cli,
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "--units",
            "mm",
            "-o",
            str(out_json),
            str(board),
        ],
        check=False,
        capture_output=True,
    )
    return (
        json.loads(Path(out_json).read_text())
        if Path(out_json).is_file()
        else {"violations": [], "unconnected_items": []}
    )


_NET_IN_BRACKETS = re.compile(r"\[([^\]]+)\]")


def _unconnected_nets(drc_doc):
    """Net names with at least one open pad in ``unconnected_items`` (each item's description
    reads e.g. "Pad 1 [VIN_5V] of R42 on F.Cu"): the connectivity ground truth that QSPI/LVDS
    completion must be judged against, not copper length (finding #1)."""
    nets = set()
    for v in drc_doc.get("unconnected_items", []):
        for item in v.get("items", []):
            m = _NET_IN_BRACKETS.search(item.get("description", ""))
            if m:
                nets.add(m.group(1))
    return nets


def step_check(a):
    """R6: a refill (independent of route's own, so check holds even if that becomes
    conditional), DRC on every severity including the ones KiCad's project defaults exclude
    from "severity-all" (finding #3), R1 (the macro's copper digest unchanged by routing), R4
    (no foreign copper next to the RF region), rf_audit A1-A6 on the *routed* board (finding
    #2: finish only audits the pre-route board), the LVDS pairs' lengths/skew and the QSPI
    nets' lengths judged by connectivity, not copper length (finding #1), and an independent
    IR-drop re-run on the refilled candidate (finding #2, not route's cached ir.json) — all
    against WORK/route/candidate.kicad_pcb. Written to OUT/measure.json."""
    work = Path(a.work)
    out_dir = Path(a.out)
    route_dir = work / "route"
    routed = route_dir / "candidate.kicad_pcb"
    if not routed.is_file():
        raise SystemExit("no routed board at %s (run the route step first)" % routed)

    # fp-lib-table beside the routed board too: same footprints, same extraction (step_finish's).
    lib_table = out_dir / "fp-lib-table"
    if lib_table.is_file():
        shutil.copyfile(lib_table, route_dir / "fp-lib-table")
        if (work / "libs").is_dir():
            libs_dst = route_dir / "libs"
            if libs_dst.exists():
                shutil.rmtree(libs_dst)
            shutil.copytree(work / "libs", libs_dst)

    rules_path, _ = _route_inputs(work)
    policy = json.loads(rules_path.read_text()) if rules_path.is_file() else {}

    # R6: an explicit refill of the candidate before anything reads its copper. route already
    # refills once; this one is check's own, so the numbers below hold even if that changes.
    _run(
        [a.kicad_python, "-m", "pnr.planes", routed, "--rules", rules_path, "--refill-only"],
        env=_env(a.engine),
        cwd=Path(a.engine) / "hardware/pnr",
        log=work / "check-refill.log",
    )

    drc_doc = _run_drc(a.kicad_cli, routed, work / "check-drc.json")
    import collections

    drc_by_type = dict(collections.Counter(v["type"] for v in drc_doc.get("violations", [])))
    unconnected = len(drc_doc.get("unconnected_items", []))
    unconnected_nets = _unconnected_nets(drc_doc)

    # Finding #3: what --severity-all alone cannot see, named rather than silently absorbed
    # into a "0 violations" claim. Promoting checks that are already above "ignore" is a no-op,
    # so this never double-counts the primary pass's own violations.
    promoted_doc = _run_drc(
        a.kicad_cli, routed, work / "check-drc-promoted.json", promote=_DRC_IGNORED_BY_DEFAULT
    )
    drc_ignored_by_default = {
        t: n
        for t, n in collections.Counter(
            v["type"] for v in promoted_doc.get("violations", [])
        ).items()
        if t in _DRC_IGNORED_BY_DEFAULT
    }

    floorplan = yaml.safe_load((HERE / "floorplan.yaml").read_text())
    graph_doc = json.loads((work / "inputs" / "graph.json").read_text())
    rf_region = [kicad_rect(floorplan, r) for r in floorplan["rf"]["region"]]
    diff_pairs = {
        name.upper().replace("LVDS_", ""): list(pn)
        for name, pn in floorplan["nets"]["LVDS"]["pairs"].items()
    }
    qspi_globs = floorplan["nets"]["QSPI"]["globs"]
    qspi_nets = sorted(
        n["name"]
        for n in graph_doc["nets"]
        if any(fnmatch.fnmatchcase(n["name"], g) for g in qspi_globs)
    )
    params = {
        "rf_region_kicad": rf_region,
        "diff_pairs": diff_pairs,
        "nets": qspi_nets,
        "unconnected_nets": sorted(unconnected_nets),
    }
    params_path = work / "check-params.json"
    params_path.write_text(json.dumps(params, indent=1))
    measure = work / "measure-kicad.json"
    _run(
        [
            a.kicad_python,
            HERE / "kicad_ops.py",
            "measure",
            routed,
            work / "finish.json",
            params_path,
            measure,
        ],
        log=work / "measure.log",
    )
    out = {
        "drc_by_type": drc_by_type,
        "unconnected": unconnected,
        "drc_ignored_by_default": drc_ignored_by_default,
    }
    out.update(json.loads(measure.read_text()))
    # Length among connected nets only: an open net's stub length is not a routed length, and
    # must not set the "max length" bar artificially low (finding #1).
    connected_lengths = [v["length_mm"] for v in out.get("nets", {}).values() if v["connected"]]
    out["qspi_max_length_mm"] = max(connected_lengths) if connected_lengths else None

    # Finding #2a: rf_audit on the routed board, not only the pre-route one step_finish checks.
    finish_report = (
        json.loads((work / "finish.json").read_text()) if (work / "finish.json").is_file() else {}
    )
    rf_routed = work / "rf-audit-routed.json"
    if finish_report.get("u1", {}).get("at_kicad"):
        subprocess.run(
            [
                a.kicad_python,
                HERE / "rf_audit.py",
                routed,
                macro_board(a.macro_variant).with_suffix(".json"),
                "--u1",
                "%s,%s" % tuple(finish_report["u1"]["at_kicad"]),
                "--net-prefix",
                "RF_",
                "--out",
                rf_routed,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    if rf_routed.is_file():
        out["rf_audit_routed"] = json.loads(rf_routed.read_text())

    # Finding #2b: an independent IR re-run on the refilled candidate, not route's cached
    # ir.json (which predates this step's own refill and DRC and could silently drift from it).
    if policy.get("ir_drop"):
        ir_dir = work / "check-ir"
        if ir_dir.exists():
            shutil.rmtree(ir_dir)
        _run(
            [
                a.kicad_python,
                "-m",
                "pnr.ir_extract",
                routed,
                "--rules",
                rules_path,
                "--out",
                ir_dir,
            ],
            # ir_extract's numeric solve runs here when KiCad's own Python has no numpy
            # (pnr.staged_signal sets this same fallback for the route step's own IR run).
            env=_env(a.engine, {"PNR_PYTHON": a.python}),
            cwd=Path(a.engine) / "hardware/pnr",
            log=work / "check-ir.log",
        )
        ir = json.loads((ir_dir / "ir.json").read_text())
        out["ir"] = {
            net: {
                k: rep.get(k)
                for k in (
                    "status",
                    "r_eff_mohm",
                    "worst_drop_mv",
                    "budget_mohm",
                    "opens",
                    "current_a",
                    "loss_w",
                )
            }
            for net, rep in (ir.get("rails") or ir).items()
            if isinstance(rep, dict)
        }
    (out_dir / "measure.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str))
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "drc_by_type",
                    "unconnected",
                    "drc_ignored_by_default",
                    "r1_macro",
                    "nets_connected",
                    "nets_total",
                    "diff_pairs_connected_legs",
                    "diff_pairs_total_legs",
                )
                if k in out
            },
            indent=1,
            default=str,
        )
    )


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
        "step",
        choices=[
            "source",
            "prepare",
            "place",
            "select",
            "finish",
            "block",
            "route",
            "check",
            "render",
            "all",
        ],
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
    ap.add_argument(
        "--library", help="place: a power-stage block library (power_block.py library.json)"
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n0", type=int, default=24, help="placement starts")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--iters", type=int, default=600)
    ap.add_argument(
        "--top-n",
        type=int,
        default=1,
        help="select: write this many legal, audit-passing candidates (ranked), not just the "
        "winner, each to OUT/candidates/rankN/placement.json",
    )
    ap.add_argument(
        "--route-iters", type=int, default=12, help="route: pnr.staged_signal max_iters"
    )
    a = ap.parse_args(argv)
    a.renders = a.renders or str(Path(a.out) / "renders")
    sys.path.insert(0, str(HERE))
    steps = {
        "source": step_source,
        "prepare": step_prepare,
        "place": step_place,
        "select": step_select,
        "finish": step_finish,
        "block": step_block,
        "route": step_route,
        "check": step_check,
        "render": step_render,
    }
    for name in list(steps) if a.step == "all" else [a.step]:
        steps[name](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
