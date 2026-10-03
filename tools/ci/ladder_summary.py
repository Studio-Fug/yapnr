#!/usr/bin/env python3
"""Markdown summary of a regression ladder run for the GitHub step summary.

    python3 tools/ci/ladder_summary.py RUN_DIR [--title TEXT] [--check]

Reads ``RUN_DIR/summary.json`` (written by ``hardware/pnr/regression/run.py``) and prints a
table: case, seed, result, KiCad opens and violations, vias, copper, seconds and the failure
reasons. A run with the opt-in gloss stage (``run.py --gloss``, PNR_GLOSS) gets a second table:
whether the stage kept its output, its transactions, why it stopped, its outer gate and the
bends and length before and after. Only case names, numbers and the runner's reason keys are
printed (no error text, which may hold paths). ``--check`` exits 1 unless every case passed.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

STAGES = ("generate", "place-route", "writeback", "planes", "refill", "audit", "drc", "via-scan")


def _violations(value):
    if isinstance(value, dict):
        return sum(int(v) for v in value.values()), ", ".join(
            "%s %d" % (k, v) for k, v in sorted(value.items())
        )
    return value, ""


def _reasons(result):
    reasons = list(result.get("reasons") or [])
    if "stage_failure" in reasons:
        done = [s for s in STAGES if s in (result.get("stages") or {})]
        failed = STAGES[len(done)] if len(done) < len(STAGES) else "after via-scan"
        reasons[reasons.index("stage_failure")] = "stage_failure (%s)" % failed
    return reasons


def _number(value, digits=1):
    if value is None:
        return "-"
    if isinstance(value, float):
        return ("%%.%df" % digits) % value
    return str(value)


def render(summary, title="Regression ladder"):
    """The Markdown of a ``summary.json`` document."""
    results = summary.get("results", [])
    passed = sum(1 for r in results if r.get("passed"))
    lines = ["### %s" % title, ""]
    state = "complete" if summary.get("complete") else "incomplete"
    lines.append("**%d of %d cases passed** (%s run)." % (passed, len(results), state))
    changed = summary.get("source_changed_during_run") or []
    if changed:
        lines.append("")
        lines.append(
            "Sources changed during the run: %d file(s); the run does not count." % len(changed)
        )
    lines += [
        "",
        "| Case | Seed | Result | Opens | Violations | Vias | Copper mm | Seconds | Reasons |",
        "| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for r in results:
        count, kinds = _violations(r.get("violations"))
        violations = _number(count) + (" (%s)" % kinds if kinds else "")
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                r.get("case", "?"),
                r.get("seed", "?"),
                "pass" if r.get("passed") else "**FAIL**",
                _number(r.get("opens")),
                violations,
                _number(r.get("vias")),
                _number(r.get("copper_length_mm")),
                _number(r.get("elapsed_seconds")),
                ", ".join(_reasons(r)) or "",
            )
        )
    glossed = [r for r in results if isinstance(r.get("gloss"), dict)]
    if glossed:
        lines += _gloss_table(glossed)
    return "\n".join(lines) + "\n"


def _gloss_table(results):
    """The gloss stage of each case (run.py --gloss): kept, transactions, stop, gate, figures."""
    lines = [
        "",
        "**Gloss stage** (PNR_GLOSS): %d of %d cases kept an edit."
        % (sum(1 for r in results if r["gloss"].get("kept")), len(results)),
        "",
        "| Case | Seed | Kept | Transactions | Stop | Outer gate | Bends | Length mm |",
        "| --- | ---: | --- | ---: | --- | --- | --- | --- |",
    ]
    for r in results:
        block = r["gloss"]
        summary = block.get("summary") or {}
        before = summary.get("metrics_before") or {}
        after = summary.get("metrics_after") or {}
        gate = block.get("outer_gate") or {}
        if block.get("status") == "error":
            gate_text = "**error**"
        elif gate:
            gate_text = "pass" if gate.get("passed") else "restored"
        else:
            gate_text = "-"
        lines.append(
            "| %s | %s | %s | %s of %s | %s | %s | %s -> %s | %s -> %s |"
            % (
                r.get("case", "?"),
                r.get("seed", "?"),
                "yes" if block.get("kept") else "no",
                _number(summary.get("accepted_transactions")),
                _number(summary.get("proposed_transactions")),
                summary.get("stop") or "-",
                gate_text,
                _number(before.get("bends_all")),
                _number(after.get("bends_all")),
                _number(before.get("length_mm")),
                _number(after.get("length_mm")),
            )
        )
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", type=Path, help="the ladder's output directory")
    parser.add_argument("--title", default="Regression ladder")
    parser.add_argument("--check", action="store_true", help="exit 1 unless every case passed")
    args = parser.parse_args(argv)
    path = args.run / "summary.json"
    if not path.is_file():
        print("### %s\n\nNo `summary.json`: the ladder did not start.\n" % args.title)
        return 1 if args.check else 0
    summary = json.loads(path.read_text())
    sys.stdout.write(render(summary, args.title))
    ok = bool(summary.get("passed")) and bool(summary.get("complete"))
    return 0 if ok or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
