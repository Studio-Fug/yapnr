#!/usr/bin/env python3
"""Fresh circuit -> native PCB -> production P/R -> saved native DRC acceptance.

Missing tools, timeout, illegal placement, opens and *any* native DRC finding fail.
Never skips/x-fails difficult cases. Outputs persist in a new, refused-if-existing
run directory, including rejected boards, stage logs and source hashes.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from designs import designs, showcases

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KI = "/Applications/KiCad/KiCad.app/Contents"


def kicad_footprints():
    """Footprint library default (src15): PNR_KICAD_FOOTPRINTS, else the SharedSupport of the app bundle
    PNR_KICAD_CLI lives in (~/Applications/KiCad-headless.app via hier/env2.json), else the system KiCad.app.
    """
    if os.environ.get("PNR_KICAD_FOOTPRINTS"):
        return Path(os.environ["PNR_KICAD_FOOTPRINTS"])
    cli = Path(os.environ.get("PNR_KICAD_CLI") or KI + "/MacOS/kicad-cli")
    if cli.parent.name == "MacOS" and cli.parent.parent.name == "Contents":
        return cli.parent.parent / "SharedSupport/footprints"
    return Path(KI + "/SharedSupport/footprints")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _oriented(block):
    """A track or arc block with its ends in sorted order (a segment drawn either way is the
    same copper; which end a merged segment keeps can follow its random uuid)."""
    ends = [i for i, text in enumerate(block) if text.startswith(("(start ", "(end "))]
    if len(ends) != 2:
        return block
    i, j = ends

    def point(text):
        return tuple(float(v) for v in text.split("(", 1)[1].rstrip(")").split()[1:])

    if point(block[j]) < point(block[i]):
        block = list(block)
        block[i], block[j] = "(start" + block[j][4:], "(end" + block[i][6:]
    return block


COPPER_ITEM = re.compile(r"^\s*\((segment|via)[\s)]", re.M)  # tracks and vias, any layout


def copper_sha(board):
    """SHA-256 of a board's copper without uuids: its track, arc and via blocks, uuid lines
    dropped, ends in sorted order, sorted. Writeback gives tracks random uuids, so two runs of
    one case differ in ``sha`` but not here when their copper is the same (pairing A/B arms,
    determinism). Raises ValueError when the board holds a track or via block this reader
    does not parse (another file layout): a hash of nothing would pair any two boards."""
    text = Path(board).read_text()
    blocks, block, depth = [], None, 0
    for line in text.splitlines():
        item = line.strip()
        if block is None:
            if item.startswith(("(segment", "(arc", "(via")) and line.startswith("\t("):
                block, depth = [item], item.count("(") - item.count(")")
            continue
        depth += item.count("(") - item.count(")")
        if not item.startswith("(uuid"):
            block.append(item)
        if depth <= 0:
            blocks.append(" ".join(_oriented(block)))
            block = None
    parsed = sum(b.startswith(("(segment", "(via")) for b in blocks)
    found = len(COPPER_ITEM.findall(text))
    if parsed != found:
        raise ValueError(
            "copper_sha: %s has %d track and via blocks, %d parsed"
            % (Path(board).name, found, parsed)
        )
    return hashlib.sha256("\n".join(sorted(blocks)).encode()).hexdigest()


def acceptance(pnr, audit, drc):
    reasons = []
    if not pnr.get("legal"):
        reasons.append("illegal_placement")
    if not pnr.get("converged") or pnr.get("unrouted") or pnr.get("deferred"):
        reasons.append("incomplete_pnr")
    if audit.get("netlist_preserved") is not True:
        reasons.append("changed_pin_netlist")
    if audit.get("subwidth_tracks"):
        reasons.append("undersized_copper")
    if any(not r["qualified"] for r in audit.get("pad_entries", [])):
        reasons.append("unqualified_pad_entry")
    if not isinstance(drc.get("unconnected_items"), list) or not isinstance(
        drc.get("violations"), list
    ):
        reasons.append("invalid_drc_report")
    else:
        if drc["unconnected_items"]:
            reasons.append("native_unconnected_items")
        if drc["violations"]:
            reasons.append("native_drc_violations")
    return reasons


def constraint_reasons(spec, placed):
    """Independent audit (stdlib, not the engine's metrics) of the showcase constraints
    on ``placed`` (placed.json): each line group collinear at its pitch or gap, in member
    order, with its declared rotation, and each hard edge part within its tolerance.
    Returns ``(checked, findings)``."""
    cons = spec["constraints"]
    width, height = cons["board"]["outline"]["w"], cons["board"]["outline"]["h"]
    comps = {c["ref"]: c for c in placed["components"]}
    checked, findings = [], []

    def extent(comp, axis_angle):
        w, h = comp["courtyard"]
        quarter = int(round((comp["rot"] - axis_angle) / 90.0)) % 2
        return h if quarter else w

    for group in cons.get("line_group") or []:
        checked.append("line_group " + group["name"])
        parts = [comps[r] for r in group["members"]]
        dx = parts[1]["pos"][0] - parts[0]["pos"][0]
        dy = parts[1]["pos"][1] - parts[0]["pos"][1]
        turn = round(math.degrees(math.atan2(dy, dx)) / 90.0) * 90 % 360
        ux, uy = round(math.cos(math.radians(turn))), round(math.sin(math.radians(turn)))
        rot = (group.get("rot", 0) + turn) % 360
        for a, b in zip(parts, parts[1:]):
            if group.get("pitch_mm") is not None:
                step = group["pitch_mm"]
            else:
                gap = group.get("gap_mm", cons["board"].get("default_clearance_mm", 0.2))
                step = (extent(a, turn) + extent(b, turn)) / 2 + gap
            want = (a["pos"][0] + ux * step, a["pos"][1] + uy * step)
            if math.dist(want, b["pos"]) > 1e-3:
                findings.append(
                    "%s: %s not at the line step after %s" % (group["name"], b["ref"], a["ref"])
                )
        for c in parts:
            if abs((c["rot"] - rot + 180) % 360 - 180) > 1e-3 or c["side"] != parts[0]["side"]:
                findings.append(
                    "%s: %s turned %s, want %s" % (group["name"], c["ref"], c["rot"], rot)
                )
    for ref, rule in sorted((cons.get("edge_align") or {}).items()):
        if not rule.get("hard"):
            continue
        checked.append("edge_align " + ref)
        comp = comps[ref]
        w, h = comp["courtyard"]
        if int(round(comp["rot"] / 90.0)) % 2:
            w, h = h, w
        x, y = comp["pos"]
        distance = dict(
            south=y - h / 2, north=height - y - h / 2, west=x - w / 2, east=width - x - w / 2
        )[rule["edge"]]
        if distance > rule.get("tolerance_mm", 1.0) + 1e-3:
            findings.append("%s: %.3f mm from the %s edge" % (ref, distance, rule["edge"]))
    return checked, findings


# The vendor profile data pnr.fab_profile resolves data profiles from (yapnr.fab.capability), frozen
# with the engine so a run under oshpark-4l or jlc-4l uses the data of its own checkout.
FAB_DATA_SOURCES = ("yapnr/__init__.py", "yapnr/fab/__init__.py", "yapnr/fab/capability.py")


def fab_data_inputs(repo):
    return [repo / p for p in FAB_DATA_SOURCES] + sorted(
        (repo / "yapnr/fab/data/profiles").glob("*.json")
    )


def source_inputs(repo):
    scanner = repo / "hardware/tools/scan_via_proximity.py"
    if not scanner.is_file():
        raise FileNotFoundError("Native regression requires " + str(scanner))
    return (
        sorted((repo / "hardware/pnr/pnr").rglob("*.py"))
        + sorted((repo / "hardware/pnr/regression").glob("*.py"))
        + [scanner]
        + [p for p in fab_data_inputs(repo) if p.is_file()]
    )


# The fabrication profile the ladder routes and is judged under (pnr.fab_profile). The fixtures
# carry their own fab block (0.2 mm clearance, 0.6/0.3 mm vias; README), which is exactly what the
# legacy profile enforces; the engine's default (jlc-pofv) overrides it with JLC capability values.
# The vendor profiles of yapnr/fab/data (oshpark-2l, oshpark-4l, jlc-4l, ...) route and judge a
# case under that vendor's rules (docs/fab-and-ordering.md).
FAB_PROFILES = ("legacy", "jlc-pofv")
DEFAULT_FAB_PROFILE = "legacy"


def fab_profiles(repo=REPO):
    """Every profile ``--fab-profile`` accepts: the built-in ones and the data profiles."""
    data = sorted(p.stem for p in (repo / "yapnr/fab/data/profiles").glob("*.json"))
    return tuple(sorted(set(FAB_PROFILES) | set(data)))


def engine_revision(repo):
    """``(commit, dirty)`` of the checkout the sources are frozen from; ``(None, None)`` without git.

    A source bundle of ``yapnr exp`` (a ``git archive``, no ``.git``) gets its commit from the task
    wrapper's ``YAPNR_ENGINE_REVISION`` and ``YAPNR_ENGINE_DIRTY``, which win over git: the
    bundle's work directory may sit inside an unrelated checkout.
    """
    if os.environ.get("YAPNR_ENGINE_REVISION"):
        return os.environ["YAPNR_ENGINE_REVISION"], os.environ.get("YAPNR_ENGINE_DIRTY") == "1"

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60, check=True
        ).stdout

    try:
        commit = git("rev-parse", "HEAD").strip()
        dirty = bool(git("status", "--porcelain", "--", "hardware/pnr", "hardware/tools").strip())
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def sources_digest(manifest):
    """One SHA-256 over the frozen sources (path and content hash): the engine, rebase-proof."""
    digest = hashlib.sha256()
    for path in sorted(manifest):
        digest.update((path + "\0" + manifest[path] + "\n").encode())
    return digest.hexdigest()


# Lists the installed distributions without pip (a uv-built venv, as in the image, has none).
LISTING = (
    "import importlib.metadata as m;"
    "print('\\n'.join(sorted('%s==%s'%(d.metadata['Name'],d.version) for d in m.distributions())))"
)


# --- PNR_GLOSS ladder stage (opt-in, docs/design/gloss.md "Ladder stage") ---------------------
# The ladder has no native loop, so --gloss runs one gloss pass with 07g semantics (no open-net
# guard) on the refilled board, before the audit: transactional and gated inside pnr.gloss, then
# gated again here by the cold kicad-cli DRC that judges the case.
GLOSS_SUMMARY_KEYS = (
    "status",
    "accepted_transactions",
    "proposed_transactions",
    "rejected_transactions",
    "split_transactions",
    "edits_by_step",
    "rejections_by_check",
    "end_gate",
    "stop",  # why the pass stopped early (time_budget, max_transactions) or null
    "budget",
    "seconds",
    "wall_seconds",
    "objective_before",
    "objective_after",
    "metrics_before",
    "metrics_after",
    "delta_by_step",
    "cross_group",
)


def gloss_flags(items, repo):
    """``--gloss-flag KEY=VALUE`` items -> {KEY: VALUE}, PNR_GLOSS_* sub-flags only (the runner
    strips ambient PNR_* variables). A relative PNR_GLOSS_CLASSES file is taken from the repo."""
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.startswith("PNR_GLOSS_"):
            raise ValueError("--gloss-flag takes PNR_GLOSS_<NAME>=VALUE, got %r" % item)
        if key == "PNR_GLOSS_CLASSES" and value and not Path(value).is_absolute():
            value = str((Path(repo) / value).resolve())
        out[key] = value
    return out


def drc_counts(report):
    """(opens, {violation type: count}) of a kicad-cli DRC report."""
    return len(report["unconnected_items"]), dict(Counter(v["type"] for v in report["violations"]))


DANGLING = ("track_dangling", "via_dangling")


def violation_keys(report):
    """As pnr.via_coalesce.violation_keys (the pass's own gate): each violation as its type and
    the sorted uuids of its items, the dangling kinds aside (they are counted)."""
    return Counter(
        (v["type"], tuple(sorted(i["uuid"] for i in v.get("items", []))))
        for v in report["violations"]
        if v["type"] not in DANGLING
    )


def gloss_gate(before, after):
    """The outer gate of the gloss stage, as strict as the pass's own transaction gate: [] when
    the cold DRC did not get worse, else the reasons. Worse: more opens, any violation that is
    new by its type and items (a violation that moved to other items is new, even when the
    count of its type holds), or more dangling tracks or vias."""
    (o0, v0), (o1, v1) = drc_counts(before), drc_counts(after)
    reasons = ["opens %d -> %d" % (o0, o1)] if o1 > o0 else []
    new = violation_keys(after) - violation_keys(before)
    for kind in sorted({kind for kind, _ in new}):
        reasons.append("new %s %d" % (kind, sum(n for (k, _), n in new.items() if k == kind)))
    for kind in DANGLING:
        if v1.get(kind, 0) > v0.get(kind, 0):
            reasons.append("%s %d -> %d" % (kind, v0.get(kind, 0), v1[kind]))
    return reasons


def freeze_gloss_groups(flags, freeze):
    """A PNR_GLOSS_CLASSES groups file is copied into the run's source freeze and the run reads
    the copy, as it reads every engine source: (flags naming the copy, the file's record
    {name, sha256} for provenance.json; None without a groups file)."""
    path = flags.get("PNR_GLOSS_CLASSES")
    if not path:
        return flags, None
    if not Path(path).is_file():
        raise ValueError("--gloss-flag PNR_GLOSS_CLASSES: no such file %r" % Path(path).name)
    target = freeze / "gloss-groups" / Path(path).name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return dict(flags, PNR_GLOSS_CLASSES=str(target)), gloss_groups_record(flags)


def gloss_groups_record(flags):
    """The groups file of the sub-flags by its name and content, never its path (or None)."""
    path = flags.get("PNR_GLOSS_CLASSES")
    if not path or not Path(path).is_file():
        return None
    return dict(name=Path(path).name, sha256=sha(path))


def gloss_flags_record(flags):
    """The sub-flags as result.json records them: a groups file by its name, never its path."""
    return {
        key: Path(value).name if key == "PNR_GLOSS_CLASSES" and value else value
        for key, value in flags.items()
    }


def gloss_summary(report):
    """The pass summary kept in result.json: no transactions, specs or paths. ``planning``
    keeps its totals (inventories, wall-clock safety-net hits), not its rows."""
    block = {k: report[k] for k in GLOSS_SUMMARY_KEYS if k in report}
    if isinstance(report.get("planning"), dict):
        block["planning"] = {k: v for k, v in report["planning"].items() if k != "rows"}
    if (block.get("cross_group") or {}).get("groups"):
        block["cross_group"] = dict(
            block["cross_group"], groups=Path(block["cross_group"]["groups"]).name
        )
    return block


def gloss_stage(root, board, args, run, flags):
    """--gloss: the PNR_GLOSS pass on ``board`` (in place); returns the case's gloss block.

    ``routed.pre-gloss.kicad_pcb`` keeps the input. The pass output replaces the board only when
    it kept edits and the cold kicad-cli DRC of the replaced board is not worse (gloss_gate);
    otherwise, and on any stage error, the input is restored."""
    folder = root / "gloss"
    folder.mkdir()
    pre = root / "routed.pre-gloss.kicad_pcb"
    shutil.copyfile(board, pre)
    pre_sha = sha(board)

    def cold_drc(name):
        out = folder / (name + ".drc.json")
        run(
            "gloss-" + name,
            [args.kicad_cli, "pcb", "drc", board, "--format", "json", "--output", out],
        )
        return json.loads(out.read_text())

    block = dict(
        pre_gloss_board_sha256=pre_sha,
        pre_gloss_copper_sha256=copper_sha(board),
        flags=gloss_flags_record(flags),
        groups=gloss_groups_record(flags),
        kept=False,
    )
    try:
        before = cold_drc("drc-before")
        candidate = folder / "candidate.kicad_pcb"
        run(
            "gloss",
            [
                args.python,
                "-m",
                "pnr.gloss",
                board,
                "--rules",
                root / "rules.json",
                "--out",
                candidate,
                "--work-dir",
                folder / "work",
                "--report",
                folder / "result.json",
                "--kicad-cli",
                args.kicad_cli,
                "--kicad-python",
                args.kicad_python,
                "--label",
                "07g-gloss",
                "--metrics",
            ],
            dict(flags, PNR_GLOSS="1"),
        )
        report = json.loads((folder / "result.json").read_text())
        block.update(summary=gloss_summary(report))
        reasons, after = [], before
        if report.get("accepted_transactions") and sha(candidate) != pre_sha:
            shutil.copyfile(candidate, board)
            after = cold_drc("drc-after")
            reasons = gloss_gate(before, after)
            block["kept"] = not reasons
        block["outer_gate"] = dict(
            passed=not reasons,
            reasons=reasons,
            before=dict(zip(("opens", "violations"), drc_counts(before))),
            after=dict(zip(("opens", "violations"), drc_counts(after))),
        )
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as ex:
        # no paths in result.json: the error's kind and exit code; the stage logs have the rest
        code = getattr(ex, "returncode", None)
        block.update(
            status="error", error=type(ex).__name__ + ("" if code is None else " %s" % code)
        )
    if not block["kept"]:
        shutil.copyfile(pre, board)
    block["board_sha256"] = sha(board)
    block["copper_sha256"] = copper_sha(board)
    return block


def measure_summary(row):
    """--gloss-measure: the A/B figures of one pnr.gloss --measure row."""
    m = row["metrics"]
    eligible = m["classes"].get("eligible") or {}
    cross = m.get("cross_group") or {}
    return dict(
        objective=row["objective"],
        drc=row["drc"],
        audit=row["audit"],
        pad_entry=row["pad_entry"],
        length_mm=round(eligible.get("length_mm", 0.0), 3),
        segments=eligible.get("segments", 0),
        bends_all=eligible.get("bends_all", 0),
        X_mm2=round(m.get("X_mm2", 0.0), 3),
        T_mm=round(m.get("T_mm", 0.0), 3),
        DS_mm2=round(m.get("DS_mm2", 0.0), 3),
        A3_mm2=round(m.get("A3_mm2", 0.0), 3),
        cross_group_max_mm=cross.get("max_mm"),
        cross_group_max_pair=cross.get("max_pair"),
        cross_group_over_cap=len(cross.get("over_cap") or []),
        seconds=row.get("seconds"),
    )


def gloss_measure(root, board, args, run, flags):
    """--gloss-measure: pnr.gloss --measure on a copy of the final board (both A/B arms)."""
    folder = root / "gloss-measure"
    folder.mkdir()
    copy = folder / board.name
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        if board.with_suffix(ext).exists():
            shutil.copyfile(board.with_suffix(ext), copy.with_suffix(ext))
    if (root / "fp-lib-table").exists():
        shutil.copyfile(root / "fp-lib-table", folder / "fp-lib-table")
    cmd = [
        args.python,
        "-m",
        "pnr.gloss",
        "--measure",
        copy,
        "--rules",
        root / "rules.json",
        "--out",
        folder / "measure.json",
        "--kicad-cli",
        args.kicad_cli,
        "--kicad-python",
        args.kicad_python,
    ]
    if flags.get("PNR_GLOSS_CLASSES"):
        cmd += ["--classes", flags["PNR_GLOSS_CLASSES"]]
    if flags.get("PNR_GLOSS_CLASSES_FROM"):
        cmd += ["--classes-from", flags["PNR_GLOSS_CLASSES_FROM"]]
    if flags.get("PNR_GLOSS_CROSS_GROUP_MM"):
        cmd += ["--cross-group-mm", flags["PNR_GLOSS_CROSS_GROUP_MM"]]
    run("gloss-measure", cmd)
    rows = json.loads((folder / "measure.json").read_text())
    return measure_summary(next(iter(rows.values())))


def new_result(spec, seed, root):
    """A case's result record; ``directory`` is relative to the run directory."""
    return dict(
        case=spec["name"],
        seed=seed,
        components=spec["expected_components"],
        passed=False,
        stages={},
        directory=root.name,
    )


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo", type=Path, default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", REPO))
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--seed", type=int, action="append")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--python")
    ap.add_argument("--dense-maze-cost", action="store_true")
    ap.add_argument(
        "--detail-pitch-mm",
        type=float,
        help="Explicit signal grid pitch for source and fixed-copper handoff; 0 keeps automatic pitch",
    )
    ap.add_argument(
        "--packed-maze", action="store_true", help="Validate the packed CPU maze kernel explicitly"
    )
    ap.add_argument(
        "--batched-wirelength",
        action="store_true",
        help="Validate CPU degree-bucket placement costs explicitly",
    )
    ap.add_argument(
        "--initial-pool",
        action="store_true",
        help="Compare a bounded set of legal global placements before round one",
    )
    ap.add_argument("--initial-starts", type=int, default=8)
    ap.add_argument("--initial-finalists", type=int, default=3)
    ap.add_argument(
        "--trace",
        action="store_true",
        help="Record a pnr-trace-v1 trace per case (CASE/trace) for pnr.animate; observational only",
    )
    ap.add_argument(
        "--trace-placement-every",
        type=int,
        help="With --trace: a global placement snapshot every N iterations (PNR_TRACE_PLACEMENT_EVERY)",
    )
    ap.add_argument(
        "--showcases",
        action="store_true",
        help="Also offer the showcase cases (designs.showcases(), outside the ladder) to --case",
    )
    ap.add_argument(
        "--gloss",
        action="store_true",
        help=(
            "Run the opt-in PNR_GLOSS pass (dekink, pull-tight, corridor packing; 07g semantics) "
            "after refill, before the audit; a cold-DRC gate restores the pre-gloss board if "
            "opens or findings rise"
        ),
    )
    ap.add_argument(
        "--gloss-flag",
        action="append",
        default=[],
        metavar="PNR_GLOSS_NAME=VALUE",
        help="A PNR_GLOSS_* sub-flag for the gloss stage and --gloss-measure (repeatable)",
    )
    ap.add_argument(
        "--gloss-measure",
        action="store_true",
        help=(
            "pnr.gloss --measure on a copy of each final board (objective, length, bends, "
            "adjacency, dead space): the figures of a gloss A/B, for both arms"
        ),
    )
    ap.add_argument(
        "--fab-profile",
        choices=fab_profiles(),
        default=DEFAULT_FAB_PROFILE,
        help=(
            "PNR_FAB_PROFILE for every stage (default legacy: the fixtures' own fab block); "
            "jlc-pofv routes and judges under the engine's default profile, a vendor profile "
            "(oshpark-4l, jlc-4l, ...) under that vendor's rules"
        ),
    )
    ap.add_argument(
        "--kicad-python",
        default=os.environ.get(
            "PNR_KICAD_PYTHON", KI + "/Frameworks/Python.framework/Versions/3.9/bin/python3"
        ),
    )  # PNR_KICAD_PYTHON: headless bundle (src15)
    ap.add_argument(
        "--kicad-cli", default=os.environ.get("PNR_KICAD_CLI", KI + "/MacOS/kicad-cli")
    )  # PNR_KICAD_CLI: headless bundle (src15)
    ap.add_argument(
        "--library", type=Path, default=kicad_footprints()
    )  # PNR_KICAD_FOOTPRINTS / PNR_KICAD_CLI bundle (src15)
    return ap


