"""Run the showcase cases traced and render their animations for the docs
(docs/design/constraint-and-hier-animations.md, sections 5 and 6.6).

The showcases (``designs.showcases()``, outside the ladder and its gate) are shown as:

- ``showcase-chaser-line.webp`` and ``.gif``: ``07-chaser-20`` (LEDs free) beside
  ``line-chaser-20`` (LEDs D1 to D5 in one line group);
- ``showcase-edge-io.webp``: ``edge-io-12-free`` beside ``edge-io-12`` (connector, button and
  LED held on the south edge);
- ``showcase-hier-twin-bank.webp``: ``hier-twin-bank-32`` in three chapters (blocks, top level,
  knitting).

It writes those files, the ``showcases`` and ``readme_showcase`` entries of ``manifest.json``
and the ``showcases`` array of ``ladder-results.json`` (the ladder's own entries stay as they
are). ``--render-only RUN_DIR`` renders a finished showcase run (no KiCad); otherwise the run
is made first (``run.py --showcases --trace --trace-placement-every 5`` with the initial
pool, seed 0). ``--runner-arg ARG`` (repeatable) passes one more argument to ``run.py``; the
options that shaped the run (``animate_ladder.runner_options``) join each entry's ``config``.
The case titles live here, next to the ladder's (``animate_ladder.TITLES``).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from animate_ladder import (  # noqa: E402
    case_result,
    ladder_provenance,
    order_summary,
    platform_name,
    runner_options,
)

POOL = ["--initial-pool", "--initial-starts", "8", "--initial-finalists", "3"]
EVERY = "5"
# The hierarchical driver ignores the pool flags (its own block trials and top-level seeds).
HIER_CASES = ("hier-twin-bank-32",)
CASES = ("07-chaser-20", "line-chaser-20", "edge-io-12-free", "edge-io-12", "hier-twin-bank-32")
SHOWCASE_TITLES = {
    "07-chaser-20": "Five-stage chaser",
    "line-chaser-20": "Five-stage chaser, LEDs in a line group",
    "edge-io-12-free": "Hold-to-blink board, parts placed freely",
    "edge-io-12": "Hold-to-blink board, I/O on the south edge",
    "hier-twin-bank-32": "Twin-bank chaser, placed and routed hierarchically",
}
# (file, kind, cases, labels, title, format), rendered in this order (the README GIF, nearest
# its budget, last); the edge comparison also replays every start of the pool in its
# shortlist, where the order along the edge changes as the parts move.
REPLAY_POOL = ("showcase-edge-io.webp",)
SHOWCASES = (
    (
        "showcase-chaser-line.webp",
        "compare",
        ("07-chaser-20", "line-chaser-20"),
        ("LEDs placed freely", "LEDs held in a line group"),
        "Five-stage chaser: free LEDs vs. a line group",
        "webp",
    ),
    (
        "showcase-edge-io.webp",
        "compare",
        ("edge-io-12-free", "edge-io-12"),
        ("Placed freely", "J1, SW1 and D1 held on the south edge"),
        "Hold-to-blink board: free vs. hard board-edge constraints",
        "webp",
    ),
    (
        "showcase-hier-twin-bank.webp",
        "hier",
        ("hier-twin-bank-32",),
        (),
        "Twin-bank chaser: hierarchical place and route",
        "webp",
    ),
    (
        "showcase-chaser-line.gif",
        "compare",
        ("07-chaser-20", "line-chaser-20"),
        ("LEDs placed freely", "LEDs held in a line group"),
        "Five-stage chaser: free LEDs vs. a line group",
        "gif",
    ),
)
README_SHOWCASE = "showcase-chaser-line.gif"
COMPARE_SECONDS = 30.0
HIER_SECONDS = 34.0
# The hierarchical animation is over 2.5 MB even at the encoder's last step (640 px, quality 60,
# 80 ms frames: 2.69 MB): 31 s of three chapters, with block copper moving through the whole
# top-level placement. The design's showcase-only WebP budget (section 6.7) applies to it.
HIER_BUDGET_MB = 3.5
PACING = "showcase"


def repo_root():
    """The workspace (``bazel run`` sets BUILD_WORKSPACE_DIRECTORY), else this checkout."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    return Path(workspace) if workspace else HERE.parent.parent.parent


