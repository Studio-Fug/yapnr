"""Refreshing Spot prices from the Cloud Billing Catalog, and ranking (family, region) pairs.

The committed price table (``data/gcp-spot-prices.json``) is a dated snapshot. ``refresh`` reads
the public Compute Engine SKUs from the Cloud Billing Catalog API
(``GET https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus``) with an API key
restricted to that API, and writes the owner's copy of the table with today's Spot prices per
vCPU-hour and GiB-hour for every family and region it finds. The key comes from the command in
``[gcp] price_api_key_command`` (the macOS Keychain in the example) and is never printed or
written anywhere.

Only SKUs whose description reads ``Spot Preemptible <FAMILY> [vendor] Instance Core|Ram running
in ...`` are used; families the table does not know are ignored, and a family/region needs both
its core and its memory SKU (T2D and H3 included) to be priced. The parse is unit-tested against
recorded SKU shapes, but the description format is Google's and can change: ``refresh`` reports
what it matched, and ``yapnr exp prices --refresh`` shows the differences from the old table
before writing.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from yapnr.exp import cost

COMPUTE_SERVICE = "6F81-5844-456A"
CATALOG_URL = "https://cloudbilling.googleapis.com/v1/services/%s/skus" % COMPUTE_SERVICE
SKU_RE = re.compile(
    r"^Spot Preemptible (?P<family>[A-Z][A-Z0-9]*)(?: (?:AMD|Intel|Arm|Ampere))? Instance "
    r"(?P<what>Core|Ram) running in "
)
DEFAULT_CACHE = "~/.cache/yapnr/exp/gcp-spot-prices.json"
TIMEOUT_S = 60
MAX_PAGES = 50


class PriceError(RuntimeError):
    pass


def api_key(command: Sequence[str]) -> str:
    if not command:
        raise PriceError("set [gcp] price_api_key_command to read the Billing Catalog API key")
    try:
        done = subprocess.run(list(command), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as err:
        raise PriceError("the price key command failed to run: %s" % type(err).__name__) from err
    key = done.stdout.strip()
    if done.returncode != 0 or not key:
        raise PriceError("the price key command returned no key (exit %d)" % done.returncode)
    return key


def _open(request: urllib.request.Request):
    return urllib.request.urlopen(request, timeout=TIMEOUT_S)


def fetch_skus(key: str, opener: Callable = _open) -> List[Dict[str, Any]]:
    skus: List[Dict[str, Any]] = []
    token = None
    for _ in range(MAX_PAGES):
        query = {"currencyCode": "USD", "pageSize": "5000", "key": key}
        if token:
            query["pageToken"] = token
        request = urllib.request.Request(CATALOG_URL + "?" + urllib.parse.urlencode(query))
        try:
            with opener(request) as response:
                page = json.loads(response.read().decode())
        except Exception as err:  # never echo the URL: it carries the key
            raise PriceError("Billing Catalog request failed: %s" % type(err).__name__) from err
        skus += page.get("skus", [])
        token = page.get("nextPageToken")
        if not token:
            return skus
    raise PriceError("the Billing Catalog returned more than %d pages" % MAX_PAGES)


def unit_price(sku: Mapping[str, Any]) -> Optional[float]:
    try:
        rates = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"]
        price = rates[-1]["unitPrice"]
    except (KeyError, IndexError, TypeError):
        return None
    return int(price.get("units") or 0) + int(price.get("nanos") or 0) / 1e9


def parse_skus(skus: Sequence[Mapping[str, Any]], families: Sequence[str]):
    """{family: {region: [vcpu-hour, gib-hour]}} for the known families with both SKUs."""
    found: Dict[str, Dict[str, Dict[str, float]]] = {}
    for sku in skus:
        match = SKU_RE.match(sku.get("description", ""))
        if not match:
            continue
        family = match["family"].lower()
        if family not in families:
            continue
        price = unit_price(sku)
        if price is None:
            continue
        for region in sku.get("serviceRegions", []):
            found.setdefault(family, {}).setdefault(region, {})[match["what"]] = price
    out: Dict[str, Dict[str, List[float]]] = {}
    for family, regions in found.items():
        for region, parts in regions.items():
            if "Core" in parts and "Ram" in parts:
                out.setdefault(family, {})[region] = [parts["Core"], parts["Ram"]]
    return out


def refresh(table: cost.PriceTable, skus, today: Optional[_dt.date] = None):
    """A new table: ``table`` with the Spot prices of ``skus``; and a list of changes."""
    data = copy.deepcopy(dict(table.data))
    parsed = parse_skus(skus, list(data["families"]))
    changes = []
    for family, regions in parsed.items():
        spot = data["families"][family].setdefault("spot", {})
        if data["families"][family].get("memory_included"):
            # The table prices these per vCPU with memory; keep that convention (4 GB per vCPU).
            per_gb = data["families"][family]["memory_gb_per_vcpu"].get("standard", 4.0)
            regions = {r: [c + per_gb * m, 0.0] for r, (c, m) in regions.items()}
        for region, value in sorted(regions.items()):
            old = spot.get(region)
            if old != value:
                changes.append((family, region, old, value))
            spot[region] = [round(value[0], 7), round(value[1], 8)]
        data["families"][family]["memory_included"] = data["families"][family].get(
            "memory_included", False
        )
    data["accessed"] = (today or _dt.date.today()).isoformat()
    data["note"] = (
        "Refreshed from the Cloud Billing Catalog API on %s by `yapnr exp prices --refresh`; "
        % data["accessed"]
        + "families or regions it did not find keep the values of the committed snapshot."
    )
    data["sources"] = [
        {
            "what": "Cloud Billing Catalog API (Compute Engine SKUs)",
            "url": CATALOG_URL,
            "accessed": data["accessed"],
        }
    ] + [s for s in data.get("sources", []) if "Catalog API" not in s.get("what", "")]
    return data, changes


def rank(
    table: cost.PriceTable,
    calibration: cost.Calibration,
    families: Sequence[str],
    regions: Sequence[str],
    vm_vcpus: int = 16,
) -> List[Tuple[float, str, str, str]]:
    """(dollars per reference-core-hour of work, family, region, shape), cheapest first."""
    out = []
    for family in families:
        for region in regions:
            try:
                p = cost.place(
                    table,
                    calibration,
                    cpus=1,
                    memory_gb=2,
                    families=[family],
                    regions=[region],
                    vm_vcpus=vm_vcpus,
                )
            except cost.CostError:
                continue
            if "fallback" in p.price_source:
                continue  # no price for this region in the table
            per_task_hour = p.vm_hour / p.tasks_per_vm / p.speed
            out.append((round(per_task_hour, 5), family, region, p.shape))
    return sorted(out)


def write(data: Mapping[str, Any], path: Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return target
