"""``yapnr atopile build``: one bounded, isolated ``ato build`` (replaces ato_build.sh and
rules_atopile's actions).

For each build the runner:

1. finds the atopile environment (``toolchain.discover``) and the headless KiCad (``kicad``);
2. copies the project into a fresh work directory (offline builds leave sources untouched;
   authoring captures selected parts, and ``--update-layout`` copies the new layout back);
   with ``--files-from`` (the Bazel rule's declared inputs) only the listed files;
3. writes every part of the project's parts lock into the copy, verified against the part cache;
4. writes the stock KiCad footprint libraries the sources reference into the build's
   ``fp-lib-table`` (atopile 0.15.8 resolves ``Library:Footprint`` only there);
5. starts the loopback picker (a thread of this process) on the catalog entries of the locked
   parts plus any ``--catalog`` files;
6. runs ``python -m atopile build`` with an allowlisted environment (``env.py``), the hook on its
   ``PYTHONPATH`` (``hook/``), explicit targets (atopile's implicit ``default`` target and its
   datasheet downloads are excluded) and a deadline that kills the whole process tree;
7. frames the board if asked (``--outline-margin-mm``), then copies the named outputs with
   ``result.json`` to the output directory (default ``./yapnr-out/<project>/<build>``; an
   existing one is replaced only when it holds an earlier build's ``result.json``): the board
   (as ``<build>.kicad_pcb``), BOM, variables, power tree and pinout, never
   ``build/manifest.json`` (it holds absolute paths).

The board, its ``fp-lib-table`` and the outputs are where atopile 0.15.8 puts them for the
project's ``ato.yaml`` (``build_paths``: ``paths.layout``, ``paths.build`` and a build's
``paths.layout``, ``fp_lib_table`` and ``output_base``), not a fixed ``elec/layout/<build>``.

``result.json`` records the input id: the sha256 of the board with every UUID replaced by the nil
UUID, because atopile stamps fresh UUIDs on each build. Two builds of the same sources give the
same id.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
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
from yapnr.frontends.atopile.picker.discovery import RELATIVE as DISCOVERY_PATH
from yapnr.frontends.atopile.picker.discovery import (
    Discovery,
)
from yapnr.frontends.atopile.picker.discovery import encoded as discovery_encoded
from yapnr.frontends.atopile.picker.server import PickerServer
from yapnr.partcache import importer
from yapnr.partcache.client import CacheError, PartCache, open_cache

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
# Left out of the work copy: anywhere in the tree, and (build outputs) at the project's top only.
COPY_IGNORE = (".git", "__pycache__", ".DS_Store")
COPY_IGNORE_TOP = ("build", "manufacturing", "yapnr-out", ".ato")
AUTOSAVE_PATTERNS = ("_autosave-*", "*-save.kicad_pcb")
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
    files: Optional[Sequence[str]] = None


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


def _inside(project: Path, rel: Any, what: str) -> Path:
    """``project / rel``, refused unless it stays inside the project (checked lexically)."""
    if not isinstance(rel, str) or not rel.strip():
        raise BuildError(f"ato.yaml: {what} must be a path")
    path = Path(os.path.normpath(project / rel))
    if path != project and not path.is_relative_to(project):
        raise BuildError(f"ato.yaml: {what} {rel!r} is outside the project")
    return path


def _copy_project(source: Path, dest: Path, files: Optional[Sequence[str]] = None) -> None:
    """Copy the project; with ``files`` (paths relative to it), exactly those."""
    if files is not None:
        dest.mkdir(parents=True)
        for rel in dict.fromkeys(list(files) + ["ato.yaml"]):
            src = _inside(source, rel, "an input")
            if not src.exists():
                raise BuildError(f"input {rel!r} does not exist in {source}")
            target = dest / src.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, symlinks=False, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
        return

    def ignore(directory: str, names: List[str]) -> List[str]:
        skip = [n for n in names if n in COPY_IGNORE]
        if Path(directory) == source:
            skip += [n for n in names if n in COPY_IGNORE_TOP]
        return skip

    shutil.copytree(source, dest, ignore=ignore, symlinks=False)
    modules = source / ".ato" / "modules"
    if modules.is_dir():  # installed atopile packages: needed offline; atopile's caches are not
        shutil.copytree(modules, dest / ".ato" / "modules", symlinks=False)


def _ato_yaml(project: Path) -> Dict[str, Any]:
    text = (project / "ato.yaml").read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        if re.search(r"^\s*(paths|layout|build|output_base|fp_lib_table)\s*:", text, re.M):
            raise BuildError("ato.yaml sets paths: reading it needs PyYAML")
        return {}
    doc = yaml.safe_load(text) or {}
    return doc if isinstance(doc, dict) else {}


def _find_layout(base: Path) -> Path:
    """atopile 0.15.8's ``BuildTargetPaths.find_layout``."""
    if base.with_suffix(".kicad_pcb").exists():
        return base.with_suffix(".kicad_pcb")
    if base.is_dir():
        found = [
            path
            for path in sorted(base.glob("*.kicad_pcb"))
            if not any(fnmatch.fnmatch(path.name, p) for p in AUTOSAVE_PATTERNS)
        ]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise BuildError(f"{base} holds {len(found)} layouts; atopile needs exactly one")
    return base / f"{base.name}.kicad_pcb"


