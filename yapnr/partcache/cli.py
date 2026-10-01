"""``yapnr part-cache ...`` (registered by yapnr/cli.py). See docs/part-cache.md."""

from __future__ import annotations

import argparse
import functools
import json
import os
import secrets
import sys
from pathlib import Path


def _err(text: str) -> int:
    print(f"yapnr: {text}", file=sys.stderr)
    return 1


def _cache(args: argparse.Namespace, create: bool = False):
    from yapnr.partcache.client import open_cache

    return open_cache(args.cache, create=create)


def _local_store(args: argparse.Namespace):
    from yapnr.partcache.client import ENV_LOCATION, LocalPartCache, default_root

    location = args.cache or os.environ.get(ENV_LOCATION) or str(default_root())
    if location.startswith(("http://", "https://")):
        raise SystemExit(_err("this command works on a local cache directory only"))
    return LocalPartCache(location).store


def _cmd_init(args: argparse.Namespace) -> int:
    cache = _cache(args, create=True)
    print(f"part cache at {cache.location}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from yapnr.partcache import server

    return server.serve(args)


def _cmd_import_parts(args: argparse.Namespace) -> int:
    from yapnr.partcache import importer

    cache = _cache(args, create=True)
    dirs = []
    for path in args.dirs:
        path = Path(path)
        dirs.extend(
            [path] if (path / f"{path.name}.ato").is_file() else importer.part_dirs_under(path)
        )
    failures = []
    try:
        records = importer.import_part_dirs(
            cache,
            dirs,
            args.imported_from,
            licence_note=args.licence_note,
            notes=args.notes,
            local_only=args.local_only,
        )
    except importer.ImportFailed as err:
        records, failures = err.imported, err.failures
    for record in records:
        print(f"{record['id'][:12]}  {record['name']}  {record.get('lcsc') or '-'}")
        for warning in record.get("warnings", []):
            print(f"  warning: {warning}", file=sys.stderr)
    for name, reason in failures:
        print(f"refused {name}: {reason}", file=sys.stderr)
    print(
        f"{len(records)} parts imported, {len(failures)} refused, into {cache.location}",
        file=sys.stderr,
    )
    return 1 if failures else 0


def _cmd_import_catalog(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile.picker import catalog
    from yapnr.partcache import importer

    cache = _cache(args, create=True)
    try:
        if args.splanc:
            doc = catalog.from_splanc(
                json.loads(Path(args.catalog).read_text(encoding="utf-8")),
                source=args.source_note or Path(args.catalog).name,
                retrieved=args.retrieved,
            )
        else:
            doc = catalog.load(args.catalog)
    except (catalog.CatalogError, KeyError, ValueError) as err:
        return _err(str(err))
    count = importer.import_catalog(cache, doc)
    print(f"{count} catalog entries in {cache.location}")
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    parts = _cache(args).find(
        lcsc=args.lcsc,
        manufacturer=args.manufacturer,
        mpn=args.mpn,
        text=args.query,
        latest_only=not args.all_versions,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(parts, indent=2))
    else:
        for part in parts:
            print(
                f"{part['id'][:12]}  {part['name']:48} {part.get('lcsc') or '-':>10}  {part['licence']}"
            )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from yapnr.partcache import model

    cache = _cache(args)
    key = args.part
    manifest = cache.manifest(key) if model.is_sha256(key) else cache.current(key)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _cmd_materialize(args: argparse.Namespace) -> int:
    from yapnr.partcache import model
    from yapnr.partcache.client import CacheError, materialize

    cache = _cache(args)
    key = args.part
    pid = key if model.is_sha256(key) else cache.current(key)["id"]
    try:
        print(materialize(cache, pid, Path(args.into), replace=args.replace))
    except CacheError as err:
        return _err(str(err))
    return 0


def _cmd_catalog(args: argparse.Namespace) -> int:
    from yapnr.frontends.atopile.picker import catalog

    doc = _cache(args).catalog(args.lcsc or None)
    text = catalog.dump(catalog.validate(doc))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {len(doc['parts'])} entries to {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    block = args.block if args.block in ("unshared", "none") else args.block.split(",")
    result = _cache(args).delete_part(args.part_id, args.reason, block=block)
    print(json.dumps(result))
    return 0


def _cmd_distribution(args: argparse.Namespace) -> int:
    manifest = _local_store(args).set_distribution(args.part_id, args.distribution)
    print(f"{manifest['id'][:12]}  {manifest['name']}  {manifest['licence']['distribution']}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = _local_store(args).verify()
    for problem in problems:
        print(problem, file=sys.stderr)
    print("ok" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


def _cmd_gc(args: argparse.Namespace) -> int:
    print(f"removed {_local_store(args).gc()} unreferenced blobs")
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    print(json.dumps(_local_store(args).reindex()))
    return 0


def _cmd_token(args: argparse.Namespace) -> int:
    from yapnr.partcache.server import hash_token

    token = "ypc_" + secrets.token_urlsafe(32)
    print(f"token (give it to the client as YAPNR_PART_CACHE_TOKEN; it is not stored): {token}")
    print("line for the server's token file:")
    print(f"{hash_token(token)} {args.scope} {args.label}")
    return 0


def _reporting_errors(func):
    """Report the cache's expected failures as one line instead of a traceback."""

    @functools.wraps(func)
    def run(args: argparse.Namespace) -> int:
        from yapnr.partcache.client import CacheError
        from yapnr.partcache.model import InvalidPart
        from yapnr.partcache.store import NotFound, TakenDown

        try:
            return func(args)
        except NotFound as err:
            return _err(f"not found: {err.args[0] if err.args else err}")
        except (CacheError, InvalidPart, TakenDown, FileExistsError, ValueError) as err:
            return _err(str(err))

    return run


def register(commands: "argparse._SubParsersAction") -> None:
    from yapnr.partcache import server

    top = commands.add_parser("part-cache", help="the part cache (local directory or server)")
    sub = top.add_subparsers(dest="cache_command", metavar="<command>", required=True)

    def add(name: str, func, help_text: str, cache: bool = True) -> argparse.ArgumentParser:
        parser = sub.add_parser(name, help=help_text)
        func = _reporting_errors(func)
        if cache:
            parser.add_argument(
                "--cache",
                help="cache directory or http(s) URL (default: $YAPNR_PART_CACHE, then"
                " ~/.local/share/yapnr/part-cache)",
            )
        parser.set_defaults(func=func)
        return parser

    add("init", _cmd_init, "create a local cache directory")
    serve = add("serve", _cmd_serve, "serve a cache directory over HTTP", cache=False)
    server.add_serve_arguments(serve)

    imp = add("import-parts", _cmd_import_parts, "add atopile part directories")
    imp.add_argument("dirs", nargs="+", help="part directories, or directories of them")
    imp.add_argument("--imported-from", required=True, help="where the parts come from")
    imp.add_argument("--licence-note", help="appended to each part's licence note")
    imp.add_argument("--notes", help="provenance notes")
    imp.add_argument(
        "--local-only",
        action="store_true",
        help="mark the parts local-only: never uploaded to, or served by, a shared server",
    )

    cat = add("import-catalog", _cmd_import_catalog, "add catalog entries from a catalog file")
    cat.add_argument("catalog")
    cat.add_argument("--splanc", action="store_true", help="the file is in Splanc's layout")
    cat.add_argument("--source-note", help="provenance source (default: the file name)")
    cat.add_argument("--retrieved", help="date the facts were collected")

    find = add("find", _cmd_find, "list parts")
    find.add_argument("--lcsc")
    find.add_argument("--manufacturer")
    find.add_argument("--mpn")
    find.add_argument("--query", "-q", help="substring of name, part number, maker or LCSC id")
    find.add_argument("--all-versions", action="store_true")
    find.add_argument("--limit", type=int, default=100)
    find.add_argument("--json", action="store_true")

    show = add("show", _cmd_show, "print a part manifest (by id or LCSC id)")
    show.add_argument("part")

    mat = add("materialize", _cmd_materialize, "write a part directory (verified)")
    mat.add_argument("part", help="part id or LCSC id (newest version)")
    mat.add_argument("--into", required=True, help="the parts directory to write into")
    mat.add_argument("--replace", action="store_true")

    catalog = add("catalog", _cmd_catalog, "export the catalog (yapnr-picker-catalog-v1)")
    catalog.add_argument("--lcsc", action="append", help="only these LCSC ids")
    catalog.add_argument("-o", "--output")

    delete = add("delete", _cmd_delete, "take a part down (admin)")
    delete.add_argument("part_id")
    delete.add_argument("--reason", required=True)
    delete.add_argument(
        "--block",
        default="unshared",
        help="files refused from now on: 'unshared' (default: every file no other part uses),"
        " 'none', or comma-separated file names of the part",
    )

    dist = add("set-distribution", _cmd_distribution, "mark a part shareable or local-only")
    dist.add_argument("part_id")
    dist.add_argument("distribution", choices=("shareable", "local-only"))

    add("verify", _cmd_verify, "re-hash every file of a local cache")
    add("gc", _cmd_gc, "remove unreferenced files of a local cache")
    add("reindex", _cmd_reindex, "rebuild a local cache's index from its files")

    token = sub.add_parser("token", help="make a server token and its token-file line")
    token.add_argument("--scope", choices=("read", "write", "admin"), required=True)
    token.add_argument("--label", required=True, help="who or what the token is for")
    token.set_defaults(func=_cmd_token)
