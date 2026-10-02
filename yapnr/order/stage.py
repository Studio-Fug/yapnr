"""``yapnr order stage``: validate, build, print the order card, then take the human to the page.

Design §8.1. Steps:

1. **Validate and build**: ``yapnr fab build`` on the board (its fab check stops on errors), or
   ``--bundle DIR`` re-checks a built bundle's manifest hashes instead.
2. **The card** with this run's choices (quantity, service, mechanism) is printed and written
   into the bundle as ``order-card.staged.{md,json}``.
3. **Stage**: by default ``Open <url> in your browser? [y/N]`` (``--yes`` answers yes for
   opening a page, nothing else); ``--print-only`` prints the page and the file; ``--reveal``
   also shows the zip in the file manager.
4. **Log** one line to ``staged.jsonl`` in the bundle: time, vendor, mechanism, URL, the zip's
   sha256 and whether a page was opened. No tokens, no response bodies.

**Dry run** (``--dry-run``, or forced by ``CI`` or ``YAPNR_ORDER_DRY_RUN=1``) does steps 1 and 2,
prints what step 3 would do and exits 0: no browser, no network, no log. Agents run ``order
stage`` only with ``--dry-run`` (AGENTS.md).

Opening a page hands a URL to the human's browser; yapnr itself fetches nothing (``--verify-url``
excepted, see ``yapnr.order.net``). It never uploads, orders, pays or logs in.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TextIO

from yapnr.fab import bundle as bundle_mod
from yapnr.order import card as card_mod
from yapnr.order import net, vendors

DRY_RUN_ENV = "YAPNR_ORDER_DRY_RUN"


class StageError(RuntimeError):
    """Staging cannot go on; the message says why (nothing was opened)."""


@dataclass
class Options:
    vendor: str
    board: Optional[Path] = None
    bundle: Optional[Path] = None
    build_request: Any = None  # yapnr.fab.build.Request when staging from a board
    qty: Optional[int] = None
    service: Optional[str] = None
    via: str = "manual"
    url: Optional[str] = None
    verify_url: bool = False
    dry_run: bool = False
    print_only: bool = False
    yes: bool = False
    reveal: bool = False
    json: bool = False


def dry_run_forced(environ=None) -> Optional[str]:
    """The reason a dry run is forced (``CI`` or ``YAPNR_ORDER_DRY_RUN=1``), else None."""
    env = os.environ if environ is None else environ
    if env.get(DRY_RUN_ENV, "").strip() == "1":
        return f"{DRY_RUN_ENV}=1"
    ci = env.get("CI", "").strip().lower()
    if ci and ci not in ("0", "false", "no"):
        return "CI is set"
    return None


def load_bundle(directory: Path):
    """(card, manifest) of a built bundle, after checking every file's sha256."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        card = json.loads((directory / "order-card.json").read_text())
    except (OSError, ValueError) as err:
        raise StageError(f"{directory} is not a yapnr fab bundle ({err})") from None
    if manifest.get("schema") != bundle_mod.MANIFEST_SCHEMA:
        raise StageError(f"{directory}/manifest.json: unknown schema")
    for entry in manifest.get("files", []):
        path = directory / entry["name"]
        if not path.is_file():
            raise StageError(f"bundle file missing: {entry['name']}")
        if bundle_mod.sha256_file(path) != entry["sha256"]:
            raise StageError(f"bundle file changed since the build: {entry['name']}")
    if card["files"]["upload"]["sha256"] != manifest["gerber_zip"]["sha256"]:
        raise StageError("the order card and the manifest name different zips")
    return card, manifest


def _apply_choices(card: Dict[str, Any], opts: Options) -> Dict[str, Any]:
    """The card with this run's quantity and service, the quantity rule re-checked."""
    from yapnr.fab import build, profiles

    card = copy.deepcopy(card)
    profile = profiles.load(card["profile"]["name"])
    if opts.qty is not None:
        rule = profile.limits.get("qty") or {}
        if opts.qty < rule.get("min", 1) or opts.qty % rule.get("multiple", 1):
            raise StageError(
                f"FAB-QTY: quantity {opts.qty}: {card['vendor']['title']} takes {card['qty']['rule']}"
                f" (at least {rule.get('min')})"
            )
        card["qty"]["value"] = opts.qty
    if opts.service is not None:
        card["service"] = build._service(profile, opts.service)
    card["estimate"] = build.estimate(
        profile, card["board"]["area_sq_in"], card["qty"]["value"] or 0, card["service"]["id"]
    )
    return card