@dataclass
class BuildPaths:
    layout: Path  # the board atopile reads and writes
    fp_lib_table: Path
    output_base: Path  # outputs are <output_base>.<suffix> and siblings of it


def build_paths(project: Path, build: str) -> BuildPaths:
    """Where atopile 0.15.8 keeps a build's board and outputs (``atopile/config.py``)."""
    project = Path(project)
    doc = _ato_yaml(project)
    top = doc.get("paths") or {}
    own = ((doc.get("builds") or {}).get(build) or {}).get("paths") or {}
    if not isinstance(top, dict) or not isinstance(own, dict):
        raise BuildError("ato.yaml: paths must be a mapping")
    if own.get("layout"):
        base = _inside(project, own["layout"], f"builds.{build}.paths.layout")
    else:
        base = _inside(project, top.get("layout", "elec/layout"), "paths.layout") / build
    layout = _find_layout(base)
    if own.get("fp_lib_table"):
        table = _inside(project, own["fp_lib_table"], f"builds.{build}.paths.fp_lib_table")
    else:
        table = layout.parent / "fp-lib-table"
    if own.get("output_base"):
        output_base = _inside(project, own["output_base"], f"builds.{build}.paths.output_base")
    else:
        build_dir = _inside(project, top.get("build", "build"), "paths.build")
        output_base = build_dir / "builds" / build / build
    return BuildPaths(layout=layout, fp_lib_table=table, output_base=output_base)


def _outputs(paths: BuildPaths) -> Dict[str, Path]:
    base, stem = paths.output_base.parent, paths.output_base.name
    candidates = {
        "pcb": paths.layout,
        "bom_csv": base / f"{stem}.bom.csv",
        "bom_json": base / f"{stem}.bom.json",
        "variables": base / f"{stem}.variables.ato.json",
        "power_tree": base / f"{stem}.power_tree.ato.json",
        "power_tree_md": base / "power_tree.md",
        "pinout": base / "pinout",
        "stackup": base / f"{stem}.stackup.json",
        "gerbers": base / f"{stem}.gerber.zip",
        "pick_and_place": base / f"{stem}.pick_and_place.csv",
        "step": base / f"{stem}.pcba.step",
        "glb": base / f"{stem}.pcba.glb",
    }
    return {name: path for name, path in candidates.items() if path.exists()}


def _output_name(name: str, path: Path, paths: BuildPaths, build: str) -> str:
    """Outputs are named after the build: ``<build>.kicad_pcb``, ``<build>.bom.csv``, ..."""
    if name == "pcb":
        return f"{build}.kicad_pcb"
    stem = paths.output_base.name
    if path.name.startswith(stem + "."):
        return build + path.name[len(stem) :]
    return path.name


