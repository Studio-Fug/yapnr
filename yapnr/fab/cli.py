"""``yapnr fab profiles|show|check|build|preview`` (docs/fab-and-ordering.md)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from yapnr.fab import capability

VENDORS = ("oshpark", "jlcpcb", "pcbway")


def from_working_directory(environ=None) -> None:
    """Under ``bazel run //:yapnr``, resolve relative paths against the caller's directory."""
    where = (os.environ if environ is None else environ).get("BUILD_WORKING_DIRECTORY")
    if where:
        os.chdir(where)


def _cmd_profiles(args: argparse.Namespace) -> int:
    rows = []
    for name in capability.names("profiles"):
        doc = capability.profile(name)
        rows.append(
            {
                "name": name,
                "status": doc["status"],
                "vendor": doc["vendor"],
                "service": doc["service"],
                "copper_layers": doc["copper_layers"],
                "default_stackup": doc["stackups"]["default"],
                "stackups": doc["stackups"]["allowed"],
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'profile':<14}{'status':<8}{'vendor':<9}{'layers':<8}{'default stackup':<22}service")
    for r in rows:
        print(
            f"{r['name']:<14}{r['status']:<8}{r['vendor']:<9}{r['copper_layers']:<8}"
            f"{str(r['default_stackup']):<22}{r['service']}"
        )
    print("\nlegacy: the fixtures' own rules (engine only, not a vendor profile)")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    if args.sources and not args.name:
        sources = capability.all_sources()
        if args.json:
            print(json.dumps(sources, indent=2))
        else:
            for s in sources:
                print(f"{s['key']:<10}{s['accessed']}  {s['url']}")
        return 0
    if not args.name:
        print("yapnr fab show: name a profile, stackup or vendor (or --sources)", file=sys.stderr)
        return 2
    for kind in capability.KINDS:
        if args.name in capability.names(kind):
            doc = capability.load(kind, args.name)
            break
    else:
        print(f"yapnr fab show: no profile, stackup or vendor {args.name!r}", file=sys.stderr)
        return 2
    if args.sources:
        doc = doc["sources"]
    if args.json or args.sources:
        print(json.dumps(doc, indent=2))
        return 0
    if kind == "stackups":
        from yapnr.fab import stackups

        s = stackups.load(args.name)
        print(f"{s.id}: {s.title} ({s.thickness_mm} mm)\n  {s.summary()}\n  checkout: {s.checkout}")
        try:
            m = s.microstrip("F.Cu")
            print(
                f"  microstrip F.Cu over {m.reference}: h {m.h_mm} mm, Dk {m.er}, Df {m.tan_delta}"
                f" (prior {m.tan_delta_prior}), nominal W50 {m.w50_mm(use_prior=True):.3f} mm"
            )
        except stackups.StackupError as err:
            print(f"  microstrip: {err}")
        return 0
    print(json.dumps(doc, indent=2))
    return 0


def _request(args: argparse.Namespace):
    from yapnr.fab.build import Request

    if not args.vendor and not args.profile:
        raise SystemExit("yapnr fab: --vendor or --profile is required")
    vendor = args.vendor
    if not vendor:
        vendor = capability.profile(args.profile)["vendor"]
    consign: List[str] = []
    for item in getattr(args, "consign", None) or []:
        consign += [r.strip() for r in item.split(",") if r.strip()]
    return Request(
        board=Path(args.board),
        vendor=vendor,
        profile=args.profile,
        stackup=args.stackup,
        qty=args.qty,
        service=getattr(args, "service", None),
        assembly=bool(getattr(args, "assembly", False)),
        parts_lock=Path(args.parts_lock) if getattr(args, "parts_lock", None) else None,
        consign=consign,
        out=Path(args.out),
        name=args.name,
        source_date_epoch=getattr(args, "source_date_epoch", None),
        public=bool(getattr(args, "public", False)),
        allow_draft=args.allow_draft,
        no_project=args.no_project,
        kicad_cli=args.kicad_cli,
        timeout=args.timeout,
        argv=["yapnr"] + list(getattr(args, "_argv", []) or []),
    )


def print_findings(findings, stream=sys.stdout) -> None:
    for f in findings:
        mark = {"error": "ERROR", "warning": "warn ", "info": "note "}[f.severity]
        print(f"  {mark} {f.code:<18} {f.message}", file=stream)


def _cmd_check(args: argparse.Namespace) -> int:
    from yapnr.fab import build, check

    from_working_directory()
    try:
        checked = build.check_board(_request(args))
    except (build.profiles.ProfileError, capability.DataError, FileNotFoundError) as err:
        print(f"yapnr fab check: {err}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([f.to_dict() for f in checked.findings], indent=2))
    else:
        s = check.summary(checked.findings)
        print(
            f"fab check {checked.name} under {checked.profile.name} / {checked.stackup.id}: "
            f"{s['error']} errors, {s['warning']} warnings, {s['info']} notes"
        )
        print_findings(checked.findings)
    return 1 if not checked.ok else 0


def _cmd_build(args: argparse.Namespace) -> int:
    from yapnr.fab import build, check
    from yapnr.order import card as card_mod

    from_working_directory()
    try:
        built = build.build(_request(args))
    except build.BuildStopped as stop:
        if args.json:
            print(json.dumps([f.to_dict() for f in stop.checked.findings], indent=2))
        else:
            print(f"yapnr fab build: {stop}", file=sys.stderr)
            print_findings(check.errors(stop.checked.findings), sys.stderr)
            print(f"  details: {stop.bundle_dir / 'fab-check.json'}", file=sys.stderr)
        return 1
    except (build.profiles.ProfileError, capability.DataError, FileNotFoundError) as err:
        print(f"yapnr fab build: {err}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"bundle": str(built.bundle_dir), "card": built.card}, indent=2))
    else:
        print(card_mod.text(built.card), end="")
        print(f"bundle      {built.bundle_dir}")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    from yapnr.fab import preview

    from_working_directory()
    zip_path = Path(args.zip)
    out = Path(args.out) if args.out else zip_path.with_name(zip_path.stem + "-preview")
    try:
        written = preview.render_zip(zip_path, out)
    except (OSError, ValueError, preview.zipfile.BadZipFile) as err:
        print(f"yapnr fab preview: {err}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    return 0


def _common(p: argparse.ArgumentParser, build: bool) -> None:
    p.add_argument("board", help="the routed .kicad_pcb (its .kicad_pro beside it)")
    p.add_argument("--vendor", choices=VENDORS)
    p.add_argument("--profile", help="vendor profile (default: the vendor's for the layer count)")
    p.add_argument("--stackup", help="stackup id (default: the profile's; required with RF parts)")
    p.add_argument("--qty", type=int, help="board quantity (default: the vendor's minimum)")
    p.add_argument("--out", default="yapnr-fab", help="output directory (default ./yapnr-fab)")
    p.add_argument("--name", help="board name in file names (default: the board file's stem)")
    p.add_argument("--allow-draft", action="store_true", help="accept a draft profile")
    p.add_argument("--no-project", action="store_true", help="check without a .kicad_pro")
    p.add_argument("--kicad-cli", help="the headless kicad-cli (else YAPNR_KICAD_CLI, discovery)")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds per kicad-cli run")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    asm = p.add_mutually_exclusive_group()
    asm.add_argument("--assembly", action="store_true", help="BOM and CPL (JLCPCB, PCBWay)")
    asm.add_argument("--no-assembly", dest="assembly", action="store_false", help="bare boards")
    p.add_argument("--parts-lock", help="yapnr-parts.lock.json: every LCSC id must be locked")
    p.add_argument("--consign", action="append", help="REF,...: parts the vendor does not place")
    if build:
        p.add_argument("--service", help="vendor service (e.g. super-swift at OSH Park)")
        p.add_argument("--source-date-epoch", type=int, help="time stamp for the files")
        p.add_argument("--public", action="store_true", help="refuse private data in the bundle")


def register(commands) -> None:
    fab = commands.add_parser("fab", help="vendor profiles, fab checks and fab bundles")
    sub = fab.add_subparsers(dest="fab_command", metavar="<fab command>")
    p = sub.add_parser("profiles", help="list the vendor profiles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_profiles)
    p = sub.add_parser("show", help="show a profile, stackup or vendor, or every source")
    p.add_argument("name", nargs="?")
    p.add_argument("--sources", action="store_true", help="the sources and their access dates")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_show)
    p = sub.add_parser("check", help="KiCad DRC under the vendor's rules plus the fab checks")
    _common(p, build=False)
    p.set_defaults(func=_cmd_check)
    p = sub.add_parser("build", help="check, then write the vendor bundle")
    _common(p, build=True)
    p.set_defaults(func=_cmd_build)
    p = sub.add_parser(
        "preview",
        help="render a gerber zip to SVG (each layer, top and bottom composites), without KiCad",
    )
    p.add_argument("zip", help="a gerber zip (the file to upload)")
    p.add_argument("--out", help="output directory (default: <zip stem>-preview beside the zip)")
    p.set_defaults(func=_cmd_preview)
    fab.set_defaults(func=lambda a: (fab.print_help(sys.stderr), 2)[1])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="yapnr")
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    args._argv = list(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))