def run_showcases(args):
    """Run the showcase cases traced into a new run directory; returns it."""
    run = args.run_dir or (
        repo_root()
        / ".yapnr"
        / "ladder"
        / ("showcase-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    command = [args.python, str(HERE / "run.py"), "--repo", str(repo_root()), "--out", str(run)]
    command += ["--python", args.python, "--seed", "0", "--trace", "--showcases"]
    command += ["--trace-placement-every", EVERY, "--timeout", str(args.timeout)] + POOL
    command += list(getattr(args, "runner_arg", None) or [])
    for flag in ("kicad_python", "kicad_cli", "library"):
        value = getattr(args, flag)
        if value:
            command += ["--" + flag.replace("_", "-"), value]
    for case in CASES:
        command += ["--case", case]
    print("running:", " ".join(command[:4]), "...", flush=True)
    # run.py bounds each case by --timeout; this bounds the whole run (the cases, one at a
    # time, and the runner's own set-up).
    try:
        subprocess.run(command, check=False, timeout=args.timeout * (len(CASES) + 1))
    except subprocess.TimeoutExpired:
        print("the showcase run timed out; rendering what it finished", flush=True)
    return run


def load_cases(run):
    """``{case: (directory, result)}`` of a showcase run (seed 0)."""
    from pnr.provenance import Trace

    summary = json.loads((run / "summary.json").read_text())
    out = {}
    for result in summary.get("results", []):
        if result.get("seed") != 0 or result["case"] not in CASES:
            continue
        directory = Path(result["directory"])
        directory = directory if directory.is_absolute() else run / directory
        full = json.loads((directory / "result.json").read_text())
        trace = (
            Trace(directory / "trace") if (directory / "trace" / "header.json").is_file() else None
        )
        out[result["case"]] = (directory, full, trace)
    return out


def render(run, out, allow_failed=False, only=None):
    cases = load_cases(run)
    missing = [c for c in CASES if c not in cases or cases[c][2] is None]
    if missing:
        raise SystemExit("the showcase run has no trace for: " + ", ".join(missing))
    entries, failed = [], []
    for file, kind, names, labels, title, fmt in SHOWCASES:
        if only and file not in only:
            continue
        try:
            entry = render_one(cases, out / file, kind, names, labels, title, fmt, allow_failed)
        except (ValueError, SystemExit) as error:
            # One file over its budget (or a failed case) must not stop the others.
            print("%s: not rendered: %s" % (file, error), flush=True)
            failed.append(file)
            continue
        entry["config"] = config_of(names[0], run_options(run))
        print(
            "%s: %d frames, %.1f s, %d bytes"
            % (entry["file"], entry["frames"], entry["seconds"], entry["bytes"]),
            flush=True,
        )
        entries.append(entry)
    return cases, entries, failed


def config_of(case, extra=()):
    """The run flags that shaped a case's result (the pool's apply to the flat cases only),
    with ``extra``, the run's own options (:func:`animate_ladder.runner_options`)."""
    every = ["--trace-placement-every", EVERY]
    return (every if case in HIER_CASES else POOL + every) + list(extra)


def run_options(run):
    """The board-changing run.py options of a showcase run (its provenance)."""
    path = Path(run) / "provenance.json"
    return runner_options(json.loads(path.read_text())) if path.is_file() else []


def render_one(cases, path, kind, names, labels, title, fmt, allow_failed):
    """Render one showcase file; returns its manifest entry."""
    from pnr.animate.cli import render_animation
    from pnr.animate.compare import render_compare

    if kind == "compare":
        left, right = (cases[n][2] for n in names)
        return render_compare(
            left,
            right,
            fmt,
            path,
            width=960,
            max_seconds=COMPARE_SECONDS,
            title=title,
            labels=list(labels),
            pacing=PACING,
            allow_failed=allow_failed,
            replay_pool=path.name in REPLAY_POOL,
        )
    trace = cases[names[0]][2]
    entry = render_animation(
        trace,
        fmt,
        path,
        width=800,
        max_seconds=HIER_SECONDS,
        title=title,
        subtitle=trace.run.get("subject", {}).get("description"),
        allow_failed=allow_failed,
        pacing=PACING,
        budget_mb=HIER_BUDGET_MB,
    )
    return dict(
        kind="hier",
        cases=list(names),
        labels=[],
        title=entry["title"],
        file=entry["file"],
        format=entry["format"],
        bytes=entry["bytes"],
        sha256=entry["sha256"],
        width=entry["width"],
        height=entry["height"],
        frames=entry["frames"],
        seconds=entry["seconds"],
        trace_sha256={names[0]: entry["trace_sha256"]},
        results={names[0]: entry["result"]},
        settings=dict(entry["settings"], max_seconds=HIER_SECONDS, budget_mb=HIER_BUDGET_MB),
        pillow=entry["pillow"],
        captions=entry["captions"],
    )


def write_documents(out, run, cases, entries, image=None):
    """The ``showcases`` entries of manifest.json and ladder-results.json."""
    kicad_path = run / "kicad-version.txt"
    kicad = kicad_path.read_text().strip().splitlines()[0] if kicad_path.is_file() else None
    ladder = ladder_provenance(run, image=image, kicad=kicad)
    import PIL

    generated = dict(
        ladder=ladder,
        render=dict(
            date=datetime.date.today().isoformat(),
            pillow=PIL.__version__,
            platform=platform_name(),
        ),
    )
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    manifest.setdefault("schema", "yapnr-animations-v1")
    current = {e["file"]: e for e in manifest.get("showcases", [])}
    for entry in entries:
        current[entry["file"]] = entry
    manifest["showcases"] = [current[k] for k in sorted(current)]
    manifest["showcases_generated"] = generated
    manifest["readme_showcase"] = dict(
        file=README_SHOWCASE, cases=list(next(s[2] for s in SHOWCASES if s[0] == README_SHOWCASE))
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    results_path = out / "ladder-results.json"
    results = json.loads(results_path.read_text()) if results_path.is_file() else {}
    results.setdefault("schema", "yapnr-ladder-results-v1")
    rows = []
    for case in CASES:
        directory, result, _trace = cases[case]
        row = case_result(
            case, directory, result, config_of(case, run_options(run)), ladder.get("fab_profile")
        )
        row["title"] = SHOWCASE_TITLES[case]
        audit = result.get("constraint_audit")
        row["constraint_audit"] = (
            dict(checked=audit.get("checked", []), findings=len(audit.get("findings") or []))
            if audit
            else None
        )
        rows.append(row)
    results["showcases"] = rows
    if order_summary(rows):
        # Over every showcase: the neighbour order kept, weighted by relation count.
        results["showcases_order"] = order_summary(rows)
    results["showcases_generated"] = generated
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--render-only", type=Path, metavar="RUN_DIR", help="render a finished run")
    ap.add_argument("--python", default=sys.executable, help="numerical Python for run.py")
    ap.add_argument("--kicad-python")
    ap.add_argument("--kicad-cli")
    ap.add_argument("--library")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--run-dir", type=Path, help="showcase run output (a new directory)")
    ap.add_argument("--out", type=Path, help="animations directory (default docs/animations)")
    ap.add_argument("--only", action="append", help="render only this file (repeatable)")
    ap.add_argument("--allow-failed", action="store_true")
    ap.add_argument("--no-documents", action="store_true", help="do not touch the JSON files")
    ap.add_argument("--image", help="container image digest the run used (manifest)")
    ap.add_argument(
        "--runner-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="one more run.py argument (repeatable; e.g. --runner-arg=--compact)",
    )
    a = ap.parse_args(argv)
    out = a.out or repo_root() / "docs" / "animations"
    run = a.render_only.resolve() if a.render_only else run_showcases(a)
    out.mkdir(parents=True, exist_ok=True)
    if out.resolve() == run or run in out.resolve().parents:
        ap.error("refusing to write into the run directory")
    cases, entries, failed = render(run, out, a.allow_failed, a.only)
    if not a.no_documents and entries:
        write_documents(out, run, cases, entries, a.image)
    if failed:
        print("not rendered: " + ", ".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
