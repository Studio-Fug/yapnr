"""Populate our local picker from public supplier facts, without Atopile accounts.

The public jlcsearch database supplies typed values; its EasyEDA mirror supplies
manufacturer identity. Both raw replies travel with the captured query. Matching
and API serialization remain our rules_atopile-derived catalog implementation.
"""

import hashlib
import json
import math
from urllib.parse import urlencode

from yapnr.frontends.atopile.picker import catalog

SOURCE = "https://jlcsearch.tscircuit.com"
MAX_CANDIDATES = 8
FIELDS = {
    "resistors": {
        "resistance_ohm": "resistance",
        "power_max_w": "power_watts",
        # Overload voltage is not a continuous working-voltage rating.
    },
    "capacitors": {"capacitance_f": "capacitance_farads", "voltage_max_v": "voltage_rating"},
}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def discover(method, path, body, read):
    sources = []

    def get(route):
        url = SOURCE + route
        data = read(url)
        sources.append(
            {"url": url, "response": data, "sha256": hashlib.sha256(encoded(data)).hexdigest()}
        )
        return data

    def query(params):
        endpoint = params.get("endpoint")
        packages = catalog.strings(params.get("package"))
        if "lcsc" in params:
            search = catalog.normalize_lcsc(params["lcsc"])
        elif params.get("part_number"):
            search = params["part_number"]
        else:
            search = None
        if search is not None:
            data = get("/components/list.json?" + urlencode({"search": search}))
            rows = data.get("components", [])
        elif endpoint in FIELDS:
            filters = {}
            if len(packages) == 1:
                filters["package"] = packages[0]
            main = "resistance" if endpoint == "resistors" else "capacitance"
            bands = catalog.intervals(params.get(main))
            if bands and len(bands) == 1:
                lo, hi = bands[0]
                midpoint = (lo + hi) / 2
                # Narrow tolerance requests can use the supplier's nominal-value filter.
                # Broad/disjoint constraints stay local; never assert exhaustive discovery.
                if math.isfinite(midpoint) and midpoint > 0 and hi - lo <= midpoint * 0.25:
                    filters[main] = format(midpoint, ".12g")
            data = get("/" + endpoint + "/list.json?" + urlencode(filters))
            rows = data.get(endpoint, [])
        else:
            raise ValueError(
                "No public typed source for this query; select an explicit LCSC/MPN or import a verified catalog"
            )
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Supplier response has an invalid candidate list")
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get("lcsc")) is not int:
                continue
            if "lcsc" in params and catalog.normalize_lcsc(row["lcsc"]) != search:
                continue
            if params.get("part_number") and row.get("mfr", "").casefold() != search.casefold():
                continue
            if packages and catalog.normalize_package(row.get("package", "")) not in {
                catalog.normalize_package(value) for value in packages
            }:
                continue
            values = {
                name: row[field]
                for name, field in FIELDS.get(endpoint, {}).items()
                if isinstance(row.get(field), (int, float))
                and not isinstance(row[field], bool)
                and math.isfinite(row[field])
                and row[field] >= 0
            }
            tolerance = row.get("tolerance_fraction")
            if (
                isinstance(tolerance, (int, float))
                and not isinstance(tolerance, bool)
                and 0 <= tolerance <= 1
            ):
                values["tolerance_pct"] = tolerance * 100
            # Unknown main-value tolerance cannot satisfy a toleranced type query.
            if endpoint in FIELDS and "tolerance_pct" not in values:
                continue
            constraints = {
                name: catalog.intervals(params.get(attribute))
                for name, (attribute, _unit) in catalog.PARAMS.items()
            }
            if any(
                bounds
                and not catalog._inside(catalog.part_interval({"params": values}, name), bounds)
                for name, bounds in constraints.items()
            ):
                continue
            candidates.append((row, values))
        candidates.sort(key=lambda item: (not bool(item[0].get("is_basic")), item[0]["lcsc"]))
        parts = []
        for row, values in candidates[:MAX_CANDIDATES]:
            identity = get("/api/easyeda_components/" + catalog.normalize_lcsc(row["lcsc"]))
            details = identity.get("easyeda_component_details", {})
            if details.get("lcsc") != row["lcsc"]:
                raise ValueError("Supplier identity belongs to a different LCSC part")
            native = details.get("easyeda_json", {})
            drawing = native.get("dataStr", {})
            if isinstance(drawing, str):
                drawing = json.loads(drawing)
            facts = drawing.get("head", {}).get("c_para", {})
            maker, mpn = facts.get("Manufacturer"), facts.get("Manufacturer Part")
            if not maker or not mpn or mpn != row.get("mfr"):
                raise ValueError("Supplier and geometry identities disagree or are incomplete")
            if (
                params.get("manufacturer_name")
                and maker.casefold() != params["manufacturer_name"].casefold()
            ):
                continue
            kind = catalog.ENDPOINTS[endpoint][0] if endpoint in FIELDS else "other"
            part = {
                "lcsc": row["lcsc"],
                "mpn": mpn,
                "manufacturer": maker,
                "package": row.get("package", ""),
                "description": row.get("description", ""),
                "kind": kind,
                "params": values,
                "basic": bool(row.get("is_basic")),
                "preferred": bool(row.get("is_preferred")),
                "stock": row.get("stock") if type(row.get("stock")) is int else "unknown",
            }
            if isinstance(row.get("price1"), (float, int)) and row["price1"] >= 0:
                part["price"] = row["price1"]
            parts.append(part)
        document = catalog.validate(
            {
                "schema": catalog.SCHEMA,
                "provenance": {
                    "source": SOURCE,
                    "licence_note": "Public supplier facts; verify ratings and availability before fabrication",
                },
                "parts": parts,
            },
            where="discovered supplier catalog",
        )
        engine = catalog.Catalog([document])
        return engine.answer(params), document

    if method == "GET" and path.startswith("/v0/component/lcsc/"):
        queries = [{"lcsc": path.rsplit("/", 1)[1]}]
    elif method == "GET" and path.startswith("/v0/component/mfr/"):
        from urllib.parse import unquote

        segments = path.split("/")
        queries = [
            {"manufacturer_name": unquote(segments[-2]), "part_number": unquote(segments[-1])}
        ]
    elif method == "POST":
        payload = json.loads(body or b"{}")
        queries = (
            payload.get("queries", [])
            if path == "/v0/query"
            else [{**payload, "endpoint": path.rsplit("/", 1)[1]}]
        )
    else:
        raise ValueError("Unsupported component query")
    if len(queries) > 16:
        raise ValueError("Discovery is bounded to sixteen queries per request")
    results, documents = [], []
    for params in queries:
        components, document = query(params)
        results.append({"components": components})
        documents.append(document)
    response = {"results": results} if path == "/v0/query" else results[0]
    response["_catalogs"] = documents
    response["_sources"] = sources
    return response
