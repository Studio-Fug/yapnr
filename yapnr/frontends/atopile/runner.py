"""``yapnr atopile build``: one bounded, isolated, offline ``ato build`` (replaces ato_build.sh and
rules_atopile's actions).

For each build the runner:

1. finds the atopile environment (``toolchain.discover``) and the headless KiCad (``kicad``);
2. copies the project into a fresh work directory (the source tree is never written, except by
   ``--update-layout``, which copies the new layout back);
3. writes every part of the project's parts lock into the copy, verified against the part cache;
4. writes the stock KiCad footprint libraries the sources reference into the build's
   ``fp-lib-table`` (atopile 0.15.8 resolves ``Library:Footprint`` only there);
5. starts the loopback picker (a thread of this process) on the catalog entries of the locked
   parts plus any ``--catalog`` files;
6. runs ``python -m atopile build`` with an allowlisted environment (``env.py``), the hook on its
   ``PYTHONPATH`` (``hook/``), explicit targets (atopile's implicit ``default`` target and its
   datasheet downloads are excluded) and a deadline that kills the whole process tree;
7. frames the board if asked (``--outline-margin-mm``), then copies the named outputs to the
   output directory with ``result.json``: the board, BOM, variables, power tree and pinout, never
   ``build/manifest.json`` (it holds absolute paths).

``result.json`` records the input id: the sha256 of the board with every UUID replaced by the nil
UUID, because atopile stamps fresh UUIDs on each build. Two builds of the same sources give the
same id.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from yapnr.frontends.atopile import env as env_mod
from yapnr.frontends.atopile import kicad, outline, parts, proc, toolchain
from yapnr.frontends.atopile.picker import catalog as catalog_mod
from yapnr.frontends.atopile.picker.server import PickerServer
from yapnr.partcache.client import PartCache, open_cache

DEFAULT_TARGETS = ("build-design", "bom", "variable-report", "power-tree", "pinout")
# Targets a build may name. Not `default`/`all` (they pull in datasheet downloads), not
# `datasheets`, `collect-manufacturing` or the viewers.
ALLOWED_TARGETS = frozenset(
    DEFAULT_TARGETS
    + ("manifest", "stackup", "data-interface-layout", "mfg-data", "step", "glb", "2d-image")
    + ("3d-image",)
)
ALWAYS_EXCLUDED = ("default", "datasheets", "collect-manufacturing")
DEFAULT_TIMEOUT = 1800.0
COPY_IGNORE = ("build", "manufacturing", ".git", "__pycache__", ".DS_Store", "yapnr-out")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NIL_UUID = "00000000-0000-0000-0000-000000000000"


class BuildError(RuntimeError):
    pass


@dataclass
class BuildOptions:
    project: Path
    build: str = "default"
    targets: Sequence[str] = DEFAULT_TARGETS
    out: Optional[Path] = None
    cache: Optional[str] = None
    catalogs: Sequence[Path] = ()
    timeout: float = DEFAULT_TIMEOUT
    outline_margin_mm: float = 0.0
    frozen: bool = False
    update_layout: bool = False
    keep_work: bool = False
    work_root: Optional[Path] = None
    stock_footprints: str = "referenced"
    kicad_cli: Optional[str] = None
    replace_parts: bool = False
    offline: bool = True


@dataclass
class BuildResult:
    ok: bool
    returncode: int
    timed_out: bool
    out: Path
    outputs: Dict[str, str] = field(default_factory=dict)
    input_id: Optional[str] = None
    work: Optional[Path] = None
    summary: Dict[str, Any] = field(default_factory=dict)


def normalized_board(text: str) -> str:
    """The board text with every UUID replaced by the nil UUID."""
    return _UUID.sub(NIL_UUID, text)


def input_id(board: Path) -> str:
    return hashlib.sha256(normalized_board(board.read_text(encoding="utf-8")).encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_build_name(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", name):
        raise BuildError(f"not a build name: {name!r}")
    return name


def _check_targets(targets: Sequence[str]) -> List[str]:
    unknown = sorted(set(targets) - ALLOWED_TARGETS)
    if unknown:
        raise BuildError(
            f"targets {unknown} are not allowed (allowed: {', '.join(sorted(ALLOWED_TARGETS))})"
        )
    return list(dict.fromkeys(targets))


def _copy_project(source: Path, dest: Path) -> None:
    def ignore(directory: str, names: List[str]) -> List[str]:
        skip = [n for n in names if n in COPY_IGNORE]
        if Path(directory) == source:
            skip += [n for n in names if n == ".ato"]
        return skip

    shutil.copytree(source, dest, ignore=ignore, symlinks=False)
    modules = source / ".ato" / "modules"
    if modules.is_dir():  # installed atopile packages: needed offline; atopile's caches are not
        shutil.copytree(modules, dest / ".ato" / "modules", symlinks=False)


def _outputs(work_project: Path, build: str) -> Dict[str, Path]:
    base = work_project / "build" / "builds" / build
    candidates = {
        "pcb": work_project / "elec" / "layout" / build / f"{build}.kicad_pcb",
        "bom_csv": base / f"{build}.bom.csv",
        "bom_json": base / f"{build}.bom.json",
        "variables": base / f"{build}.variables.ato.json",
        "power_tree": base / f"{build}.power_tree.ato.json",
        "power_tree_md": base / "power_tree.md",
        "pinout": base / "pinout",
        "stackup": base / f"{build}.stackup.json",
        "gerbers": base / f"{build}.gerber.zip",
        "pick_and_place": base / f"{build}.pick_and_place.csv",
        "step": base / f"{build}.pcba.step",
        "glb": base / f"{build}.pcba.glb",
    }
    return {name: path for name, path in candidates.items() if path.exists()}


def _catalogs(
    lock: Optional[Dict[str, Any]], cache: Optional[PartCache], files: Sequence[Path]
) -> List[Dict[str, Any]]:
    docs = [catalog_mod.load(path) for path in files]
    if lock is not None and cache is not None:
        wanted = parts.locked_lcsc(lock, cache)
        if wanted:
            docs.append(catalog_mod.validate(cache.catalog(wanted), where="part cache catalog"))
    return docs


def build(options: BuildOptions, log=print) -> BuildResult:
    project = Path(options.project).resolve()
    if not (project / "ato.yaml").is_file():
        raise BuildError(f"{project} has no ato.yaml")
    build_name = _check_build_name(options.build)
    targets = _check_targets(options.targets)
    if options.stock_footprints not in ("referenced", "all", "none"):
        raise BuildError("--stock-footprints must be referenced, all or none")
    out = Path(options.out or project / "yapnr-out" / build_name).resolve()

    tool = toolchain.discover()
    try:
        kicad_cli: Optional[Path] = kicad.find_cli(options.kicad_cli)
    except kicad.Unavailable as err:
        kicad_cli = None
        log(f"atopile: {err}; targets that need kicad-cli are unavailable")
    footprints = None
    if options.stock_footprints != "none":
        try:
            footprints = kicad.find_footprints(kicad_cli)
        except kicad.Unavailable as err:
            log(f"atopile: {err}")

    lock = parts.load(project)
    cache = open_cache(options.cache) if (lock and lock["parts"]) or options.cache else None

    work_root = Path(options.work_root) if options.work_root else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"yapnr-atopile-{build_name}-", dir=work_root))
    started = time.monotonic()
    try:
        work_project = work / "project"
        _copy_project(project, work_project)
        logs = work / "logs"
        logs.mkdir()
        materialized = []
        if lock and lock["parts"]:
            materialized = parts.materialize_lock(
                work_project, lock, cache, replace=options.replace_parts
            )
            log(f"atopile: {len(materialized)} parts from {cache.location}")
        stock = []
        if footprints is not None:
            table = work_project / "elec" / "layout" / build_name / "fp-lib-table"
            stock = kicad.write_fp_lib_table(
                table, footprints, work_project / "elec", options.stock_footprints == "all"
            )
        docs = _catalogs(lock, cache, options.catalogs)
        catalog_snapshot = logs / "catalog.json"
        catalog_snapshot.write_text(
            catalog_mod.dump(
                {
                    "schema": catalog_mod.SCHEMA,
                    "provenance": {"source": "yapnr atopile build (merged)"},
                    "parts": [p for d in docs for p in d["parts"]],
                }
            ),
            encoding="utf-8",
        )
        requests: List[Dict[str, Any]] = []
        hook_log = logs / "hook.jsonl"
        ato_log = logs / "ato.log"
        cmd = [str(tool.python), "-m", "atopile", "build", "-b", build_name, "-v"]
        for target in targets:
            cmd += ["-t", target]
        for target in ALWAYS_EXCLUDED:
            cmd += ["-x", target]
        cmd.append("--frozen" if options.frozen else "--no-frozen")
        with PickerServer(docs, on_request=requests.append) as picker:
            child_env = env_mod.build_env(
                work,
                picker.url,
                kicad_cli=kicad_cli,
                footprints=footprints,
                hook_log=hook_log,
                offline=options.offline,
            )
            log(f"atopile: ato build -b {build_name} ({', '.join(targets)}) in {work}")
            code, timed_out = proc.run(
                cmd,
                timeout=options.timeout,
                env=child_env,
                cwd=str(work_project),
                log_path=str(ato_log),
            )
        board = work_project / "elec" / "layout" / build_name / f"{build_name}.kicad_pcb"
        if code == 0 and options.outline_margin_mm > 0 and board.is_file():
            outline.frame_file(board, options.outline_margin_mm)
        produced = _outputs(work_project, build_name) if code == 0 else {}
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        copied: Dict[str, str] = {}
        hashes: Dict[str, str] = {}
        for name, path in produced.items():
            dest = out / path.name
            if path.is_dir():
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)
                hashes[name] = _sha256(dest)
            copied[name] = dest.name
        for name in ("ato.log", "hook.jsonl", "catalog.json"):
            if (logs / name).is_file():
                shutil.copy2(logs / name, out / name)
        events = []
        if hook_log.is_file():
            events = [json.loads(line) for line in hook_log.read_text().splitlines() if line]
        board_id = input_id(out / copied["pcb"]) if "pcb" in copied else None
        summary = {
            "atopile": tool.version,
            "atopile_source": tool.source,
            "build": build_name,
            "targets": targets,
            "returncode": code,
            "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1),
            "outputs": copied,
            "sha256": hashes,
            "input_id": board_id,
            "parts_lock": _sha256(project / parts.LOCK_NAME) if lock else None,
            "parts": [e["name"] for e in lock["parts"]] if lock else [],
            "catalog_sha256": _sha256(catalog_snapshot),
            "catalog_parts": sum(len(d["parts"]) for d in docs),
            "stock_footprint_libraries": stock,
            "kicad_cli": kicad.version(kicad_cli) if kicad_cli else None,
            "picker_requests": [
                {"method": r["method"], "path": r["path"].split("?")[0], "status": r["status"]}
                for r in requests
            ],
            "local_parts": sorted({e["lcsc"] for e in events if e.get("event") == "local-part"}),
            "refused": [e for e in events if e.get("event", "").endswith("-refused")],
        }
        (out / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        if options.update_layout and code == 0 and board.is_file():
            dest = project / "elec" / "layout" / build_name / board.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(board, dest)
            log(f"atopile: updated {dest.relative_to(project)}")
        return BuildResult(
            ok=code == 0,
            returncode=code,
            timed_out=timed_out,
            out=out,
            outputs=copied,
            input_id=board_id,
            work=work if options.keep_work else None,
            summary=summary,
        )
    finally:
        if not options.keep_work:
            shutil.rmtree(work, ignore_errors=True)
