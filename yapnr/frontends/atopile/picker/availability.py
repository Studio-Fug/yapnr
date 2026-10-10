"""Quantity-aware assembly inventory screening; snapshots never reserve stock.

JLC's public assembly search protocol is also used by yaqwsx/jlcparts. Keep only
identity/inventory fields: replies can contain signed image URLs and temporary
credentials. PCBWay sourcing requires a supplier quote, not an LCSC stock count.
"""

import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.request import HTTPRedirectHandler, Request, build_opener

from yapnr.frontends.atopile.picker.catalog import normalize_lcsc

SCHEMA = "yapnr-assembly-availability-v1"
URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
FIELDS = (
    "componentCode",
    "componentModelEn",
    "componentBrandEn",
    "stockCount",
    "canPresaleNumber",
    "leastPatchNumber",
    "lossNumber",
    "minPurchaseNum",
)
LIMIT = 64


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(lcsc):
    lcsc = normalize_lcsc(lcsc)
    payload = {
        "currentPage": 1,
        "pageSize": 25,
        "keyword": normalize_lcsc(lcsc),
        "searchSource": "search",
        "searchType": 2,
        "componentBrandList": [],
        "componentSpecificationList": [],
        "componentAttributeList": [],
        "paramList": [],
    }
    request = Request(
        URL,
        data=encoded(payload),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with build_opener(NoRedirect()).open(request, timeout=8) as response:
        raw = response.read(4 * 1024**2 + 1)
    if len(raw) > 4 * 1024**2:
        raise ValueError("Assembly supplier response exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("code") != 200:
        raise ValueError("Assembly supplier query failed")
    page = data.get("data")
    if not isinstance(page, dict) or not isinstance(page.get("componentPageInfo"), dict):
        raise ValueError("Invalid assembly supplier page")
    rows = page["componentPageInfo"].get("list")
    if not isinstance(rows, list) or len(rows) > 25:
        raise ValueError("Invalid assembly supplier candidate list")
    matches = [row for row in rows if isinstance(row, dict) and row.get("componentCode") == lcsc]
    if len(matches) != 1:
        raise ValueError("Assembly supplier did not return one exact part identity")
    return {key: matches[0].get(key) for key in FIELDS}


def bom_parts(payload, include_through_hole=False):
    """Canonical complete BOM (one ref/row) or native supplier BOM (grouped refs)."""
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    if not rows or len(rows) > 1000:
        raise ValueError("BOM must contain 1 to 1000 rows")
    parts = []
    for row in rows:
        ref = row.get("Reference", row.get("Designator", row.get("Designators", "")))
        mpn = row.get("MPN", row.get("Mfg Part #", row.get("Manufacturer Part Number", "")))
        lcsc = row.get("LCSC", row.get("LCSC Part #", row.get("LCSC Part Number", "")))
        mount = row.get("Mount", row.get("Type", "")).upper()
        assembly = row.get("Assembly", "").lower()
        if not include_through_hole and (
            mount in ("THT", "TH", "THROUGH HOLE") or "hand" in assembly or "separate" in assembly
        ):
            continue
        if not ref:
            raise ValueError("BOM requires explicit designators")
        refs = [r.strip() for r in ref.replace(";", ",").split(",") if r.strip()]
        qty = row.get("Qty", row.get("Quantity", str(len(refs))))
        if not str(qty).isdigit() or int(qty) != len(refs):
            raise ValueError("BOM quantity must match explicit designators")
        parts.append(
            {"refs": refs, "mpn": mpn.strip(), "lcsc": lcsc.strip(), "per_board": len(refs)}
        )
    return parts


def aggregate(parts):
    groups, seen = {}, set()
    for part in parts:
        refs, mpn = part["refs"], part["mpn"]
        if (
            not isinstance(mpn, str)
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(r, str) or not r for r in refs)
        ):
            raise ValueError("Parts require an MPN and explicit designators")
        if type(part.get("per_board")) is not int or part["per_board"] != len(refs):
            raise ValueError("Part quantity must match designators")
        if len(set(refs)) != len(refs) or seen.intersection(refs):
            raise ValueError("Duplicate BOM designator")
        seen.update(refs)
        lcsc = normalize_lcsc(part["lcsc"]) if part.get("lcsc") else ""
        key = (lcsc, mpn.casefold())
        if lcsc and any(k[0] == lcsc and k != key for k in groups):
            raise ValueError("One LCSC identity has conflicting MPNs")
        group = groups.setdefault(key, {"lcsc": lcsc, "mpn": mpn, "refs": [], "per_board": 0})
        group["refs"].extend(refs)
        group["per_board"] += len(refs)
    if not groups or len(groups) > LIMIT:
        raise ValueError("Availability check requires 1 to 64 distinct parts")
    return sorted(groups.values(), key=lambda p: (p["lcsc"], p["mpn"]))


def _integer(row, name, signed=False):
    value = row.get(name)
    return value if type(value) is int and (signed or value >= 0) else None


def evaluate(part, quantity, vendor, fact=None):
    result = {
        **part,
        "needed": part["per_board"] * quantity,
        "status": "unknown",
        "listed_stock": None,
        "orderable_stock": None,
        "screening_required": None,
    }
    if vendor == "pcbway":
        return {
            **result,
            "status": "quote_required",
            "reason": (
                "PCBWay must confirm sourcing and assembly allowance in its quote; "
                "JLC/LCSC stock is not PCBWay inventory."
            ),
        }
    if not isinstance(fact, dict) or not fact:
        return {
            **result,
            "reason": "No verified assembly-house inventory response; refresh or request supplier confirmation.",
        }
    if (
        fact.get("componentCode") != part["lcsc"]
        or str(fact.get("componentModelEn", "")).strip().casefold() != part["mpn"].casefold()
    ):
        return {
            **result,
            "reason": "Assembly supplier identity does not match the requested LCSC/MPN.",
        }
    stock = _integer(fact, "stockCount")
    orderable = _integer(fact, "canPresaleNumber", signed=True)
    attrition = _integer(fact, "lossNumber")
    placement = _integer(fact, "leastPatchNumber")
    purchase = _integer(fact, "minPurchaseNum")
    effective = (
        min(stock, max(0, orderable)) if stock is not None and orderable is not None else None
    )
    required = (
        max(result["needed"] + attrition, placement, purchase)
        if None not in (attrition, placement, purchase)
        else None
    )
    result.update(
        listed_stock=stock,
        orderable_stock=effective,
        screening_required=required,
        attrition=attrition,
        minimum_placement=placement,
        minimum_purchase=purchase,
    )
    if effective is not None and effective < result["needed"]:
        return {
            **result,
            "status": "shortage",
            "reason": "Orderable assembly stock is below the board demand.",
        }
    if required is None or effective is None:
        return {
            **result,
            "reason": "Supplier quantity rules or orderable inventory are incomplete.",
        }
    if effective < required:
        return {
            **result,
            "status": "shortage",
            "reason": "Stock is below demand plus declared attrition/minimum quantity screening.",
        }
    return {
        **result,
        "status": "available",
        "reason": (
            "Quantity screening passed at retrieval time; stock is not reserved. "
            "Confirm allowances and availability in the quote."
        ),
    }


def check(parts, vendor, quantity, *, read=None, snapshot=None, retrieved=None):
    if (
        vendor not in ("jlcpcb", "pcbway")
        or type(quantity) is not int
        or not 1 <= quantity <= 10000
    ):
        raise ValueError("Choose JLCPCB or PCBWay and 1 to 10000 boards")
    groups = aggregate(parts)
    if snapshot is not None:
        if (
            snapshot.get("schema") != SCHEMA
            or snapshot.get("vendor") != vendor
            or snapshot.get("parts") != groups
            or snapshot.get("quantity") != quantity
        ):
            raise ValueError("Inventory snapshot does not match vendor, quantity and BOM")
        facts = snapshot["facts"]
        if hashlib.sha256(encoded(facts)).hexdigest() != snapshot["facts_sha256"]:
            raise ValueError("Inventory snapshot facts hash changed")
        retrieved = snapshot["retrieved"]
    else:
        reader = read or fetch

        def lookup(part):
            if vendor != "jlcpcb" or not part["lcsc"] or not part["mpn"]:
                return None
            try:
                fact = reader(part["lcsc"])
                if not isinstance(fact, dict):
                    return None
                return {key: fact.get(key) for key in FIELDS}
            except (OSError, ValueError, KeyError, TypeError):
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            facts = list(pool.map(lookup, groups))
        retrieved = retrieved or datetime.now(timezone.utc).isoformat()
    if not isinstance(facts, list) or len(facts) != len(groups):
        raise ValueError("Inventory snapshot has invalid part facts")
    results = [evaluate(part, quantity, vendor, fact) for part, fact in zip(groups, facts)]
    return {
        "schema": SCHEMA,
        "vendor": vendor,
        "quantity": quantity,
        "parts": groups,
        "retrieved": retrieved,
        "facts": facts,
        "facts_sha256": hashlib.sha256(encoded(facts)).hexdigest(),
        "source": (
            URL
            if vendor == "jlcpcb"
            else "https://www.pcbway.com/pcb_prototype/Electronic_Components.html"
        ),
        "screening_policy": (
            "max(board demand + declared lossNumber, leastPatchNumber, minPurchaseNum); "
            "quote confirms actual assembly allowance"
        ),
        "results": results,
        "all_available": all(r["status"] == "available" for r in results),
        "notice": (
            "Time-stamped inventory evidence, not a reservation or order approval. "
            "Refresh before handoff; replay preserves historical evidence only."
        ),
    }
