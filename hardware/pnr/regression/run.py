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


def source_inputs(repo):
    scanner = repo / "hardware/tools/scan_via_proximity.py"
    if not scanner.is_file():
        raise FileNotFoundError("Native regression requires " + str(scanner))
    return (
        sorted((repo / "hardware/pnr/pnr").rglob("*.py"))
        + sorted((repo / "hardware/pnr/pnr").rglob("*.c"))
        + sorted((repo / "hardware/pnr/regression").glob("*.py"))
        + [scanner]
    )


# The fabrication profile the ladder routes and is judged under (pnr.fab_profile). The fixtures
# carry their own fab block (0.2 mm clearance, 0.6/0.3 mm vias; README), which is exactly what the
# legacy profile enforces; the engine's default (jlc-pofv) overrides it with JLC capability values.
FAB_PROFILES = ("legacy", "jlc-pofv")
DEFAULT_FAB_PROFILE = "legacy"


def engine_revision(repo):
    """``(commit, dirty)`` of the checkout the sources are frozen from; ``(None, None)`` without git."""

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
        "--lane",
        choices=("nightly", "manual"),
        help="Run the hard rungs whose ci.lane is LANE (with any --case given); implies --hard",
    )
    ap.add_argument(
        "--fab-profile",
        choices=FAB_PROFILES,
        default=DEFAULT_FAB_PROFILE,
        help=(
            "PNR_FAB_PROFILE for every stage (default legacy: the fixtures' own fab block); "
            "jlc-pofv routes and judges under the engine's default profile"
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
    out.mkdir(parents=True, exist_ok=False)
    hard = hard_rungs() if args.hard or args.lane else []
    allcases = designs() + (showcases() if args.showcases else []) + hard
    if args.lane:
        lane = {c["name"] for c in hard if c["ci"]["lane"] == args.lane}
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
    env = dict(os.environ, PYTHONPATH=str(freeze / "hardware/pnr"), PNR_LOCAL_PRESSURE="1")
    # Ambient experiment switches must not silently change the suite configuration.
    for key in list(env):
        if key.startswith("PNR_") and key not in ("PNR_LOCAL_PRESSURE", "PNR_JOINT_ACCESS"):
            del env[key]
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
        arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        pnr_environment={
            k: v for k, v in env.items() if k.startswith("PNR_") and k != "PNR_MAZE_LIB"
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
                    reasons=acceptance(pnr, audit, drc),
                    opens=len(drc["unconnected_items"]),
                    violations=dict(Counter(x["type"] for x in drc["violations"])),
                    tracks=audit["tracks"],
                    vias=audit["vias"],
                    copper_length_mm=audit["copper_length_mm"],
                    pnr=pnr,
                    source_board_sha256=source,
                    board_sha256=sha(board),
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
