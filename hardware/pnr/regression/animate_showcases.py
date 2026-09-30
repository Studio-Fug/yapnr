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
pool, seed 0). The case titles live here, next to the ladder's (``animate_ladder.TITLES``).
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

from animate_ladder import case_result, ladder_provenance, platform_name  # noqa: E402

POOL = ["--initial-pool", "--initial-starts", "8", "--initial-finalists", "3"]
EVERY = "5"
CASES = ("07-chaser-20", "line-chaser-20", "edge-io-12-free", "edge-io-12", "hier-twin-bank-32")
SHOWCASE_TITLES = {
    "07-chaser-20": "Five-stage chaser",
    "line-chaser-20": "Five-stage chaser, LEDs in a line group",
    "edge-io-12-free": "Hold-to-blink board, parts placed freely",
    "edge-io-12": "Hold-to-blink board, I/O on the south edge",
    "hier-twin-bank-32": "Twin-bank chaser, placed and routed hierarchically",
}
# (file, kind, cases, labels, title, format, README?)
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
        "showcase-chaser-line.gif",
        "compare",
        ("07-chaser-20", "line-chaser-20"),
        ("LEDs placed freely", "LEDs held in a line group"),
        "Five-stage chaser: free LEDs vs. a line group",
        "gif",
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
    for flag in ("kicad_python", "kicad_cli", "library"):
        value = getattr(args, flag)
        if value:
            command += ["--" + flag.replace("_", "-"), value]
    for case in CASES:
        command += ["--case", case]
    print("running:", " ".join(command[:4]), "...", flush=True)
    subprocess.run(command, check=False)
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
    from pnr.animate.cli import render_animation
    from pnr.animate.compare import render_compare

    cases = load_cases(run)
    missing = [c for c in CASES if c not in cases or cases[c][2] is None]
    if missing:
        raise SystemExit("the showcase run has no trace for: " + ", ".join(missing))
    entries = []
    for file, kind, names, labels, title, fmt in SHOWCASES:
        if only and file not in only:
            continue
        path = out / file
        if kind == "compare":
            left, right = (cases[n][2] for n in names)
            entry = render_compare(
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
            )
        else:
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
            entry = dict(
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
                settings=dict(
                    entry["settings"], max_seconds=HIER_SECONDS, budget_mb=HIER_BUDGET_MB
                ),
                pillow=entry["pillow"],
                captions=entry["captions"],
            )
        entry["config"] = POOL + ["--trace-placement-every", EVERY]
        print(
            "%s: %d frames, %.1f s, %d bytes"
            % (entry["file"], entry["frames"], entry["seconds"], entry["bytes"]),
            flush=True,
        )
        entries.append(entry)
    return cases, entries


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
            case,
            directory,
            result,
            POOL + ["--trace-placement-every", EVERY],
            ladder.get("fab_profile"),
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
    a = ap.parse_args(argv)
    out = a.out or repo_root() / "docs" / "animations"
    run = a.render_only.resolve() if a.render_only else run_showcases(a)
    out.mkdir(parents=True, exist_ok=True)
    if out.resolve() == run or run in out.resolve().parents:
        ap.error("refusing to write into the run directory")
    cases, entries = render(run, out, a.allow_failed, a.only)
    if not a.no_documents:
        write_documents(out, run, cases, entries, a.image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
