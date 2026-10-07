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
from hard_rungs import dru_text, hard_rungs

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

# ``pnr.live`` (hardware/pnr/pnr/live.py) is telemetry only: ``emit`` is a no-op whenever
# PNR_LIVE_DIR is unset, so importing it unconditionally here costs nothing for the common,
# non-live run and changes no byte of run.py's own results. HERE.parent is hardware/pnr, which
# holds the `pnr` package this script otherwise never needs on sys.path (it imports `designs`/
# `hard_rungs` from its own directory instead).
sys.path.insert(0, str(HERE.parent))
from pnr.live import emit as live_emit  # noqa: E402

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


# A KiCad unconnected item's pad: "Pad K4 [VCC] of U1 on F.Cu".
PAD_ITEM = re.compile(r"^Pad (\S+) \[[^\]]*\] of (\S+) on ")


def designed_open(item, pads):
    """Whether KiCad's unconnected ``item`` names one of ``pads`` ("REF.PAD"): a rung's
    designed opens (``designed_open``), which its ``unconnected`` check holds exact."""
    for end in item.get("items") or []:
        m = PAD_ITEM.match(end.get("description") or "")
        if m and "%s.%s" % (m.group(2), m.group(1)) in pads:
            return True
    return False


def acceptance(pnr, audit, drc, designed=()):
    """The reasons a routed case fails. ``designed`` ("REF.PAD", a spec's
    ``designed_open``): KiCad's unconnected items at those pads are the design's own
    (the partial-fanout rung leaves one ball open by design) and are not reasons; the
    spec's ``unconnected`` check judges that exactly those pads are cut off."""
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
        pads = set(designed)
        if [u for u in drc["unconnected_items"] if not (pads and designed_open(u, pads))]:
            reasons.append("native_unconnected_items")
        if drc["violations"]:
            reasons.append("native_drc_violations")
    return reasons


def _quarter(x, y, rot):
    """``(x, y)`` turned ``rot`` degrees CCW (a quarter turn, exactly)."""
    return ((x, y), (-y, x), (-x, -y), (y, -x))[int(round(rot / 90.0)) % 4]


def body_extent(comp, use_body=True):
    """``(x0, y0, x1, y1)`` of a placed.json component's box at its pose: its off-centre
    ``body`` (``use_body``, when it has one), else the ``courtyard`` centred on ``pos``."""
    x, y = comp["pos"]
    body = comp.get("body") if use_body else None
    if not body:
        w, h = comp["courtyard"]
        body = (-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)
    corners = [
        _quarter(px, py, comp["rot"]) for px in (body[0], body[2]) for py in (body[1], body[3])
    ]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return (x + min(xs), y + min(ys), x + max(xs), y + max(ys))


def compactness(placed):
    """Stdlib measure of a placed.json (every arm alike, the engine's
    pnr.place.compact.metrics): the bounding box of the parts' body boxes, their summed
    area, utilization (area / bbox), occupancy (area / outline) and the outline area."""
    boxes = [body_extent(c) for c in placed["components"]]
    outline = placed.get("outline") or {}
    board = float(outline.get("width") or 0.0) * float(outline.get("height") or 0.0)
    if not boxes:
        return None
    bw = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    bh = max(b[3] for b in boxes) - min(b[1] for b in boxes)
    area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    return dict(
        bbox_mm2=round(bw * bh, 3),
        bbox_mm=[round(bw, 3), round(bh, 3)],
        area_mm2=round(area, 3),
        utilization=round(area / (bw * bh), 3) if bw * bh > 0 else 0.0,
        occupancy=round(area / board, 3) if board > 0 else None,
        outline_mm2=round(board, 3),
    )