def main():
    global REPO
    args = parser().parse_args()
    REPO = args.repo.resolve()
    args.python = args.python or str(REPO / "output/pnr-regression-runtime/bin/python")
    out = args.out.resolve()
    if args.trace_placement_every is not None and (
        not args.trace or args.trace_placement_every < 1
    ):
        raise SystemExit("--trace-placement-every needs --trace and a positive N")
    try:
        glossing = gloss_flags(args.gloss_flag, REPO)
    except ValueError as error:
        raise SystemExit(str(error))
    if glossing and not (args.gloss or args.gloss_measure):
        raise SystemExit("--gloss-flag needs --gloss or --gloss-measure")
    out.mkdir(parents=True, exist_ok=False)
    allcases = designs() + (showcases() if args.showcases else [])
    cases = [c for c in allcases if not args.case or c["name"] in args.case]
    if not cases or (set(args.case) - {c["name"] for c in cases}):
        raise SystemExit("Unknown/empty case selection")
    source_files = source_inputs(REPO)
    manifest = {str(p.relative_to(REPO)): sha(p) for p in source_files}
    freeze = out / "source-freeze"
    for source_path in source_files:
        target = freeze / source_path.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
    frozen_here = freeze / "hardware/pnr/regression"
    try:
        glossing, gloss_groups = freeze_gloss_groups(glossing, freeze)
    except ValueError as error:
        raise SystemExit(str(error))
    # The frozen root carries yapnr.fab.capability and its profile data (fab_data_inputs).
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(freeze / "hardware/pnr"), str(freeze)]),
        PNR_LOCAL_PRESSURE="1",
    )
    # Ambient experiment switches must not silently change the suite configuration.
    for key in list(env):
        if key.startswith("PNR_") and key not in ("PNR_LOCAL_PRESSURE", "PNR_JOINT_ACCESS"):
            del env[key]
    if args.packed_maze:
        env["PNR_PACKED_MAZE"] = "1"
    if args.dense_maze_cost:
        env["PNR_DENSE_MAZE_COST"] = "1"
    if args.detail_pitch_mm is not None:
        import math

        if not math.isfinite(args.detail_pitch_mm) or args.detail_pitch_mm < 0:
            raise ValueError("Detail pitch must be finite and nonnegative")
        env["PNR_DETAIL_PITCH_MM"] = str(args.detail_pitch_mm)
    if args.batched_wirelength:
        env["PNR_BATCHED_WIRELENGTH"] = "1"
    env["PNR_FAB_PROFILE"] = (
        args.fab_profile
    )  # routed and judged under one profile (route_case.py, writeback)
    if args.initial_pool:
        if not 2 <= args.initial_starts <= 128 or not 1 <= args.initial_finalists <= min(
            args.initial_starts, 16
        ):
            raise ValueError("Invalid initial placement pool size/finalist budget")
        env.update(
            PNR_INITIAL_POOL="1",
            PNR_INITIAL_STARTS=str(args.initial_starts),
            PNR_INITIAL_FINALISTS=str(args.initial_finalists),
            PNR_INITIAL_PROXY_BUDGET=str(args.initial_starts),
        )
    commit, dirty = engine_revision(REPO)
    provenance = dict(
        schema="pnr-regression-v1",
        source_hashes=manifest,
        sources_sha256=sources_digest(manifest),
        engine_revision=commit,
        engine_dirty=dirty,
        fab_profile=args.fab_profile,
        platform="%s-%s" % (sys.platform, platform.machine().lower()),
        seeds=args.seed or [0],
        trace=bool(args.trace),
        gloss=dict(
            enabled=bool(args.gloss),
            measure=bool(args.gloss_measure),
            flags=gloss_flags_record(glossing),
            groups=gloss_groups,
        ),
        arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        pnr_environment={k: v for k, v in env.items() if k.startswith("PNR_")},
    )
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2))
    for key, cmd in [
        ("python", [args.python, "-c", LISTING]),
        ("kicad", [args.kicad_cli, "version"]),
    ]:
        (out / (key + "-version.txt")).write_text(
            subprocess.check_output(cmd, text=True, timeout=300)
        )  # a version query
    tracing = None
    if args.trace:
        sys.path[:0] = [
            str(frozen_here),
            str(freeze / "hardware/pnr"),
        ]  # frozen, stdlib-only trace modules
        import trace_native as tracing
    results = []

    def stage(root, name, cmd, extra=None, cpu=None):
        """Run one stage; its wall seconds are returned and, with ``cpu``, its CPU
        seconds (user + system of the stage's whole waited-for process tree) recorded."""
        t = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            with (root / (name + ".log")).open("w") as log:
                subprocess.run(
                    list(map(str, cmd)),
                    cwd=REPO,
                    env=dict(env, **(extra or {})),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=args.timeout,
                )
        finally:
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            if cpu is not None:
                cpu[name] = round(
                    after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime, 3
                )
        return time.monotonic() - t

    for spec in cases:
        for seed in args.seed or [0]:
            root = out / (spec["name"] + "-seed-" + str(seed))
            root.mkdir()
            (root / "design.json").write_text(json.dumps(spec, indent=2))
            result = new_result(spec, seed, root)
            t = time.monotonic()
            print("START " + root.name, flush=True)
            try:

                result["cpu_stages"] = {}

                def run(name, cmd, extra=None):
                    result["stages"][name] = stage(root, name, cmd, extra, result["cpu_stages"])

                native = tracing.NativeTrace(root, spec, seed, args, out.name) if tracing else None
                run(
                    "generate",
                    [
                        args.kicad_python,
                        frozen_here / "native.py",
                        "make",
                        root,
                        "--library",
                        args.library,
                    ],
                )
                source = sha(root / "source.kicad_pcb")
                graph = json.loads((root / "source-graph.json").read_text())
                assert len(graph["components"]) == spec["expected_components"]
                assert (
                    sum(bool(p["net"]) for c in graph["components"] for p in c["pads"])
                    == spec["expected_connected_pads"]
                )
                # A design's driver: flat (route_case.py) or hierarchical (hier_case.py).
                driver = "hier_case.py" if spec.get("driver") == "hier" else "route_case.py"
                run(
                    "place-route",
                    [args.python, frozen_here / driver, root, seed, args.rounds],
                    native.environment() if native else None,
                )
                board = root / "routed.kicad_pcb"
                run(
                    "writeback",
                    [
                        args.kicad_python,
                        "-m",
                        "pnr.writeback",
                        root / "source.kicad_pcb",
                        root / "placed.json",
                        "--out",
                        board,
                        "--rules",
                        root / "rules.json",
                        "--routes",
                        root / "routes.json",
                    ],
                )
                if native:
                    native.snapshot(
                        "writeback", board, args.kicad_cli, args.timeout
                    )  # a copy with its own DRC
                run(
                    "planes",
                    [args.kicad_python, "-m", "pnr.planes", board, "--rules", root / "rules.json"],
                )
                if native:
                    native.snapshot("planes", board, args.kicad_cli, args.timeout)
                run(
                    "refill",
                    [
                        args.kicad_python,
                        "-m",
                        "pnr.planes",
                        board,
                        "--rules",
                        root / "rules.json",
                        "--refill-only",
                    ],
                )
                if args.gloss:
                    result["gloss"] = gloss_stage(root, board, args, run, glossing)
                    if result["gloss"].get("status") == "error":
                        result["gloss_error"] = result["gloss"]["error"]
                    if native:
                        native.snapshot("gloss", board, args.kicad_cli, args.timeout)
                run(
                    "audit",
                    [args.kicad_python, frozen_here / "native.py", "audit", root, "--pcb", board],
                )
                run(
                    "drc",
                    [
                        args.kicad_cli,
                        "pcb",
                        "drc",
                        board,
                        "--format",
                        "json",
                        "--output",
                        root / "drc.json",
                    ],
                )
                run(
                    "via-scan",
                    [
                        args.kicad_python,
                        freeze / "hardware/tools/scan_via_proximity.py",
                        board,
                        "--radius-mm",
                        "5",
                        "--out-dir",
                        root / "via-scan",
                    ],
                )
                pnr = json.loads((root / "pnr-report.json").read_text())
                audit = json.loads((root / "native-audit.json").read_text())
                drc = json.loads((root / "drc.json").read_text())
                result.update(
                    reasons=acceptance(pnr, audit, drc),
                    opens=len(drc["unconnected_items"]),
                    violations=dict(Counter(x["type"] for x in drc["violations"])),
                    tracks=audit["tracks"],
                    vias=audit["vias"],
                    copper_length_mm=audit["copper_length_mm"],
                    pnr=pnr,
                    source_board_sha256=source,
                    board_sha256=sha(board),
                    copper_sha256=copper_sha(board),
                    project_sha256=sha(board.with_suffix(".kicad_pro")),
                )
                if source != sha(root / "source.kicad_pcb"):
                    result["reasons"].append("source_changed")
                if result.get("gloss_error"):
                    result["reasons"].append("gloss_error")
                if args.gloss_measure:
                    result["gloss_measure"] = gloss_measure(root, board, args, run, glossing)
                constraints = spec["constraints"]
                if constraints.get("line_group") or any(
                    rule.get("hard") for rule in (constraints.get("edge_align") or {}).values()
                ):
                    checked, findings = constraint_reasons(
                        spec, json.loads((root / "placed.json").read_text())
                    )
                    result["constraint_audit"] = dict(checked=checked, findings=findings)
                    if findings:
                        result["reasons"].append("constraint_violated")
                result["passed"] = not result["reasons"]
                if native:
                    native.finish(board, drc, result)  # the saved board with the runner's final DRC
            except Exception as ex:
                result.update(
                    error=str(ex), traceback=traceback.format_exc(), reasons=["stage_failure"]
                )
            result["elapsed_seconds"] = time.monotonic() - t
            result["cpu_seconds"] = round(sum((result.get("cpu_stages") or {}).values()), 3)
            (root / "result.json").write_text(json.dumps(result, indent=2))
            results.append(result)
            (out / "summary.json").write_text(
                json.dumps(
                    dict(passed=all(r["passed"] for r in results), complete=False, results=results),
                    indent=2,
                )
            )
            print(
                ("PASS " if result["passed"] else "FAIL ")
                + root.name
                + " "
                + str(result.get("reasons")),
                flush=True,
            )
    changed = [p for p, digest in manifest.items() if sha(freeze / p) != digest]
    summary = dict(
        passed=all(r["passed"] for r in results) and not changed,
        complete=True,
        source_changed_during_run=changed,
        results=results,
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    suite = ET.Element(
        "testsuite",
        name="native-pnr-ladder",
        tests=str(len(results)),
        failures=str(sum(not r["passed"] for r in results)),
    )
    for r in results:
        test = ET.SubElement(
            suite,
            "testcase",
            name=r["case"] + "-seed-" + str(r["seed"]),
            time=str(r["elapsed_seconds"]),
        )
        if not r["passed"]:
            ET.SubElement(test, "failure", message=", ".join(r["reasons"])).text = json.dumps(
                r, indent=2
            )
    if changed:
        ET.SubElement(suite, "error", message="frozen_source_changed").text = json.dumps(changed)
    ET.ElementTree(suite).write(out / "junit.xml", encoding="utf-8", xml_declaration=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
