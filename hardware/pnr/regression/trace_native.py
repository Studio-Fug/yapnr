"""The ladder runner's side of :mod:`pnr.trace` (``run.py --trace``): one traced case.

The ``place-route`` stage records the engine lane (``PNR_TRACE_DIR=CASE/trace``, set for that
stage only). After ``writeback`` and ``planes`` the runner copies the board, with its project,
custom rules (``.kicad_dru``) and library table, to ``CASE/trace/native/<stage>/``, runs
``kicad-cli pcb drc`` on the copy (never on the saved board), removes the copied library table
again (it holds absolute library paths; the trace stays path-free) and appends a ``board``
event to the ``native`` lane; after the
final DRC it records the saved board as the ``refill`` stage with the runner's own report, and
the acceptance fields as a ``result`` event. Failures here never fail a case: they disable the
native lane and are written to ``CASE/trace/errors.json``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pnr import trace
from pnr.trace_board import read, refine_header

STAGES = ["generate", "place-route", "writeback", "planes", "refill", "audit", "drc", "via-scan"]


class NativeTrace:
    """The native lane of one case's trace directory."""

    def __init__(self, root, spec, seed, args, run_name):
        self.root = Path(root)
        self.dir = self.root / "trace"
        self.recorder = None
        self.total = None
        config = dict(
            rounds=args.rounds,
            initial_pool=bool(args.initial_pool),
            initial_starts=args.initial_starts if args.initial_pool else None,
            initial_finalists=args.initial_finalists if args.initial_pool else None,
            detail_pitch_mm=args.detail_pitch_mm,
            packed_maze=bool(args.packed_maze),
            batched_wirelength=bool(args.batched_wirelength),
            dense_maze_cost=bool(args.dense_maze_cost),
            fab_profile=getattr(args, "fab_profile", None),
        )
        self.every = getattr(args, "trace_placement_every", None)
        if self.every:
            config["trace_placement_every"] = int(self.every)  # recorded only when set
        subject = dict(
            kind="ladder-case",
            case=spec["name"],
            seed=seed,
            description=spec["description"],
            parts=len(spec["parts"]),
            layers=spec["constraints"]["board"]["layers"],
        )
        trace.write_run(
            self.dir,
            dict(schema="pnr-trace-run-v1", subject=subject, stages=STAGES, config=config),
        )

    def environment(self):
        """The variables of the ``place-route`` stage (the runner strips ambient ones)."""
        env = dict(PNR_TRACE_DIR=str(self.dir), PNR_TRACE_LANE="engine")
        if self.every:
            env[trace.ENV_PLACEMENT_EVERY] = str(int(self.every))
        return env

    def _open(self):
        if self.recorder is None:
            header_path = self.dir / "header.json"
            header = json.loads(header_path.read_text())
            source = read(self.root / "source.kicad_pcb")
            refine_header(header, source)
            header_path.write_text(json.dumps(header, indent=1, sort_keys=True) + "\n")
            self.total = header.get("connections_total")
            self.recorder = trace.Recorder(self.dir, lane="native")
        return self.recorder

    def _record(self, stage, board, report):
        recorder = self._open()
        recorder.board(stage, read(board), report, self.total)

    def _guard(self, where, action):
        try:
            action()
        except Exception as error:  # noqa: BLE001 - tracing never fails a case
            trace.record_error(self.dir, "native", where, error)
            if self.recorder is not None:
                self.recorder.close()
            self.recorder = _Off()

    def snapshot(self, stage, board, kicad_cli, timeout):
        """Record a copy of ``board`` after ``stage`` with its own KiCad DRC."""

        def action():
            recorder = self._open()
            if not recorder.active:
                return
            folder = self.dir / "native" / stage
            folder.mkdir(parents=True)
            copy = folder / "board.kicad_pcb"
            shutil.copy2(board, copy)
            # KiCad reads <board>.kicad_pro and <board>.kicad_dru: the copy judges like the board.
            for suffix in (".kicad_pro", ".kicad_dru"):
                if Path(board).with_suffix(suffix).exists():
                    shutil.copy2(Path(board).with_suffix(suffix), copy.with_suffix(suffix))
            table = folder / "fp-lib-table"
            if (Path(board).parent / "fp-lib-table").exists():
                shutil.copy2(Path(board).parent / "fp-lib-table", table)
            report = folder / "drc.json"
            command = [kicad_cli, "pcb", "drc", str(copy), "--format", "json", "--output"]
            try:
                subprocess.run(
                    command + [str(report)], check=True, timeout=timeout, capture_output=True
                )
            finally:
                if table.exists():
                    table.unlink()  # absolute library paths stay out of the trace
            self._record(stage, copy, json.loads(report.read_text()))

        self._guard("native_" + stage, action)

    def finish(self, board, drc, result):
        """The saved board (``refill``) with the runner's DRC report, and the verdict."""

        def action():
            recorder = self._open()
            if not recorder.active:
                return
            self._record("refill", board, drc)
            recorder.result(
                case=result["case"],
                seed=result["seed"],
                passed=bool(result["passed"]),
                reasons=list(result.get("reasons") or []),
                opens=result.get("opens"),
                violations=result.get("violations"),
                rules=trace.drc_rules(drc) if drc else {},
                vias=result.get("vias"),
                tracks=result.get("tracks"),
                copper_length_mm=result.get("copper_length_mm"),
            )
            recorder.close()

        self._guard("native_finish", action)


class _Off:
    """A closed native lane."""

    active = False

    def board(self, *args, **kwargs):
        return None

    def result(self, **kwargs):
        return None

    def close(self):
        return None