def _check_out(out: Path, project: Path) -> None:
    """Refuse an output directory whose replacement would lose anything but an earlier output."""
    if out == project or project.is_relative_to(out):
        raise BuildError(f"--out {out} would replace the project itself")
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"--out {out} is not a directory")
        if any(out.iterdir()) and not (out / "result.json").is_file():
            raise BuildError(
                f"--out {out} is not empty and holds no earlier build (result.json); refusing to"
                " replace it"
            )


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
    if options.frozen and not options.offline:
        raise BuildError("Frozen builds must use captured offline parts; remove --online")
    if options.stock_footprints not in ("referenced", "all", "none"):
        raise BuildError("--stock-footprints must be referenced, all or none")
    out = Path(options.out or Path("yapnr-out") / project.name / build_name).resolve()
    _check_out(out, project)

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
    article_cache = _inside(project, ".yapnr/parts/cache", "captured part cache")
    # Captured article inputs take precedence over an operator cache, which may not
    # contain parts selected in a later authoring build.
    cache_location = str(article_cache) if article_cache.is_dir() else options.cache
    cache = open_cache(cache_location) if (lock and lock["parts"]) or cache_location else None

    work_root = Path(options.work_root) if options.work_root else None
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"yapnr-atopile-{build_name}-", dir=work_root))
    started = time.monotonic()
    try:
        work_project = work / "project"
        _copy_project(project, work_project, options.files)
        paths = build_paths(work_project, build_name)
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
            stock = kicad.write_fp_lib_table(
                paths.fp_lib_table,
                footprints,
                work_project / "elec",
                options.stock_footprints == "all",
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
        try:
            discovery = Discovery(project, online=not options.offline, python=tool.python)
        except (OSError, ValueError, KeyError) as error:
            raise BuildError("Cannot read captured component queries: " + str(error)) from error
        with PickerServer(docs, on_request=requests.append, resolve=discovery.answer) as picker:
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
        board = paths.layout
        if code == 0 and options.outline_margin_mm > 0 and board.is_file():
            outline.frame_file(board, options.outline_margin_mm)
        produced = _outputs(paths) if code == 0 else {}
        _check_out(out, project)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        copied: Dict[str, str] = {}
        hashes: Dict[str, str] = {}
        for name, path in produced.items():
            dest = out / _output_name(name, path, paths, build_name)
            if path.is_dir():
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)
                hashes[name] = _sha256(dest)
            copied[name] = dest.name
        native_returncode = code
        capture_errors = []
        if not options.offline:
            try:
                selected_parts = work_project / parts.parts_dir_of(work_project)
                directories = (
                    importer.part_dirs_under(selected_parts) if selected_parts.is_dir() else []
                )
                if directories:
                    captured_cache = open_cache(article_cache, create=True)
                    importer.import_part_dirs(
                        captured_cache, directories, "yapnr on-demand component discovery"
                    )
                    captured_lock = parts.lock_directory(work_project, cache=captured_cache)
                    wanted = {entry.get("lcsc") for entry in captured_lock["parts"]}
                    for document in docs:
                        selected_catalog = {
                            **document,
                            "parts": [part for part in document["parts"] if part["lcsc"] in wanted],
                        }
                        if selected_catalog["parts"]:
                            importer.import_catalog(captured_cache, selected_catalog)
                    parts.materialize_lock(
                        project, captured_lock, captured_cache, replace=options.replace_parts
                    )
                    (project / parts.LOCK_NAME).write_text(
                        parts.dump(captured_lock), encoding="utf-8"
                    )
                    lock = captured_lock
            except (OSError, ValueError, CacheError, importer.ImportFailed) as error:
                capture_errors.append("Selected part inputs could not be captured: " + str(error))
                code = code or 1
        discovery_snapshot = logs / "discovery.json"
        discovery_snapshot.write_bytes(discovery_encoded(discovery.document))
        for name in ("ato.log", "hook.jsonl", "catalog.json", "discovery.json"):
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
            "native_returncode": native_returncode,
            "part_capture_errors": capture_errors,
            "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1),
            "outputs": copied,
            "sha256": hashes,
            "input_id": board_id,
            "layout": board.relative_to(work_project).as_posix(),
            "parts_lock": _sha256(project / parts.LOCK_NAME) if lock else None,
            "parts": [e["name"] for e in lock["parts"]] if lock else [],
            "catalog_sha256": _sha256(catalog_snapshot),
            "catalog_parts": len(
                {p["lcsc"] for d in docs for p in d["parts"]}
                | {
                    "C" + str(p["lcsc"])
                    for entry in discovery.document["queries"].values()
                    for batch in entry["response"].get("results", [entry["response"]])
                    for p in batch.get("components", [])
                }
            ),
            "component_discovery": {
                "snapshot": DISCOVERY_PATH,
                "sha256": _sha256(discovery_snapshot),
                "remote_requests": discovery.remote_queries,
                "replayed_requests": discovery.replayed_queries,
                "failures": discovery.failures,
                "offline": options.offline,
            },
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
            dest = project / board.relative_to(work_project)
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
