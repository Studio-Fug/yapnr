"""``yapnr order stage|vendors`` (docs/fab-and-ordering.md)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yapnr.fab import capability
from yapnr.fab import cli as fab_cli


def _cmd_vendors(args: argparse.Namespace) -> int:
    from yapnr.order import vendors

    rows = []
    for name in capability.names("vendors"):
        doc = capability.vendor(name)
        rows.append(
            {
                "vendor": name,
                "title": doc["title"],
                "country": doc["country"],
                "assembly": bool(doc.get("assembly")),
                "profiles": doc["profiles_by_layers"],
                "mechanisms": vendors.mechanisms(name),
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        layers = ", ".join(f"{k}L {v}" for k, v in sorted(r["profiles"].items()))
        print(
            f"{r['title']} ({r['country']}): {layers}; assembly: {'yes' if r['assembly'] else 'no'}"
        )
        for m in r["mechanisms"]:
            where = f"  {m['url']}" if m["url"] else ""
            print(f"  {m['mechanism']:<20}{m['status']}{where}")
    return 0


def _cmd_stage(args: argparse.Namespace) -> int:
    from yapnr.fab import build
    from yapnr.order import stage

    fab_cli.from_working_directory()
    if bool(args.board) == bool(args.bundle):
        print("yapnr order stage: give a BOARD or --bundle DIR (one of them)", file=sys.stderr)
        return 2
    request = None
    if args.board:
        request = fab_cli._request(args)
    opts = stage.Options(
        vendor=args.vendor,
        board=Path(args.board) if args.board else None,
        bundle=Path(args.bundle) if args.bundle else None,
        build_request=request,
        qty=args.qty,
        service=args.service,
        via=args.via,
        url=args.url,
        verify_url=args.verify_url,
        dry_run=args.dry_run,
        print_only=args.print_only,
        yes=args.yes,
        reveal=args.reveal,
        json=args.json,
    )
    try:
        return stage.stage(opts)
    except (
        stage.StageError,
        build.profiles.ProfileError,
        capability.DataError,
        FileNotFoundError,
        ValueError,
    ) as err:
        print(f"yapnr order stage: {err}", file=sys.stderr)
        return 2


def register(commands) -> None:
    order = commands.add_parser(
        "order", help="stage an order: the card, then the vendor's page (never pays)"
    )
    sub = order.add_subparsers(dest="order_command", metavar="<order command>")
    p = sub.add_parser("vendors", help="vendors, staging mechanisms and their status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_vendors)
    p = sub.add_parser(
        "stage",
        help="build or verify the bundle, print the order card, open the vendor's page",
        description=(
            "Staging only: yapnr prepares the files and takes you to the vendor's upload or "
            "quote page. It never uploads, orders or pays. Agents use --dry-run."
        ),
    )
    p.add_argument("board", nargs="?", help="the routed .kicad_pcb (or --bundle)")
    p.add_argument("--vendor", required=True, choices=fab_cli.VENDORS)
    p.add_argument("--bundle", help="a bundle directory written by yapnr fab build")
    p.add_argument("--profile")
    p.add_argument("--stackup")
    p.add_argument("--qty", type=int)
    p.add_argument("--service", help="e.g. super-swift (OSH Park)")
    p.add_argument("--out", default="yapnr-fab")
    p.add_argument("--name")
    p.add_argument("--allow-draft", action="store_true")
    p.add_argument("--no-project", action="store_true")
    p.add_argument("--kicad-cli")
    p.add_argument("--timeout", type=float, default=600.0)
    asm = p.add_mutually_exclusive_group()
    asm.add_argument("--assembly", action="store_true")
    asm.add_argument("--no-assembly", dest="assembly", action="store_false")
    p.add_argument("--parts-lock")
    p.add_argument("--consign", action="append")
    p.add_argument("--source-date-epoch", type=int)
    p.add_argument("--public", action="store_true")
    p.add_argument(
        "--via",
        default="manual",
        choices=("manual", "import-url"),
        help="manual: open the upload page; import-url: OSH Park fetches a public zip (--url)",
    )
    p.add_argument("--url", help="with --via import-url: the zip's public https URL")
    p.add_argument(
        "--verify-url", action="store_true", help="GET the public zip once and compare its sha256"
    )
    p.add_argument("--dry-run", action="store_true", help="no browser, no network, no log")
    p.add_argument("--print-only", action="store_true", help="print the page and file only")
    p.add_argument("--yes", action="store_true", help="open the page without asking")
    p.add_argument("--reveal", action="store_true", help="also show the zip in the file manager")
    p.add_argument("--json", action="store_true", help="print the card as JSON")
    p.set_defaults(func=_cmd_stage)
    order.set_defaults(func=lambda a: (order.print_help(sys.stderr), 2)[1])
