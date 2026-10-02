"""The order card: what the human checks before uploading, as text, Markdown and JSON.

Design §8.2. The card is data (``yapnr-order-card-v1``) built by ``yapnr fab build`` and completed
by ``yapnr order stage`` with the run's choices (mechanism, page, quantity). It names the vendor,
service, board, stackup and what to pick at checkout, the options to set, the checks, the file to
drop and its sha256, and the next steps on the vendor's page. Stdlib only.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

SCHEMA = "yapnr-order-card-v1"
BANNER = "staging only: yapnr uploads nothing, orders nothing, pays nothing"
WIDTH = 12


def _rows(card: Dict[str, Any]) -> List[tuple]:
    v, b, s, o = card["vendor"], card["board"], card["stackup"], card["options"]
    svc = card["service"]
    rows = [("vendor", f"{v['title']} ({v['country']}), {svc['title']} service")]
    if card["profile"].get("status") == "draft":
        rows.append(("PROFILE", f"{card['profile']['name']} is a DRAFT profile (--allow-draft)"))
    else:
        rows.append(("profile", f"{card['profile']['name']}: {card['profile']['title']}"))
    w, h = b["size_mm"] or (0.0, 0.0)
    rows.append(
        (
            "board",
            f"{b['name']}   {w:.2f} x {h:.2f} mm   {b['area_sq_in']:.3f} sq in   "
            f"input id {b['input_id'][:12]}",
        )
    )
    rows.append(
        (
            "layers",
            f"{len(b['layers'])}: {', '.join(b['layers'])}   thickness {o.get('thickness_mm')} mm",
        )
    )
    rows.append(("stackup", f"{s['id']}: {s['summary']}"))
    rows.append(("CHECKOUT", s["checkout"]))
    if o.get("via_option"):
        rows.append(("", f"via option: {o['via_option']}"))
    rows.append(
        (
            "finish/mask",
            f"{o.get('finish')} / {o.get('mask_color')}"
            + (" (fixed)" if o.get("fixed") else "")
            + f"   via covering: {o.get('via_covering')}",
        )
    )
    rows.append(("copper", str(o.get("copper"))))
    if o.get("remark"):
        rows.append(("remark", f"paste into the order remark: {o['remark']}"))
    rows.append(("impedance", card["impedance"]))
    q = card["qty"]
    rows.append(("qty", f"{q['value']} ({q['rule']})"))
    est = card.get("estimate")
    if est:
        rows.append(("estimate", f"${est['usd']:.2f} ({est['formula']}; {est['source']})"))
    else:
        rows.append(("estimate", "see the vendor's quote"))
    c = card["checks"]
    rows.append(
        (
            "checks",
            f"{card['profile']['name']}: DRC {c['drc_errors']} errors, {c['drc_warnings']} warnings; "
            f"fab check {c['summary']['error']} errors, {c['summary']['warning']} warnings, "
            f"{c['summary']['info']} notes",
        )
    )
    for note in c.get("notable", []):
        rows.append(("", note))
    f = card["files"]["upload"]
    rows.append(("file", f["path"]))
    rows.append(
        ("", f"sha256 {f['sha256'][:16]}...  ({f['members']} files, {f['bytes'] / 1000:.0f} kB)")
    )
    asm = card.get("assembly")
    if asm:
        rows.append(("assembly", f"BOM {asm['bom']}   CPL {asm['cpl']}"))
        rows.append(("", f"{asm['parts']} parts, {asm['lines']} BOM lines"))
        for note in asm.get("notes", []):
            rows.append(("", note))
    for warning in card.get("warnings", []):
        rows.append(("WARNING", warning))
    st = card["staging"]
    if st.get("url"):
        rows.append(("page", st["url"]))
    for i, step in enumerate(st.get("steps", []), 1):
        rows.append(("next" if i == 1 else "", f"{i}. {step}"))
    return rows


def text(card: Dict[str, Any]) -> str:
    """The terminal card (§8.2)."""
    lines = [f"ORDER CARD  {BANNER}"]
    for key, value in _rows(card):
        lines.append(f"{key:<{WIDTH}}{value}")
    return "\n".join(lines) + "\n"


def markdown(card: Dict[str, Any]) -> str:
    """The card as a Markdown table (written into the bundle)."""
    lines = [
        f"# Order card: {card['board']['name']} at {card['vendor']['title']}",
        "",
        f"_{BANNER}._",
        "",
        "| | |",
        "| --- | --- |",
    ]
    for key, value in _rows(card):
        cell = str(value).replace("|", "\\|")
        lines.append(f"| {key} | {cell} |")
    return "\n".join(lines) + "\n"


def to_json(card: Dict[str, Any]) -> str:
    return json.dumps(card, indent=2, sort_keys=True) + "\n"