def constraint_reasons(spec, placed, use_body=False, shrunk=False):
    """Independent audit (stdlib, not the engine's metrics) of the showcase constraints
    on ``placed`` (placed.json): each line group collinear at its pitch or gap, in member
    order, with its declared rotation, and each hard edge part within its tolerance of
    the design's outline (placed.json's with ``shrunk``: ``--shrink``), measured on its
    body box with ``use_body`` (``--compact``: the placer holds off-centre bodies).
    Returns ``(checked, findings)``."""
    cons = spec["constraints"]
    width, height = cons["board"]["outline"]["w"], cons["board"]["outline"]["h"]
    outline = placed.get("outline") or {}
    if shrunk and outline.get("width") and outline.get("height"):
        width, height = outline["width"], outline["height"]
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
        x0, y0, x1, y1 = body_extent(comp, use_body)
        distance = dict(south=y0, north=height - y1, west=x0, east=width - x1)[rule["edge"]]
        if distance > rule.get("tolerance_mm", 1.0) + 1e-3:
            findings.append("%s: %.3f mm from the %s edge" % (ref, distance, rule["edge"]))
    return checked, findings


# The vendor profile data pnr.fab_profile resolves data profiles from (yapnr.fab.capability), frozen
# with the engine so a run under oshpark-4l or jlc-4l uses the data of its own checkout.
FAB_DATA_SOURCES = ("yapnr/__init__.py", "yapnr/fab/__init__.py", "yapnr/fab/capability.py")

# The parts of PNR_COMPACT (pnr.compact_flags.PARTS; test_compact keeps them equal)
# --compact-off may drop.
COMPACT_PARTS = (
    "GP",
    "RANK",
    "LEGALIZE",
    "COURTYARD",
    "DROPS",
    "WIRE",
    "TURN",
    "SATELLITES",
    "PAIRS",
    "RELAX",
)


# Ambient PNR_* switches an operator happens to have set must not silently change the suite's
# configuration; telemetry is the exception: PNR_LIVE_* (hardware/pnr/pnr/live.py) and
# PNR_PROFILE_DIR (pnr/profile.py) feed no routing/placement decision, and letting them through
# is what lets a task under `yapnr exp` with [live] or [profile] on emit events and profiles.
LIVE_ENV_PREFIX = "PNR_LIVE_"
TELEMETRY_ENV = ("PNR_PROFILE_DIR",)
AMBIENT_ENV_KEEP = ("PNR_LOCAL_PRESSURE", "PNR_JOINT_ACCESS")


def scrubbed_suite_env(base_env, **overrides):
    """``base_env`` plus ``overrides``, with ambient ``PNR_*`` switches stripped except
    ``AMBIENT_ENV_KEEP``, ``TELEMETRY_ENV`` and anything under ``LIVE_ENV_PREFIX``."""
    env = dict(base_env, **overrides)
    for key in list(env):
        if (
            key.startswith("PNR_")
            and key not in AMBIENT_ENV_KEEP
            and key not in TELEMETRY_ENV
            and not key.startswith(LIVE_ENV_PREFIX)
        ):
            del env[key]
    return env


def compact_environment(compact, compact_off=(), shrink=False):
    """The PNR_COMPACT / PNR_SHRINK variables of ``--compact``, ``--compact-off`` and
    ``--shrink`` (set after the ambient PNR_* variables are stripped, so provenance
    records them); empty when none is given."""
    if compact_off and not compact:
        raise ValueError("--compact-off needs --compact")
    env = {}
    if compact:
        env["PNR_COMPACT"] = "1"
        for part in sorted(set(compact_off)):
            env["PNR_COMPACT_" + part] = "0"
    if shrink:
        env["PNR_SHRINK"] = "1"
    return env


