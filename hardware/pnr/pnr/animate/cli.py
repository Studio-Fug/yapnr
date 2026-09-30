"""The command line of :mod:`pnr.animate`: ``python -m pnr.animate SOURCE [...] --out PATH``.

SOURCE is a trace directory, a ladder case directory (its ``trace/``, or with ``--coarse``
what it saved), a ladder run directory (``--case NAME --seed N``, or every case when
``--out`` is a directory), a successive-halving run or a synthesis library (DAG only:
``--storyboard``). ``--out`` is a file (``.webp``, ``.gif``, ``.mp4``) or a directory
(``<case>.<ext>`` per ``--format``). The animator only reads its sources and refuses an
output path inside one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from pnr import provenance
from pnr.animate import encode, storyboard
from pnr.animate.render import Renderer, safe_text
from pnr.animate.timeline import Timeline
from pnr.provenance import Trace, coarse_ladder_trace

FORMATS = ("webp", "gif", "mp4")


def _is_within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def load(source, case=None, seed=None, coarse=False):
    """``[(label, Trace)]`` of a source directory."""
    source = Path(source)
    kind = provenance.detect(source)
    if kind == "trace":
        trace = Trace(source)
        return [(_label(trace, source.parent.name), trace)]
    if kind == "ladder-case":
        if (source / "trace" / "header.json").is_file():
            trace = Trace(source / "trace")
            return [(_label(trace, source.name), trace)]
        if not coarse:
            raise SystemExit(
                "%s has no trace; rerun the ladder with --trace, or pass --coarse" % source.name
            )
        trace = coarse_ladder_trace(source)
        return [(_label(trace, source.name), trace)]
    if kind == "ladder-run":
        summary = json.loads((source / "summary.json").read_text())
        out = []
        for result in summary.get("results", []):
            if case and result["case"] != case:
                continue
            if seed is not None and result["seed"] != seed:
                continue
            if seed is None and case is None and result["seed"] != 0:
                continue
            directory = Path(result["directory"])
            directory = directory if directory.is_absolute() else source / directory
            out += load(directory, coarse=coarse)
        if not out:
            raise SystemExit("no matching case in the ladder run")
        return out
    if kind in ("halving", "synthesis"):
        raise SystemExit(
            "%s runs have no board trace to animate here yet; use --storyboard for their "
            "provenance DAG" % kind
        )
    raise SystemExit("not an animation source: " + source.name)


def _label(trace, fallback):
    subject = trace.run.get("subject", {})
    return str(subject.get("case") or fallback)


def trace_digest(trace):
    """SHA-256 over a trace's content (not its location)."""
    digest = hashlib.sha256()
    if trace.root is not None:
        files = [trace.root / "header.json", trace.root / "run.json"]
        files += sorted((trace.root / "streams").glob("*.jsonl"))
        files += sorted((trace.root / "blobs").glob("*.json"))
        for path in files:
            if path.is_file():
                digest.update(path.relative_to(trace.root).as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    else:
        digest.update(json.dumps(trace.header, sort_keys=True).encode())
        for lane in sorted(trace.lanes):
            for event in trace.lanes[lane]:
                digest.update(json.dumps(event, sort_keys=True).encode())
    return digest.hexdigest()


def render_animation(
    trace,
    fmt,
    path,
    width=800,
    max_seconds=22.0,
    budget_mb=2.5,
    title=None,
    subtitle=None,
    allow_failed=False,
):
    """Render one output file; returns its manifest entry (without file-system paths)."""
    board = storyboard.build(trace, title=title, subtitle=subtitle)
    result = (trace.results[-1] if trace.results else {}) or {}
    if result and not result.get("passed", False) and not allow_failed:
        raise SystemExit("the traced run failed its gate; pass --allow-failed to animate it")
    subject = board["subject"]

    def make(w, frame_ms):
        renderer = Renderer(trace.header, subject, width=w)
        frames = Timeline(trace, board, frame_ms=frame_ms, max_seconds=max_seconds).frames
        return renderer, frames

    if fmt == "mp4":
        renderer, frames = make(width, 60)
        if not encode.mp4(renderer, frames, path):
            print("warning: ffmpeg not found; no MP4 written", file=sys.stderr)
            return None
        data = Path(path).read_bytes()
        settings = dict(frame_ms=60, width=width, crf=23)
    else:
        steps = [
            dict(s, width=min(s["width"], width))
            for s in (encode.WEBP_STEPS if fmt == "webp" else encode.GIF_STEPS)
        ]
        data, settings, frames, renderer = encode.encode(
            fmt, make, int(budget_mb * 1024 * 1024), steps
        )
        Path(path).write_bytes(data)
    import PIL

    return dict(
        case=subject.get("case"),
        title=subject.get("title"),
        seed=subject.get("seed"),
        config=subject.get("config", {}),
        file=Path(path).name,
        format=fmt,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=renderer.width,
        height=renderer.height,
        frames=len(frames),
        seconds=round(sum(ms for _v, ms in frames) / 1000.0, 2),
        trace_sha256=trace_digest(trace),
        coarse=bool(trace.coarse),
        result={
            k: result.get(k)
            for k in ("passed", "opens", "violations", "rules", "vias", "copper_length_mm")
        },
        settings=settings,
        pillow=PIL.__version__,
        captions=sorted(s for s in renderer.strings if s),
    )


def update_manifest(path, entries):
    path = Path(path)
    doc = json.loads(path.read_text()) if path.is_file() else dict(schema="yapnr-animations-v1")
    current = {(e.get("case"), e.get("file")): e for e in doc.get("animations", [])}
    for entry in entries:
        current[(entry.get("case"), entry.get("file"))] = entry
    doc["animations"] = [current[k] for k in sorted(current, key=lambda k: (str(k[0]), str(k[1])))]
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m pnr.animate", description=__doc__.split("\n")[0])
    ap.add_argument("sources", nargs="+", type=Path, metavar="SOURCE")
    ap.add_argument("--out", type=Path, help="a .webp/.gif/.mp4 file or a directory")
    ap.add_argument("--format", default="webp", help="comma list of webp, gif, mp4")
    ap.add_argument("--case")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--gif-width", type=int, default=640)
    ap.add_argument("--max-seconds", type=float, default=22.0)
    ap.add_argument("--budget-mb", type=float, default=2.5)
    ap.add_argument("--gif-budget-mb", type=float, default=5.0)
    ap.add_argument("--title")
    ap.add_argument("--subtitle")
    ap.add_argument("--storyboard", type=Path, help="write the storyboard (or DAG) JSON and stop")
    ap.add_argument("--manifest", type=Path, help="add or update entries in this manifest")
    ap.add_argument("--coarse", action="store_true", help="reconstruct untraced ladder cases")
    ap.add_argument("--allow-failed", action="store_true")
    a = ap.parse_args(argv)
    for text in (a.title, a.subtitle):
        if text:
            safe_text(text)
    formats = [f.strip() for f in a.format.split(",") if f.strip()]
    if any(f not in FORMATS for f in formats):
        ap.error("unknown format; choose from " + ", ".join(FORMATS))
    if a.storyboard:
        docs = []
        for source in a.sources:
            kind = provenance.detect(source)
            if kind == "halving":
                docs.append(provenance.from_halving(source).to_json())
            elif kind == "synthesis":
                docs.append(provenance.from_synthesis(source).to_json())
            else:
                for _label_, trace in load(source, a.case, a.seed, a.coarse):
                    docs.append(storyboard.build(trace, title=a.title, subtitle=a.subtitle))
        a.storyboard.write_text(json.dumps(docs[0] if len(docs) == 1 else docs, indent=1) + "\n")
        return 0
    if a.out is None:
        ap.error("--out is required (or --storyboard)")
    for source in a.sources:
        if _is_within(a.out, source):
            ap.error("refusing to write into a source directory")
    items = [item for source in a.sources for item in load(source, a.case, a.seed, a.coarse)]
    entries = []
    single = a.out.suffix.lstrip(".") in FORMATS
    if single and (len(items) > 1 or len(formats) > 1 and a.out.suffix.lstrip(".") != formats[0]):
        formats = [a.out.suffix.lstrip(".")]
    if single and len(items) > 1:
        ap.error("several sources need a directory --out")
    if not single:
        a.out.mkdir(parents=True, exist_ok=True)
    for label, trace in items:
        for fmt in [a.out.suffix.lstrip(".")] if single else formats:
            path = a.out if single else a.out / ("%s.%s" % (label, fmt))
            width = a.gif_width if fmt == "gif" else a.width
            budget = a.gif_budget_mb if fmt == "gif" else a.budget_mb
            entry = render_animation(
                trace,
                fmt,
                path,
                width=width,
                max_seconds=a.max_seconds,
                budget_mb=budget,
                title=a.title,
                subtitle=a.subtitle,
                allow_failed=a.allow_failed,
            )
            if entry:
                entries.append(entry)
                print(
                    "%s: %d frames, %.1f s, %d bytes"
                    % (entry["file"], entry["frames"], entry["seconds"], entry["bytes"])
                )
    if a.manifest:
        update_manifest(a.manifest, entries)
    return 0