def _staging(card: Dict[str, Any], opts: Options) -> Dict[str, Any]:
    zip_name = card["files"]["upload"]["name"]
    if opts.via == "manual":
        page = vendors.manual_page(opts.vendor, assembly=bool(card.get("assembly")))
        return {
            "mechanism": "manual",
            "url": page["url"],
            "steps": vendors.steps(page, zip_name, bool(card.get("assembly"))),
        }
    if opts.via == "import-url":
        if not opts.url:
            raise StageError("--via import-url needs --url <public https URL of the zip>")
        link = vendors.import_link(opts.vendor, opts.url)
        from yapnr.fab import capability

        spec = capability.vendor(opts.vendor)["staging"]["import_url"]
        return {
            "mechanism": "import-url",
            "url": link,
            "public_zip": opts.url,
            "steps": vendors.steps(spec, zip_name, bool(card.get("assembly"))),
        }
    raise StageError(f"unknown mechanism {opts.via!r}: {vendors.NOT_BUILT.get(opts.via, '')}")


def _reveal(path: Path) -> None:
    from yapnr.frontends.atopile import proc

    if sys.platform == "darwin":
        proc.run(["open", "-R", str(path)], timeout=30)
    elif sys.platform.startswith("linux"):
        proc.run(["xdg-open", str(path.parent)], timeout=30)


def stage(
    opts: Options,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
    open_page: Optional[Callable[[str], bool]] = None,
    ask: Callable[[str], str] = input,
    is_tty: Optional[Callable[[], bool]] = None,
    environ=None,
    verify: Callable[..., str] = net.verify_url,
    reveal: Callable[[Path], None] = _reveal,
) -> int:
    """Run ``yapnr order stage``; returns the exit code."""
    out = out or sys.stdout
    err = err or sys.stderr
    open_page = open_page or webbrowser.open
    forced = dry_run_forced(environ)
    dry_run = opts.dry_run or forced is not None
    # 1. Validate and build (or verify a built bundle).
    if opts.bundle is not None:
        bundle_dir = Path(opts.bundle).resolve()
        card, _ = load_bundle(bundle_dir)
        if card["vendor"]["id"] != opts.vendor:
            raise StageError(f"the bundle is for {card['vendor']['title']}, not {opts.vendor}")
    else:
        from yapnr.fab import build

        try:
            built = build.build(opts.build_request)
        except build.BuildStopped as stop:
            from yapnr.fab.check import errors
            from yapnr.fab.cli import print_findings

            print(f"yapnr order stage: {stop}", file=err)
            print_findings(errors(stop.checked.findings), err)
            return 1
        bundle_dir, card = built.bundle_dir, built.card
    zip_path = bundle_dir / card["files"]["upload"]["name"]

    # 2. The card with this run's choices.
    card = _apply_choices(card, opts)
    card["staging"] = _staging(card, opts)
    card["run"] = {"dry_run": dry_run, "mechanism": card["staging"]["mechanism"]}
    (bundle_dir / "order-card.staged.json").write_text(card_mod.to_json(card))
    (bundle_dir / "order-card.staged.md").write_text(card_mod.markdown(card))
    if opts.json:
        print(card_mod.to_json(card), end="", file=out)
    else:
        print(card_mod.text(card), end="", file=out)
        print(f"{'file path':<12}{zip_path}", file=out)
    url = card["staging"]["url"]

    # 3. Stage.
    if dry_run:
        why = f" ({forced})" if forced and not opts.dry_run else ""
        if card["staging"]["mechanism"] == "import-url" and opts.verify_url:
            print(f"DRY RUN{why}: would verify {opts.url} against the zip's sha256", file=out)
        print(
            f"DRY RUN{why}: would open {url}\n"
            "Nothing was opened, nothing was uploaded and no network connection was made.",
            file=out,
        )
        return 0
    if card["staging"]["mechanism"] == "import-url" and opts.verify_url:
        try:
            verify(opts.url, card["files"]["upload"]["sha256"])
        except net.VerifyError as error:
            print(f"yapnr order stage: {error}", file=err)
            return 1
        print(f"verified    {opts.url} is this bundle's zip", file=out)
    opened = False
    if opts.print_only:
        print(f"open        {url}", file=out)
    else:
        tty = is_tty() if is_tty is not None else sys.stdin.isatty()
        answer = "y" if opts.yes else ""
        if not opts.yes and tty:
            answer = ask(f"Open {url} in your browser? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            opened = bool(open_page(url))
            if not opened:
                print(f"could not open a browser; open {url}", file=out)
        else:
            print(f"open        {url}", file=out)
    if opts.reveal:
        reveal(zip_path)
    # 4. Log (no tokens, no response bodies).
    entry = {
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vendor": opts.vendor,
        "mechanism": card["staging"]["mechanism"],
        "url": url,
        "zip_sha256": card["files"]["upload"]["sha256"],
        "opened": opened,
        "verified_url": bool(opts.verify_url and card["staging"]["mechanism"] == "import-url"),
    }
    with open(bundle_dir / "staged.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, sort_keys=True) + "\n")
    print(
        "Staging only: yapnr uploaded nothing and ordered nothing. Check the preview, choose the "
        "options on the card and pay on the vendor's page.",
        file=out,
    )
    return 0
