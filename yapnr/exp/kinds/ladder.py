"""``ladder-cell``: one regression ladder cell (case x seed x configuration) per task.

Each task runs ``hardware/pnr/regression/run.py`` for one case and one seed from the campaign's
source bundle, with the runner's own options from ``[config]`` (and named variants in
``[configs.<name>]``). The initial placement pool stays inside the cell. ``assemble`` rebuilds,
per configuration, the run directory one ``run.py --out`` of all cells would have written
(``summary.json``, ``junit.xml``, ``provenance.json`` with a ``shards`` list, the version files
and every ``CASE-seed-S/``), so ``tools/ci/ladder_summary.py`` and the animation renderer read it
unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping
from xml.etree import ElementTree as ET

from yapnr.exp.kinds import base

RUNNER = "src/hardware/pnr/regression/run.py"
# Every hard-rung stackup variant (hardware/pnr/regression/hard_rungs.py with_stackup, e.g.
# "...-6L-SGSGPS") names its layer codes in upper case, so a campaign cannot select most hard
# rungs without it.
CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,80}$")
FAB_PROFILES = ("legacy", "jlc-pofv")

# Seconds per case on the development Mac (Apple M4 performance core) with the initial pool of
# 8 starts and 3 finalists, from docs/animations/ladder-results.json (2026-10). Without the pool a
# cell takes about 0.6 of this. Unknown cases (hard rungs, showcases) assume 900 s until
# ``[config] reference_seconds`` or a calibration says otherwise.
MAC_SECONDS_WITH_POOL = {
    "01-connector-led-2": 8.7,
    "02-resistor-led-3": 8.0,
    "03-branched-leds-5": 8.7,
    "04-inverter-leds-8": 17.1,
    "05-timer-led-10": 30.5,
    "06-chaser-14": 67.5,
    "07-chaser-20": 125.8,
    "08-chaser-20-plane": 202.4,
}
UNKNOWN_CASE_SECONDS = 900.0
NO_POOL_FACTOR = 0.6

OPTIONS = {
    "rounds": int,
    "timeout": (int, float),
    "fab_profile": str,
    "initial_pool": bool,
    "initial_starts": int,
    "initial_finalists": int,
    "packed_maze": bool,
    "batched_wirelength": bool,
    "dense_maze_cost": bool,
    "detail_pitch_mm": (int, float),
    "trace": bool,
    "profile": bool,
    "trace_placement_every": int,
    "showcases": bool,
    "hard": bool,
    "compact": bool,
    "compact_off": list,
    "shrink": bool,
    "gloss": bool,
    "gloss_measure": bool,
    "gp_polish": bool,
    "gp_channels": (int, float),
    "pool_source_clamp": bool,
    "legalize_hpwl": (int, float),
    "legalize_reorient": bool,
    "legalize_reorient_wire": bool,
    "legalize_channel_clearance_fab": bool,
    "line_satellites": bool,
    "channel_layers": bool,
    "power_first": bool,
    "route_pairs_diff_pairs": bool,
    "route_compact": (bool, str),
    "macro_hull": bool,
    "hull_dovetail": (int, float),
    "rail_alloc": str,
    "exact_late_room": bool,
    "exact_settle": bool,
    "fanout_best_round": bool,
    "in_pad_scan": bool,
    "fanout_band_mm": (int, float),
    "maze_kernel": str,
}
FLAGS = {
    "packed_maze": "--packed-maze",
    "batched_wirelength": "--batched-wirelength",
    "dense_maze_cost": "--dense-maze-cost",
    "trace": "--trace",
    # pnr.profile around each cell's place-route stage (CASE/profile/*.json).
    "profile": "--profile",
    "showcases": "--showcases",
    # The hard rungs (hardware/pnr/regression/hard_rungs.py) become selectable cases.
    "hard": "--hard",
    # PNR_COMPACT / PNR_SHRINK (docs/design/compact-placement.md) and PNR_GLOSS.
    "compact": "--compact",
    "shrink": "--shrink",
    "gloss": "--gloss",
    "gloss_measure": "--gloss-measure",
    # The legalizer and global-placement switches (hardware/pnr/pnr/legalize_flags.py).
    "gp_polish": "--gp-polish",
    "pool_source_clamp": "--pool-source-clamp",
    "legalize_reorient": "--legalize-reorient",
    "line_satellites": "--line-satellites",
    "channel_layers": "--channel-layers",
    # ladder-v2 ab-pairs-pool A/B.
    "power_first": "--power-first",
    "route_pairs_diff_pairs": "--route-pairs-diff-pairs",
    # PNR_MACRO_HULL (hardware/pnr/pnr/place/hull.py).
    "macro_hull": "--macro-hull",
    # The exact-separation recovery beside late plane drops (router-keepouts).
    "exact_late_room": "--exact-late-room",
    # Settled contested space and the fanout's best-round legalization (top-rung).
    "exact_settle": "--exact-settle",
    "fanout_best_round": "--fanout-best-round",
    "in_pad_scan": "--in-pad-scan",
}
# Weighted legalizer switches: option -> runner flag taking the weight.
WEIGHTS = {
    "gp_channels": "--gp-channels",
    "legalize_hpwl": "--legalize-hpwl",
    "hull_dovetail": "--hull-dovetail",
    "fanout_band_mm": "--fanout-band-mm",
}
# The PNR_COMPACT parts ``compact_off`` may name (run.py --compact-off; equal to
# hardware/pnr/pnr/compact_flags.py PARTS, which test_kinds checks).
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

# The parts of PNR_ROUTE_COMPACT ``route_compact`` may name (run.py --route-compact; equal to
# hardware/pnr/pnr/place/route_compact.py PARTS).
ROUTE_COMPACT_PARTS = ("TOP", "BLOCK", "FLAT")

SUMMARY = [
    "run/summary.json",
    "run/provenance.json",
    "run/junit.xml",
    "run/*-version.txt",
    "run/*/result.json",
    "run/*/drc.json",
    "run/*/design.json",
    "run/*/profile/*.json",
]


def runner_arguments(options: Mapping[str, Any]) -> List[str]:
    args = [
        "--rounds",
        str(options.get("rounds", 4)),
        "--timeout",
        str(options.get("timeout", 600)),
        "--fab-profile",
        options.get("fab_profile", "legacy"),
    ]
    for key, flag in FLAGS.items():
        if options.get(key):
            args.append(flag)
    if options.get("initial_pool"):
        args += [
            "--initial-pool",
            "--initial-starts",
            str(options.get("initial_starts", 8)),
            "--initial-finalists",
            str(options.get("initial_finalists", 3)),
        ]
    if options.get("maze_kernel"):
        args += ["--maze-kernel", str(options["maze_kernel"])]  # packed or native
    if options.get("detail_pitch_mm") is not None:
        args += ["--detail-pitch-mm", str(options["detail_pitch_mm"])]
    if options.get("trace_placement_every") is not None:
        args += ["--trace-placement-every", str(options["trace_placement_every"])]
    for part in options.get("compact_off") or []:
        args += ["--compact-off", part]
    for key, flag in WEIGHTS.items():
        if options.get(key) is not None:
            args += [flag, repr(float(options[key]))]
    if options.get("legalize_reorient_wire") and not options.get("legalize_reorient"):
        args += ["--legalize-reorient", "wire"]  # the in-place turns without the channel guard
    if options.get("legalize_channel_clearance_fab"):
        args += ["--legalize-channel-clearance", "fab"]
    compact_parts = options.get("route_compact")
    if compact_parts is not None:
        # PNR_ROUTE_COMPACT (hardware/pnr/pnr/place/route_compact.py): true for every part,
        # false (an explicit `route_compact = false`, distinct from leaving the key out) opts
        # a hierarchical cell out of the driver's default bundle (run.py --route-compact 0).
        if compact_parts is False:
            args += ["--route-compact", "0"]
        else:
            args += ["--route-compact", "1" if compact_parts is True else str(compact_parts)]
    if options.get("rail_alloc"):
        args += ["--rail-alloc", options["rail_alloc"]]  # PNR_RAIL_ALLOC (pnr.rail_alloc)
    return args


class LadderCell(base.Kind):
    name = "ladder-cell"
    short = "ladder"
    source_paths = ("hardware/pnr", "hardware/tools")
    # The vendor profile data pnr.fab_profile resolves a data profile from (run.py
    # FAB_DATA_SOURCES and yapnr/fab/data): a cell under jlc-6l-hdi or oshpark-4l, or a rung
    # that declares one, uses its own checkout's data, not the image's.
    optional_source_paths = (
        "yapnr/__init__.py",
        "yapnr/fab/__init__.py",
        "yapnr/fab/capability.py",
        "yapnr/fab/data",
    )
    # A cell's work directory holds a few MB (the source bundle and the case's outputs); 2 GB
    # keeps 8 cells per VM within the boot disk's free space (cost.BOOT_DISK_RESERVE_GB).
    default_resources = dict(cpus=1, memory_gb=3, disk_gb=2, max_wall_s=7200)
    default_determinism = "wall_clock_budgeted"
    config_keys = frozenset(OPTIONS) | {"prune", "reference_seconds"}
    matrix_axes = ("case", "seed", "config")
    required_axes = ("case", "seed")

    def check(self, campaign: Mapping[str, Any]) -> List[str]:
        errors = super().check(campaign)
        matrix = campaign.get("matrix", {})
        for case in matrix.get("case", []):
            if not isinstance(case, str) or not CASE_RE.match(case):
                errors.append("matrix.case %r is not a case name" % (case,))
        for seed in matrix.get("seed", []):
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                errors.append("matrix.seed %r is not a non-negative integer" % (seed,))
        configs = campaign.get("configs", {})
        for name in matrix.get("config", []):
            if name not in configs:
                errors.append("matrix.config names %r, which has no [configs.%s]" % (name, name))
        for where, options in [("config", campaign.get("config", {}))] + [
            ("configs.%s" % k, v) for k, v in configs.items()
        ]:
            if not isinstance(options, dict):
                errors.append("%s is a table" % where)
                continue
            for key, value in options.items():
                if key in ("prune", "reference_seconds") and where == "config":
                    continue
                kind = OPTIONS.get(key)
                if kind is None:
                    if where != "config":
                        errors.append("%s.%s is not a runner option" % (where, key))
                elif (
                    isinstance(value, bool)
                    and bool not in (kind if isinstance(kind, tuple) else (kind,))
                    or not isinstance(value, kind)
                ):
                    errors.append("%s.%s has the wrong type" % (where, key))
            if options.get("fab_profile", "legacy") not in FAB_PROFILES:
                errors.append("%s.fab_profile is one of %s" % (where, ", ".join(FAB_PROFILES)))
            if options.get("rail_alloc", "search") not in ("search", "static"):
                errors.append("%s.rail_alloc is search or static" % where)
            off = options.get("compact_off")
            if isinstance(off, list) and any(p not in COMPACT_PARTS for p in off):
                errors.append(
                    "%s.compact_off names parts of %s" % (where, ", ".join(COMPACT_PARTS))
                )
            parts = options.get("route_compact")
            if (
                isinstance(parts, str)
                and parts != "1"
                and not {p.strip().upper() for p in parts.split(",") if p.strip()}
                <= set(ROUTE_COMPACT_PARTS)
            ):
                errors.append(
                    "%s.route_compact is true or a comma list of %s"
                    % (where, ", ".join(ROUTE_COMPACT_PARTS))
                )
            if options.get("legalize_reorient") and options.get("legalize_reorient_wire"):
                errors.append("%s: legalize_reorient and legalize_reorient_wire exclude" % where)
            for key in WEIGHTS:
                value = options.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                    errors.append("%s.%s is a positive weight" % (where, key))
            common = campaign.get("config", {}) if where != "config" else {}
            if off and not (options.get("compact") or common.get("compact")):
                errors.append("%s.compact_off needs compact" % where)
        for name in configs:
            if not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", name):
                errors.append("configuration name %r: lower-case letters, digits, dashes" % name)
        return errors

    def _configs(self, campaign: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        common = {
            k: v
            for k, v in campaign.get("config", {}).items()
            if k not in ("prune", "reference_seconds")
        }
        configs = campaign.get("configs", {})
        if not configs:
            return {"default": common}
        names = campaign.get("matrix", {}).get("config") or sorted(configs)
        return {name: dict(common, **configs[name]) for name in names}

    def expand(self, campaign: Mapping[str, Any], ctx: base.Context) -> List[Dict[str, Any]]:
        if ctx.source_input is None:
            raise ValueError("ladder-cell tasks need a source bundle (source = 'HEAD' or a commit)")
        configs = self._configs(campaign)
        prune = list(campaign.get("config", {}).get("prune", []))
        matrix = {k: v for k, v in campaign["matrix"].items() if k != "config"}
        tasks = []
        for name, options in configs.items():
            for row in base.matrix_product(matrix, ("case", "seed")):
                case, seed = row["case"], int(row["seed"])
                parts = ["ladder"] + ([name] if len(configs) > 1 else []) + [case, "s%d" % seed]
                command = [
                    "${PYTHON}",
                    RUNNER,
                    "--repo",
                    "src",
                    "--out",
                    "out/run",
                    "--python",
                    "${PYTHON}",
                    "--kicad-python",
                    "${KICAD_PYTHON}",
                    "--kicad-cli",
                    "${KICAD_CLI}",
                    "--library",
                    "${FOOTPRINTS}",
                    "--case",
                    case,
                    "--seed",
                    str(seed),
                ] + runner_arguments(options)
                tasks.append(
                    base.make_task(
                        ctx,
                        kind=self.name,
                        readable_id="/".join(parts),
                        command=command,
                        env=base.SINGLE_THREAD_ENV,
                        outputs={"root": "out", "summary": SUMMARY, "prune": prune},
                        done={"file": "out/run/summary.json", "json": {"complete": True}},
                        verdict={"file": "out/run/summary.json", "json_path": "passed"},
                        labels={"case": case, "seed": str(seed), "config": name},
                    )
                )
        return tasks

    def reference_seconds(self, task: Mapping[str, Any]) -> float:
        case = task["labels"].get("case", "")
        seconds = MAC_SECONDS_WITH_POOL.get(case, UNKNOWN_CASE_SECONDS)
        if case in MAC_SECONDS_WITH_POOL and "--initial-pool" not in task["command"]:
            seconds *= NO_POOL_FACTOR
        return min(seconds, float(task["resources"]["max_wall_s"]))

    def assemble(
        self,
        plan: Mapping[str, Any],
        tasks: List[Dict[str, Any]],
        fetched: Path,
        dest: Path,
        allow_mixed: bool = False,
    ) -> Dict[str, Any]:
        report = base.new_report()
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for task in tasks:
            groups.setdefault(task["labels"].get("config", "default"), []).append(task)
        for config, group in groups.items():
            out = dest / config
            assemble_run(plan, group, fetched, out, allow_mixed, report)
            report["paths"].append(str(out))
        return report


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def assemble_run(plan, tasks, fetched: Path, out: Path, allow_mixed: bool, report) -> None:
    """One run directory from the tasks of one configuration (in campaign order)."""
    out.mkdir(parents=True, exist_ok=True)
    results, shards, changed, provenances = [], [], [], []
    complete = True
    first_run = None
    for task in tasks:
        root = base.output_root(fetched, task)
        run = root / "run" if root else None
        summary = _read_json(run / "summary.json") if run else None
        if not summary or not summary.get("results"):
            complete = False
            report["missing"].append(task["id"])
            continue
        if first_run is None:
            first_run = run
        complete = complete and bool(summary.get("complete"))
        changed += summary.get("source_changed_during_run") or []
        for result in summary["results"]:
            directory = result.get("directory") or "%s-seed-%s" % (result["case"], result["seed"])
            base.copy_tree(run / directory, out / directory)
            results.append(result)
        provenance = _read_json(run / "provenance.json")
        if provenance:
            provenances.append(provenance)
        shards.append(base.shard(base.task_record(fetched, task), task))
        report["tasks"] += 1
    keys = ("sources_sha256", "engine_revision", "platform", "fab_profile")
    mixed = sorted(k for k in keys if len({json.dumps(p.get(k)) for p in provenances}) > 1)
    if mixed:
        message = "tasks differ in %s" % ", ".join(mixed)
        if not allow_mixed:
            raise ValueError(message + " (use --allow-mixed to assemble anyway)")
        report["warnings"].append(message)
    if first_run is not None:
        for name in ("python-version.txt", "kicad-version.txt"):
            if (first_run / name).is_file():
                base.copy_tree(first_run / name, out / name)
        if (first_run / "source-freeze").is_dir():
            base.copy_tree(first_run / "source-freeze", out / "source-freeze")
    passed = bool(results) and complete and all(r.get("passed") for r in results) and not changed
    summary = dict(
        passed=passed,
        complete=complete,
        source_changed_during_run=sorted(set(changed)),
        results=results,
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    write_junit(results, sorted(set(changed)), out / "junit.xml")
    provenance = dict(provenances[0]) if provenances else {"schema": "pnr-regression-v1"}
    provenance["seeds"] = sorted({r["seed"] for r in results})
    arguments = dict(provenance.get("arguments") or {})
    arguments["case"] = sorted({r["case"] for r in results})
    arguments["seed"] = provenance["seeds"]
    arguments.pop("out", None)
    arguments.pop("repo", None)
    provenance["arguments"] = arguments
    provenance["assembled"] = dict(
        by="yapnr exp fetch",
        campaign=plan.get("id"),
        image=(plan.get("image") or {}).get("ref"),
        mixed=mixed,
    )
    provenance["shards"] = shards
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2))


def write_junit(results: List[Mapping[str, Any]], changed: List[str], path: Path) -> None:
    """The runner's junit.xml (hardware/pnr/regression/run.py) for assembled results."""
    suite = ET.Element(
        "testsuite",
        name="native-pnr-ladder",
        tests=str(len(results)),
        failures=str(sum(not r.get("passed") for r in results)),
    )
    for r in results:
        test = ET.SubElement(
            suite,
            "testcase",
            name="%s-seed-%s" % (r["case"], r["seed"]),
            time=str(r.get("elapsed_seconds", 0)),
        )
        if not r.get("passed"):
            ET.SubElement(test, "failure", message=", ".join(r.get("reasons") or [])).text = (
                json.dumps(r, indent=2)
            )
    if changed:
        ET.SubElement(suite, "error", message="frozen_source_changed").text = json.dumps(changed)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
