"""Ordinary routing after native paired placement, retaining exact fixed copper."""

import json
import os
import subprocess
import time
from pathlib import Path


def run(board, rules, constraints, out, kicad_python, kicad_cli, iterations=12):
    import yaml

    from pnr.constraints import compile_constraints
    from pnr.graph import BoardGraph
    from pnr.route.detail.router import route_board

    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    rules = Path(rules).resolve()
    board = Path(board).resolve()
    # The KiCad workers import pnr and, for a vendor data profile (PNR_FAB_PROFILE), the
    # yapnr package beside it (yapnr.fab: the profile files).
    pnr_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(pnr_root), str(pnr_root.parent.parent)]))

    def invoke(args, name):
        from pnr.proc import (  # one KiCad worker: PNR_WORKER_TIMEOUT; stays in this process group
            run_checked,
        )

        with (out / name).open("w") as f:
            run_checked(args, session=False, env=env, stdout=f, stderr=subprocess.STDOUT)

    # With fixed blocks declared (rules fixed_blocks) the export holds their
    # footprints out of placed.json and records their copper as blocks.
    invoke(
        [kicad_python, "-m", "pnr.fixed_copper", str(board), "--export-dir", str(out)]
        + (["--rules", str(rules)] if json.loads(rules.read_text()).get("fixed_blocks") else []),
        "export.log",
    )
    g = BoardGraph.from_json((out / "placed.json").read_text())
    policy = json.loads(rules.read_text())
    cc = compile_constraints(
        yaml.safe_load(Path(constraints).read_text()),
        g.refs,
        {c.address: c.ref for c in g.components},
        {f"{c.address}:{p.name}": p.net for c in g.components for p in c.pads},
    )
    g.components = [c for c in g.components if c.ref not in {h["name"] for h in cc.mounting_holes}]
    from pnr.live import emit

    emit(
        "signal_start",
        board=board,
        layout=json.loads(g.to_json()),
        data=dict(phase="signals", provisional=True),
    )
    # board.edge: exact: this board's own outline for the router (pnr.board_edge);
    # board.dru_routing: its custom rules where they constrain routing (pnr.dru_rules).
    from pnr.board_edge import attach_edges
    from pnr.dru_rules import attach_dru

    attach_edges(policy, board.read_text())
    dru_path = board.with_suffix(".kicad_dru")
    attach_dru(
        policy, dru_path.read_text() if dru_path.exists() else None, [n.name for n in g.nets]
    )
    started = time.monotonic()
    result = route_board(
        g,
        cc,
        policy,
        max_iters=iterations,
        fixed_copper=json.loads((out / "fixed.json").read_text()),
    )
    routes = out / "routes.json"
    payload = dict(tracks=result.tracks, vias=result.vias, unrouted=result.result.unrouted)
    if result.via_spans:  # blind, buried and micro vias (pnr.via_policy)
        payload["via_spans"] = result.via_spans
    payload.update(result.extras())  # a declared fanout's via sizes and locked copper
    routes.write_text(json.dumps(payload))
    (out / "result.json").write_text(
        json.dumps(
            dict(
                seconds=time.monotonic() - started,
                unfinished_signal=sorted(set(result.result.unrouted) - result.deferred_nets),
                deferred=sorted(result.deferred_nets),
                failure_sites=result.failure_sites,
            ),
            indent=2,
        )
    )
    final = out / "candidate.kicad_pcb"
    invoke(
        [
            kicad_python,
            "-m",
            "pnr.fixed_copper",
            str(board),
            "--fixed",
            str(out / "fixed.json"),
            "--routes",
            str(routes),
            "--rules",
            str(rules),
            "--out",
            str(final),
        ],
        "append.log",
    )
    invoke(
        [kicad_python, "-m", "pnr.planes", str(final), "--rules", str(rules), "--refill-only"],
        "refill.log",
    )
    if policy.get("ir_drop"):
        # The rails' IR-drop report on the refilled board (pnr.ir_extract), declared only.
        invoke(
            [kicad_python, "-m", "pnr.ir_extract", str(final), "--rules", str(rules)]
            + ["--out", str(out / "ir"), "--heatmaps"],
            "ir.log",
        )
    from pnr.native_drc import run_drc

    run_drc(kicad_cli, board, out / "baseline.drc.json", env=env)
    run_drc(kicad_cli, final, out / "candidate.drc.json", env=env)
    cmd = [
        kicad_python,
        "-m",
        "pnr.fixed_copper",
        str(board),
        "--validate",
        str(final),
        "--rules",
        str(rules),
        "--before-drc",
        str(out / "baseline.drc.json"),
        "--after-drc",
        str(out / "candidate.drc.json"),
        "--out",
        str(out / "checks.json"),
    ]
    references = board.parent / "paired-reference.json"
    if references.exists():
        cmd += ["--references", str(references)]
    invoke(cmd, "validate.log")
    return final