def legalize_environment(
    gp_polish=False,
    gp_channels=None,
    pool_source_clamp=False,
    legalize_hpwl=None,
    reorient=None,
    channel_clearance=None,
    line_satellites=False,
    channel_layers=False,
):
    """The PNR_GP_POLISH / PNR_GP_CHANNELS / PNR_POOL_SOURCE_CLAMP / PNR_LEGALIZE_HPWL /
    PNR_LEGALIZE_REORIENT / PNR_LEGALIZE_CHANNEL_CLEARANCE / PNR_LINE_SATELLITES /
    PNR_CHANNEL_LAYERS variables of ``--gp-polish``, ``--gp-channels``, ``--pool-source-clamp``,
    ``--legalize-hpwl``, ``--legalize-reorient``, ``--legalize-channel-clearance``,
    ``--line-satellites`` and ``--channel-layers``
    (``pnr.legalize_flags``; set after the ambient PNR_* variables are stripped, so provenance
    records them); empty when none is given. ``reorient`` is ``"1"`` (guarded), ``"wire"`` or
    None; ``channel_clearance`` is ``"fab"`` or None."""
    env = {}
    for name, value in (("--gp-channels", gp_channels), ("--legalize-hpwl", legalize_hpwl)):
        if value is not None and not (math.isfinite(value) and value > 0):
            raise ValueError("%s takes a positive weight, got %r" % (name, value))
    if gp_polish:
        env["PNR_GP_POLISH"] = "1"
    if gp_channels is not None:
        env["PNR_GP_CHANNELS"] = repr(float(gp_channels))
    if pool_source_clamp:
        env["PNR_POOL_SOURCE_CLAMP"] = "1"
    if legalize_hpwl is not None:
        env["PNR_LEGALIZE_HPWL"] = repr(float(legalize_hpwl))
    if reorient not in (None, "1", "wire"):
        raise ValueError("--legalize-reorient takes nothing or wire, got %r" % (reorient,))
    if reorient:
        env["PNR_LEGALIZE_REORIENT"] = reorient
    if channel_clearance not in (None, "fab"):
        raise ValueError("--legalize-channel-clearance takes fab, got %r" % (channel_clearance,))
    if channel_clearance:
        env["PNR_LEGALIZE_CHANNEL_CLEARANCE"] = channel_clearance
    if line_satellites:
        env["PNR_LINE_SATELLITES"] = "1"
    if channel_layers:
        env["PNR_CHANNEL_LAYERS"] = "1"
    return env


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
        + sorted((repo / "hardware/pnr/pnr").rglob("*.c"))
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


