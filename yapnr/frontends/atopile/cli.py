"""``yapnr atopile ...`` and ``yapnr picker ...`` (registered by yapnr/cli.py)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yapnr.frontends.atopile import ATOPILE_VERSION


def _err(text: str) -> int:
    print(f"yapnr: {text}", file=sys.stderr)
    return 1


# --- yapnr atopile ----------------------------------------------------------------------------


def _cmd_setup(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile import toolchain

    try:
        env_dir = toolchain.setup(
            root=Path(args.root) if args.root else None,
            uv=args.uv,
            wheel=Path(args.wheel) if args.wheel else None,
            force=args.force,
            any_uv=args.any_uv,
        )
    except toolchain.ToolchainError as err:
        return _err(str(err))
    print(toolchain.venv_python(env_dir / "venv"))
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile import kicad, toolchain

    report = toolchain.status()
    try:
        cli = kicad.find_cli()
        report["kicad_cli"] = str(cli)
        report["kicad_footprints"] = str(kicad.find_footprints(cli))
    except kicad.Unavailable as err:
        report["kicad_cli"] = None
        report["kicad_error"] = str(err)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key in sorted(report):
            print(f"{key:18} {report[key]}")
    return 0 if report.get("ok") else 1


def _cmd_build(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile import runner, toolchain
    from yapnr.frontends.atopile.picker.catalog import CatalogError
    from yapnr.partcache.client import CacheError
    from yapnr.partcache.store import NotFound

    options = runner.BuildOptions(
        project=Path(args.project),
        build=args.build,
        targets=args.target or runner.DEFAULT_TARGETS,
        out=Path(args.out) if args.out else None,
        cache=args.cache,
        catalogs=[Path(c) for c in args.catalog],
        timeout=args.timeout,
        outline_margin_mm=args.outline_margin_mm,
        frozen=args.frozen,
        update_layout=args.update_layout,
        keep_work=args.keep_work,
        work_root=Path(args.work_root) if args.work_root else None,
        stock_footprints=args.stock_footprints,
        kicad_cli=args.kicad_cli,
        replace_parts=args.replace_parts,
        offline=not args.online,
    )
    try:
        result = runner.build(options, log=lambda text: print(text, file=sys.stderr))
    except (runner.BuildError, toolchain.ToolchainError, CatalogError, CacheError) as err:
        return _err(str(err))
    except NotFound as err:
        return _err(f"not in the part cache: {err}")
    state = "ok" if result.ok else ("TIMED OUT" if result.timed_out else "FAILED")
    print(f"atopile build {state}: {result.out} (input id {result.input_id})")
    if not result.ok:
        print(f"see {result.out / 'ato.log'}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_lock_parts(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile import parts
    from yapnr.partcache.client import open_cache

    project = Path(args.project)
    cache = open_cache(args.cache, create=args.upload) if (args.cache or args.upload) else None
    try:
        doc = parts.lock_directory(
            project,
            cache=cache,
            upload=args.upload,
            imported_from=args.imported_from or project.name,
            licence_note=args.licence_note,
        )
    except (parts.LockError, KeyError, ValueError) as err:
        return _err(str(err))
    output = Path(args.output) if args.output else project / parts.LOCK_NAME
    output.write_text(parts.dump(doc), encoding="utf-8")
    print(f"locked {len(doc['parts'])} parts in {output}")
    return 0


def _cmd_materialize(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile import parts
    from yapnr.partcache.client import CacheError, open_cache

    project = Path(args.project)
    lock = parts.load(project)
    if lock is None:
        return _err(f"{project} has no {parts.LOCK_NAME}")
    try:
        written = parts.materialize_lock(project, lock, open_cache(args.cache), args.replace)
    except CacheError as err:
        return _err(str(err))
    print(f"{len(written)} parts in {project / lock['parts_dir']}")
    return 0


def register_atopile(commands: "argparse._SubParsersAction") -> None:
    top = commands.add_parser(
        "atopile", help=f"the atopile {ATOPILE_VERSION} toolchain (setup, build, parts)"
    )
    sub = top.add_subparsers(dest="atopile_command", metavar="<command>", required=True)

    setup = sub.add_parser("setup", help="create the hash-pinned atopile environment (needs uv)")
    setup.add_argument("--root", help="environments directory (default: ~/.cache/yapnr/atopile)")
    setup.add_argument("--uv", help="the uv executable (default: uv on PATH)")
    setup.add_argument("--wheel", help="an atopile wheel built by tools/atopile/build_wheel.sh")
    setup.add_argument("--force", action="store_true", help="rebuild the environment")
    setup.add_argument("--any-uv", action="store_true", help="accept another uv version")
    setup.set_defaults(func=_cmd_setup)

    info = sub.add_parser("info", help="report the atopile environment and KiCad found")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=_cmd_info)

    build = sub.add_parser("build", help="build an atopile project offline, in isolation")
    build.add_argument("project", help="the directory holding ato.yaml")
    build.add_argument("--build", "-b", default="default", help="the ato.yaml build")
    build.add_argument("--target", "-t", action="append", help="build target (repeatable)")
    build.add_argument("--out", help="output directory (default: <project>/yapnr-out/<build>)")
    build.add_argument("--cache", help="part cache: a directory or http(s) URL")
    build.add_argument("--catalog", action="append", default=[], help="extra picker catalog")
    build.add_argument("--timeout", type=float, default=1800.0, help="seconds (default 1800)")
    build.add_argument("--outline-margin-mm", type=float, default=0.0, help="frame the board")
    build.add_argument("--frozen", action="store_true", help="the layout must not change")
    build.add_argument("--update-layout", action="store_true", help="copy the new layout back")
    build.add_argument("--keep-work", action="store_true", help="keep the work directory")
    build.add_argument("--work-root", help="where work directories go (default: temp)")
    build.add_argument(
        "--stock-footprints",
        choices=("referenced", "all", "none"),
        default="referenced",
        help="stock KiCad libraries in the build's fp-lib-table",
    )
    build.add_argument("--kicad-cli", help="the headless kicad-cli")
    build.add_argument("--replace-parts", action="store_true", help="overwrite differing parts")
    build.add_argument(
        "--online",
        action="store_true",
        help="let atopile fetch parts that are not locked from EasyEDA (not reproducible)",
    )
    build.set_defaults(func=_cmd_build)

    lock = sub.add_parser("lock-parts", help="write yapnr-parts.lock.json for the project's parts")
    lock.add_argument("project")
    lock.add_argument("--cache", help="part cache to check against or upload to")
    lock.add_argument("--upload", action="store_true", help="add the parts to the cache first")
    lock.add_argument("--imported-from", help="provenance note for uploaded parts")
    lock.add_argument("--licence-note", help="added to each uploaded part's licence note")
    lock.add_argument("--output", help="write the lock here instead of the project")
    lock.set_defaults(func=_cmd_lock_parts)

    mat = sub.add_parser("materialize", help="write the locked parts into the project's tree")
    mat.add_argument("project")
    mat.add_argument("--cache", help="part cache: a directory or http(s) URL")
    mat.add_argument("--replace", action="store_true", help="overwrite differing part dirs")
    mat.set_defaults(func=_cmd_materialize)


# --- yapnr picker -----------------------------------------------------------------------------


def _cmd_picker_serve(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile.picker import server

    argv = ["--host", args.host, "--port", str(args.port)]
    for path in args.catalog:
        argv += ["--catalog", path]
    if args.port_file:
        argv += ["--port-file", args.port_file]
    return server.main(argv)


def _cmd_catalog_import(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile.picker import catalog

    try:
        doc = catalog.from_splanc(
            json.loads(Path(args.source).read_text(encoding="utf-8")),
            source=args.source_note or Path(args.source).name,
            retrieved=args.retrieved,
        )
    except (catalog.CatalogError, KeyError, ValueError) as err:
        return _err(str(err))
    Path(args.output).write_text(catalog.dump(doc), encoding="utf-8")
    print(f"wrote {len(doc['parts'])} parts to {args.output}")
    return 0


def _cmd_catalog_validate(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile.picker import catalog

    status = 0
    for path in args.catalogs:
        try:
            doc = catalog.load(path)
            print(f"{path}: {len(doc['parts'])} parts, ok")
        except (catalog.CatalogError, OSError) as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


def register_picker(commands: "argparse._SubParsersAction") -> None:
    top = commands.add_parser("picker", help="the offline atopile part picker and its catalogs")
    sub = top.add_subparsers(dest="picker_command", metavar="<command>", required=True)

    serve = sub.add_parser("serve", help="serve catalogs on the loopback interface")
    serve.add_argument("--catalog", action="append", default=[], help="catalog file")
    serve.add_argument("--host", default="127.0.0.1", help="a loopback address")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--port-file")
    serve.set_defaults(func=_cmd_picker_serve)

    cat = sub.add_parser("catalog", help="catalog files")
    cat_sub = cat.add_subparsers(dest="catalog_command", metavar="<command>", required=True)
    imp = cat_sub.add_parser("import", help="convert a Splanc picker catalog to schema v1")
    imp.add_argument("source")
    imp.add_argument("-o", "--output", required=True)
    imp.add_argument("--source-note", help="provenance source (default: the file name)")
    imp.add_argument("--retrieved", default=None, help="date the facts were collected")
    imp.set_defaults(func=_cmd_catalog_import)
    val = cat_sub.add_parser("validate", help="check catalog files against the schema")
    val.add_argument("catalogs", nargs="+")
    val.set_defaults(func=_cmd_catalog_validate)