def case_fab_profile(spec, profile):
    """The fab profile a case is routed and judged under: the run's ``profile``, or the
    one a design declares it needs (``fab_profile``: a BGA rung's 0.15 mm drills) when
    the run selects a fab profile at all. Legacy keeps every design's own fab block (the
    A/B baseline), so a declaration does not apply under it."""
    declared = spec.get("fab_profile")
    if declared and profile != "legacy":
        return declared
    return profile


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
        "--packed-maze",
        action="store_true",
        help="The packed CPU maze kernel (the default; kept so recorded configurations still parse)",
    )
    ap.add_argument(
        "--maze-kernel",
        choices=("packed", "native"),
        default="packed",
        help=(
            "native: build the C search loop from the frozen sources with the host compiler and "
            "route with it (identical routes; the packed kernel runs if it cannot load)"
        ),
    )
    ap.add_argument(
        "--exact-separation",
        choices=("off", "recover", "full"),
        help=(
            "PNR_EXACT_SEPARATION: recover routes again with the exact pairwise separation when "
            "a detailed route leaves connections open (and keeps it only with fewer open), full "
            "routes with it only; default: the engine's (recover)"
        ),
    )
    ap.add_argument(
        "--reference-maze",
        action="store_true",
        help="Route with the reference dict A* kernel (PNR_PACKED_MAZE=0) instead of the packed one",
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
        "--profile",
        action="store_true",
        help="Profile each case's place-route stage (pnr.profile: CASE/profile/*.json, its "
        "cProfile functions and pnr.profile.span stages); observational only",
    )
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
        "--hard",
        action="store_true",
        help="Also offer the hard rungs (hard_rungs.hard_rungs(), outside the ladder) to --case",
    )
    ap.add_argument(
        "--design-json",
        action="append",
        default=[],
        help="Also offer the designs in this JSON list (e.g. lenmatch_scratch.py write) to --case",
    )
    ap.add_argument(
        "--lane",
        action="append",
        choices=("ladder", "nightly", "manual"),
        help=(
            "Run the hard rungs whose ci.lane is LANE (repeatable; with any --case given); "
            "implies --hard. The ladder lane is the hard rungs promoted to the public ladder"
        ),
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
        "--compact",
        action="store_true",
        help=(
            "PNR_COMPACT=1: compact placement (spread 1.0, clustered starts, the courtyard gap "
            "and copper margins in the legalizer, offset courtyards, a compactness tie-break)"
        ),
    )
    ap.add_argument(
        "--compact-off",
        action="append",
        default=[],
        choices=COMPACT_PARTS,
        metavar="PART",
        help="With --compact: drop one part, PNR_COMPACT_<PART>=0 (repeatable; ablations)",
    )
    ap.add_argument(
        "--gp-polish",
        action="store_true",
        help=(
            "PNR_GP_POLISH=1: a final global-placement phase on the legalizer's own slots, turns "
            "frozen (docs/design/compact-placement.md, section 11)"
        ),
    )
    ap.add_argument(
        "--gp-channels",
        type=float,
        metavar="LAMBDA",
        help="PNR_GP_CHANNELS=LAMBDA: the polish (implied) also weighs the legalizer's channel cost",
    )
    ap.add_argument(
        "--pool-source-clamp",
        action="store_true",
        help="PNR_POOL_SOURCE_CLAMP=1: the initial pool's source start begins inside the outline",
    )
    ap.add_argument(
        "--legalize-hpwl",
        type=float,
        metavar="W",
        help=(
            "PNR_LEGALIZE_HPWL=W: the legalizer's slot cost gains W times the part's wirelength "
            "and the turn is chosen with the slot among all four"
        ),
    )
    ap.add_argument(
        "--legalize-reorient",
        nargs="?",
        const="1",
        choices=("1", "wire"),
        help=(
            "PNR_LEGALIZE_REORIENT=1: in-place turns that shorten wires after legalization, "
            "never raising a part's channel shortage; 'wire' drops that guard"
        ),
    )
    ap.add_argument(
        "--legalize-channel-clearance",
        choices=("fab",),
        help=(
            "PNR_LEGALIZE_CHANNEL_CLEARANCE=fab: the legalizer's channel model spaces unclassed "
            "nets at the fab clearance (the router's) instead of the board default"
        ),
    )
    ap.add_argument(
        "--line-satellites",
        action="store_true",
        help=(
            "PNR_LINE_SATELLITES=1: a line group carries each member's series part (a two-pad "
            "part on a two-pin net to the member) flush beside it"
        ),
    )
    ap.add_argument(
        "--channel-layers",
        action="store_true",
        help=(
            "PNR_CHANNEL_LAYERS=1: the routing-channel model (global placement, the legalizer "
            "and its push) credits the board's other signal layers"
        ),
    )
    ap.add_argument(
        "--shrink",
        action="store_true",
        help=(
            "PNR_SHRINK=1: the flat driver searches a smaller outline inside the design's "
            "(the board shrinks); hard rungs are exempt"
        ),
    )
    ap.add_argument(
        "--power-first",
        action="store_true",
        help="PNR_POWER_FIRST=1: lexicographic power-first placement (pnr.place.power_first)",
    )
    ap.add_argument(
        "--route-pairs-diff-pairs",
        action="store_true",
        help=(
            "PNR_FORCE_ROUTE_PAIRS_FOR_DIFF_PAIRS=1: route_pairs: coupled for any design that "
            "declares a diff_pair, without editing the case (ladder-v2 ab-pairs-pool A/B)"
        ),
    )
    ap.add_argument(
        "--rail-alloc",
        choices=("search", "static"),
        default=None,
        help=(
            "PNR_RAIL_ALLOC: plane_partition candidates decided by routing the best "
            "alternatives (search, the engine's default) or by the static rule of thumb "
            "(static: the A/B arm, pnr.rail_alloc)"
        ),
    )
    ap.add_argument(
        "--exact-late-room",
        action="store_true",
        help=(
            "PNR_EXACT_LATE_ROOM=1: the exact-separation recovery also runs on a board whose "
            "plane pads writeback drops after routing, keeping its route only when every drop "
            "keeps its room (pnr.route.detail.maze.late_drop_room)"
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
    hard = hard_rungs() if args.hard or args.lane else []
    allcases = designs() + (showcases() if args.showcases else []) + hard
    for path in args.design_json:
        allcases += json.loads(Path(path).read_text())
    if args.lane:
        lane = {c["name"] for c in hard if c["ci"]["lane"] in args.lane}
        cases = [c for c in allcases if c["name"] in lane or c["name"] in args.case]
    else:
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
    env = scrubbed_suite_env(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(freeze / "hardware/pnr"), str(freeze)]),
        PNR_LOCAL_PRESSURE="1",
    )
    if args.packed_maze and args.reference_maze:
        raise SystemExit("--packed-maze and --reference-maze are exclusive")
    if args.packed_maze:
        env["PNR_PACKED_MAZE"] = "1"
    if args.reference_maze:
        env["PNR_PACKED_MAZE"] = "0"
    if args.exact_separation:
        env["PNR_EXACT_SEPARATION"] = args.exact_separation
    native = None
    if args.maze_kernel == "native":
        if args.reference_maze:
            raise SystemExit("--maze-kernel native and --reference-maze are exclusive")
        # The frozen C source, compiled once for the run (pnr.route.detail.native_maze).
        # Without a working compiler the run keeps the packed kernel (identical routes)
        # and says so here and in provenance.
        try:
            library = subprocess.run(
                [
                    args.python,
                    "-c",
                    "import sys; from pnr.route.detail.native_maze import build_library; "
                    "print(build_library(sys.argv[1]))",
                    str(out / "native"),
                ],
                env=dict(env, PYTHONPATH=str(freeze / "hardware/pnr")),
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            detail = (getattr(error, "stderr", None) or str(error)).strip().splitlines()
            reason = detail[-1] if detail else type(error).__name__
            print("native maze kernel not built (%s); the packed kernel routes" % reason)
            native = dict(library=None, error=reason)
        else:
            env.update(PNR_MAZE_KERNEL="native", PNR_MAZE_LIB=library)
            native = dict(library=Path(library).name, sha256=sha(Path(library)))
    if args.dense_maze_cost:
        env["PNR_DENSE_MAZE_COST"] = "1"
    if args.power_first:
        env["PNR_POWER_FIRST"] = "1"
    if args.route_pairs_diff_pairs:
        env["PNR_FORCE_ROUTE_PAIRS_FOR_DIFF_PAIRS"] = "1"
    if args.rail_alloc:
        env["PNR_RAIL_ALLOC"] = args.rail_alloc
    if args.exact_late_room:
        env["PNR_EXACT_LATE_ROOM"] = "1"
    if args.detail_pitch_mm is not None:
        import math

        if not math.isfinite(args.detail_pitch_mm) or args.detail_pitch_mm < 0:
            raise ValueError("Detail pitch must be finite and nonnegative")
        env["PNR_DETAIL_PITCH_MM"] = str(args.detail_pitch_mm)
    if args.batched_wirelength:
        env["PNR_BATCHED_WIRELENGTH"] = "1"
    try:
        env.update(compact_environment(args.compact, args.compact_off, args.shrink))
        env.update(
            legalize_environment(
                args.gp_polish,
                args.gp_channels,
                args.pool_source_clamp,
                args.legalize_hpwl,
                args.legalize_reorient,
                args.legalize_channel_clearance,
                args.line_satellites,
                args.channel_layers,
            )
        )
    except ValueError as error:
        raise SystemExit(str(error))
    env["PNR_FAB_PROFILE"] = (
        args.fab_profile
    )  # routed and judged under one profile (route_case.py, writeback)
    # The KiCad-side judge's numeric solves (pnr.ir_extract, the ir_drop check) run in this
    # Python when KiCad's has no numpy (the container image's).
    env["PNR_PYTHON"] = str(args.python)
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
        pnr_environment={
            k: v
            for k, v in env.items()
            if k.startswith("PNR_") and k not in ("PNR_MAZE_LIB", "PNR_PYTHON") + TELEMETRY_ENV
        },
        native_maze=native,
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

                case_profile = case_fab_profile(spec, args.fab_profile)
                if case_profile != args.fab_profile:
                    result["fab_profile"] = case_profile

                def run(name, cmd, extra=None):
                    if case_profile != args.fab_profile:
                        extra = dict(extra or {}, PNR_FAB_PROFILE=case_profile)
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
                # A hard rung's custom rules (via policy, plane layers, pair skew) sit
                # beside the boards before any stage loads them: the zone filler and
                # the judge read <board>.kicad_dru (never generated under legacy, so
                # the fab profile leaves a hand-written file alone).
                dru = dru_text(spec) if spec.get("tier") == "hard" else None
                if dru:
                    for stem in ("source", "routed"):
                        (root / (stem + ".kicad_dru")).write_text(dru)
                # A design's driver: flat (route_case.py), hierarchical (hier_case.py) or
                # Monte-Carlo successive halving (mc_case.py).
                driver = {"hier": "hier_case.py", "mc": "mc_case.py"}.get(
                    spec.get("driver"), "route_case.py"
                )
                extra = dict(native.environment()) if native else {}
                if args.shrink and spec.get("tier") == "hard":
                    # A hard rung's outline is part of its contract: never shrunk.
                    extra["PNR_SHRINK"] = "0"
                    result["shrink_exempt"] = True
                place_route = [args.python, frozen_here / driver, root, seed, args.rounds]
                if args.profile:
                    # pnr.profile runs the driver under cProfile with its spans recorded.
                    place_route[1:1] = ["-m", "pnr.profile", "--label", "place-route"]
                    extra["PNR_PROFILE_DIR"] = str(root / "profile")
                run("place-route", place_route, extra or None)
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
                # The independent constraint check (check_constraints.py) on the saved
                # board: every case, gating only the hard rungs.
                run(
                    "checks",
                    [
                        args.kicad_python,
                        frozen_here / "check_constraints.py",
                        board,
                        "--spec",
                        root / "design.json",
                        "--out",
                        root / "checks.json",
                        "--exit-zero",
                    ],
                )
                pnr = json.loads((root / "pnr-report.json").read_text())
                audit = json.loads((root / "native-audit.json").read_text())
                drc = json.loads((root / "drc.json").read_text())
                result.update(
                    reasons=acceptance(pnr, audit, drc, spec.get("designed_open") or ()),
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
                checks = json.loads((root / "checks.json").read_text())
                result["checks"] = checks["summary"]
                if spec.get("tier") == "hard":
                    result["dims"] = spec["dims"]
                    if checks["summary"]["satisfied"] != checks["summary"]["total"]:
                        result["reasons"].append("constraint_violated")
                if result.get("gloss_error"):
                    result["reasons"].append("gloss_error")
                if args.gloss_measure:
                    result["gloss_measure"] = gloss_measure(root, board, args, run, glossing)
                placed_doc = json.loads((root / "placed.json").read_text())
                result["compactness"] = compactness(placed_doc)
                constraints = spec["constraints"]
                if constraints.get("line_group") or any(
                    rule.get("hard") for rule in (constraints.get("edge_align") or {}).values()
                ):
                    checked, findings = constraint_reasons(
                        spec,
                        placed_doc,
                        use_body=args.compact and "COURTYARD" not in args.compact_off,
                        shrunk=args.shrink,
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
            # The case/seed lane's own terminal event (docs/viewer.md "Finished without a final
            # event"): candidates run under this case (hardware/pnr/pnr/place/initial_pool.py and
            # friends) already emit their own candidate_complete/candidate_failed, but nothing
            # emitted one for the case lane itself -- a live/mirrored campaign's parent lanes had
            # no terminal event at all, only whatever mid-run telemetry (source_round_start,
            # worker_config_applied) happened to land last, which read "stalled" forever once the
            # campaign actually finished. PNR_LIVE_CANDIDATE here is this task's own id (ladder/
            # <case>/sN, set by yapnr.exp.task for a live campaign; "controller" for a bare local
            # run), never a candidate's -- run.py never overrides it the way the pool code does
            # for its own nested candidates, and always restores it afterward.
            # violations is a total count here (yapnr.viewer.progress reads it the same way a
            # route_result/phase_complete frame's "violations" is: a number, not result.json's
            # own by-DRC-type breakdown dict) rather than result["violations"] verbatim.
            violation_total = sum((result.get("violations") or {}).values())
            live_emit(
                "case_complete" if result["passed"] else "case_failed",
                data=dict(
                    passed=result["passed"],
                    reasons=result.get("reasons") or [],
                    opens=result.get("opens"),
                    violations=violation_total,
                    elapsed_seconds=result["elapsed_seconds"],
                    case=spec["name"],
                    seed=seed,
                    error=result.get("error"),
                ),
            )
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
