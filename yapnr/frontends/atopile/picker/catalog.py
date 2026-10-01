"""Picker catalogs: the ``yapnr-picker-catalog-v1`` schema, its validator, and part matching.

A catalog is a list of facts about orderable parts (supplier id, manufacturer, part number,
package, kind and typed parameters) plus where those facts came from. The picker service answers
atopile's components-API queries from one or more catalogs; the part cache exports its catalog in
the same schema. Catalog files are data a user or project owns; yapnr commits none (docs/part-
cache.md, "What may be stored").

Stdlib only: the picker runs under any Python 3.9+ interpreter, including atopile's own.

Schema (JSON; YAML too when PyYAML is importable)::

    {
      "schema": "yapnr-picker-catalog-v1",
      "provenance": {"source": "...", "retrieved": "YYYY-MM-DD",
                     "terms_url": "...", "licence_note": "..."},
      "parts": [
        {"lcsc": "C25082", "mpn": "...", "manufacturer": "...", "package": "0402",
         "description": "...", "kind": "resistor",
         "params": {"resistance_ohm": 150.0, "tolerance_pct": 1.0},
         "basic": true, "stock": "unknown", "datasheet_url": "https://..."}
      ]
    }

``stock`` is a non-negative integer or ``"unknown"``. atopile needs numbers, so the service sends
0 for an unknown stock and a price of 0.0 when none is known; nothing downstream treats a picker
answer as evidence that a part is orderable.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = "yapnr-picker-catalog-v1"

KINDS = (
    "ic",
    "resistor",
    "capacitor",
    "inductor",
    "diode",
    "led",
    "transistor",
    "connector",
    "crystal",
    "fuse",
    "switch",
    "module",
    "mechanical",
    "other",
)

# Typed parameters: catalog name -> (atopile attribute name, API unit name). The unit names are
# the lower-case pint names that atopile 0.15.8 sends and accepts (Units.serialize_for_api).
PARAMS: Dict[str, Tuple[str, str]] = {
    "resistance_ohm": ("resistance", "ohm"),
    "capacitance_f": ("capacitance", "farad"),
    "inductance_h": ("inductance", "henry"),
    "voltage_max_v": ("max_voltage", "volt"),
    "power_max_w": ("max_power", "watt"),
    "current_max_a": ("max_current", "ampere"),
    "saturation_current_a": ("saturation_current", "ampere"),
    "dc_resistance_ohm": ("dc_resistance", "ohm"),
    "self_resonant_frequency_hz": ("self_resonant_frequency", "hertz"),
}
# Non-numeric or modifier parameters that are allowed but never sent as a quantity.
OTHER_PARAMS = ("tolerance_pct", "tempco")

# The value that a tolerance widens (the part's nominal value becomes an interval).
TOLERANCED = {"resistance_ohm", "capacitance_f", "inductance_h"}

# atopile 0.15.8's type-pick endpoints, their part kind, and the attributes its modules declare
# (the Resistor, Capacitor and Inductor of its standard library).
ENDPOINTS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "resistors": ("resistor", ("resistance", "max_power", "max_voltage")),
    "capacitors": ("capacitor", ("capacitance", "max_voltage", "temperature_coefficient")),
    "inductors": (
        "inductor",
        (
            "inductance",
            "max_current",
            "dc_resistance",
            "saturation_current",
            "self_resonant_frequency",
        ),
    ),
}
_ATTRIBUTE_PARAM = {attr: name for name, (attr, _unit) in PARAMS.items()}
_KIND_ATTRIBUTES = {kind: attrs for kind, attrs in ENDPOINTS.values()}

_LCSC_RE = re.compile(r"^C?([1-9][0-9]{0,11})$")
_PART_KEYS = {
    "lcsc",
    "mpn",
    "manufacturer",
    "package",
    "description",
    "kind",
    "params",
    "basic",
    "preferred",
    "stock",
    "price",
    "datasheet_url",
}
_TEXT_LIMIT = 512


class CatalogError(ValueError):
    """A catalog that does not match the schema; the message lists every problem."""


def normalize_lcsc(value: Any) -> str:
    """``C25082`` from ``"C25082"``, ``"25082"`` or ``25082``; ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError(f"not an LCSC part number: {value!r}")
    match = _LCSC_RE.match(str(value).strip().upper())
    if not match:
        raise ValueError(f"not an LCSC part number: {value!r}")
    return "C" + match.group(1)


def lcsc_number(lcsc: str) -> int:
    return int(normalize_lcsc(lcsc)[1:])


def normalize_package(value: Optional[str]) -> str:
    """Compare packages without the R/C/L prefix atopile adds to imperial sizes (R0402 = 0402)."""
    text = (value or "").strip().upper()
    match = re.match(r"^[RCL](\d{4,5})$", text)
    return match.group(1) if match else text


def _problem(problems: List[str], where: str, text: str) -> None:
    problems.append(f"{where}: {text}")


def _text(part: Dict[str, Any], key: str, problems: List[str], where: str, required: bool) -> str:
    value = part.get(key)
    if value is None:
        if required:
            _problem(problems, where, f"missing {key!r}")
        return ""
    if not isinstance(value, str):
        _problem(problems, where, f"{key!r} must be a string")
        return ""
    if len(value) > _TEXT_LIMIT or any(ord(ch) < 32 for ch in value):
        _problem(problems, where, f"{key!r} is too long or has control characters")
        return ""
    return value.strip()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validate_part(raw: Any, where: str, problems: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        _problem(problems, where, "a part must be an object")
        return None
    unknown = sorted(set(raw) - _PART_KEYS)
    if unknown:
        _problem(problems, where, f"unknown keys {unknown}")
    part: Dict[str, Any] = {}
    try:
        part["lcsc"] = normalize_lcsc(raw.get("lcsc"))
    except ValueError as err:
        _problem(problems, where, str(err))
    part["mpn"] = _text(raw, "mpn", problems, where, required=True)
    part["manufacturer"] = _text(raw, "manufacturer", problems, where, required=True)
    part["package"] = _text(raw, "package", problems, where, required=False)
    part["description"] = _text(raw, "description", problems, where, required=False)
    part["datasheet_url"] = _text(raw, "datasheet_url", problems, where, required=False)
    if part["datasheet_url"] and not part["datasheet_url"].startswith(("https://", "http://")):
        _problem(problems, where, "datasheet_url must be an http(s) URL")
    kind = raw.get("kind", "other")
    if kind not in KINDS:
        _problem(problems, where, f"kind {kind!r} is not one of {', '.join(KINDS)}")
    part["kind"] = kind
    params = raw.get("params", {})
    if not isinstance(params, dict):
        _problem(problems, where, "params must be an object")
        params = {}
    clean: Dict[str, Any] = {}
    for name, value in params.items():
        if name in PARAMS or name == "tolerance_pct":
            number = _number(value)
            if number is None or number < 0:
                _problem(problems, where, f"param {name!r} must be a non-negative number")
                continue
            clean[name] = number
        elif name == "tempco":
            if not isinstance(value, str) or not value:
                _problem(problems, where, "param 'tempco' must be a string such as 'X7R'")
                continue
            clean[name] = value
        else:
            _problem(problems, where, f"unknown param {name!r}")
    part["params"] = clean
    for flag in ("basic", "preferred"):
        value = raw.get(flag, False)
        if not isinstance(value, (bool, int)) or isinstance(value, float):
            _problem(problems, where, f"{flag!r} must be a boolean")
        part[flag] = bool(value)
    stock = raw.get("stock", "unknown")
    if stock != "unknown" and (isinstance(stock, bool) or not isinstance(stock, int) or stock < 0):
        _problem(problems, where, "stock must be a non-negative integer or 'unknown'")
        stock = "unknown"
    part["stock"] = stock
    price = raw.get("price")
    if price is not None:
        number = _number(price)
        if number is None or number < 0:
            _problem(problems, where, "price must be a non-negative number (unit price)")
            price = None
        else:
            price = number
    part["price"] = price
    return part


def validate(doc: Any, where: str = "catalog") -> Dict[str, Any]:
    """Return the normalized catalog, or raise CatalogError listing every problem."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        raise CatalogError(f"{where}: a catalog must be a JSON object")
    if doc.get("schema") != SCHEMA:
        _problem(problems, where, f"schema must be {SCHEMA!r} (got {doc.get('schema')!r})")
    unknown = sorted(set(doc) - {"schema", "provenance", "parts"})
    if unknown:
        _problem(problems, where, f"unknown top-level keys {unknown}")
    provenance = doc.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("source"), str):
        _problem(problems, where, "provenance must be an object with a 'source' string")
        provenance = {}
    raw_parts = doc.get("parts")
    if not isinstance(raw_parts, list):
        _problem(problems, where, "parts must be a list")
        raw_parts = []
    parts = []
    seen: Dict[str, int] = {}
    for index, raw in enumerate(raw_parts):
        part = _validate_part(raw, f"{where}: parts[{index}]", problems)
        if part is None:
            continue
        lcsc = part.get("lcsc")
        if lcsc in seen:
            _problem(problems, where, f"parts[{index}]: {lcsc} repeats parts[{seen[lcsc]}]")
            continue
        if lcsc:
            seen[lcsc] = index
        parts.append(part)
    if problems:
        raise CatalogError("\n".join(problems))
    return {"schema": SCHEMA, "provenance": dict(provenance), "parts": parts}


def load(path: "str | Path") -> Dict[str, Any]:
    """Read and validate a catalog file (JSON, or YAML when PyYAML is available)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as err:  # the picker may run where PyYAML is missing
            raise CatalogError(f"{path}: YAML catalogs need PyYAML; use JSON") from err
        doc = yaml.safe_load(text)
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as err:
            raise CatalogError(f"{path}: not valid JSON: {err}") from err
    return validate(doc, where=str(path.name))


def dump(doc: Dict[str, Any]) -> str:
    """Canonical JSON for a validated catalog (sorted parts, stable key order)."""
    parts = sorted(doc["parts"], key=lambda p: lcsc_number(p["lcsc"]))
    return json.dumps({**doc, "parts": parts}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --- Splanc's authoring catalog ---------------------------------------------------------------

_SPLANC_LIST_KIND = {
    "resistors": "resistor",
    "capacitors": "capacitor",
    "inductors": "inductor",
    "leds": "led",
    "ics": "ic",
}


def _guess_passive(description: str) -> str:
    """The kind of a Splanc ``passives`` entry, from the unit in its description only."""
    text = description.replace("Ω", "ohm").lower()
    if re.search(r"\d(\.\d+)?\s*[pnuµm]?f\b", text):
        return "capacitor"
    if re.search(r"\d(\.\d+)?\s*[kmg]?\s*ohm|\d[rk]\d", text):
        return "resistor"
    if re.search(r"\d(\.\d+)?\s*[nuµm]?h\b", text):
        return "inductor"
    return "other"


def from_splanc(
    doc: Dict[str, Any], source: str, retrieved: Optional[str] = None
) -> Dict[str, Any]:
    """Convert Splanc's picker catalog layout (lists of parts by type) to schema v1.

    Only facts that are present are carried over: ``resistance_ohms`` becomes
    ``params.resistance_ohm``; a missing stock becomes ``"unknown"``; nothing is parsed out of
    free-text descriptions except the kind of a ``passives`` entry.
    """
    parts = []
    for key, value in doc.items():
        if not isinstance(value, list):
            continue
        for raw in value:
            part = {
                "lcsc": raw["lcsc"],
                "mpn": raw["mpn"],
                "manufacturer": raw["manufacturer"],
                "package": raw.get("package", ""),
                "description": raw.get("description", ""),
                "kind": _SPLANC_LIST_KIND.get(key) or _guess_passive(raw.get("description", "")),
                "basic": bool(raw.get("basic", False)),
                "preferred": bool(raw.get("preferred", False)),
                "stock": raw["stock"] if isinstance(raw.get("stock"), int) else "unknown",
            }
            if raw.get("datasheet_url"):
                part["datasheet_url"] = raw["datasheet_url"]
            if "resistance_ohms" in raw:
                part["params"] = {"resistance_ohm": float(raw["resistance_ohms"])}
            parts.append(part)
    return validate(
        {
            "schema": SCHEMA,
            "provenance": {
                "source": source,
                **({"retrieved": retrieved} if retrieved else {}),
                "licence_note": "hand-curated part facts (supplier id, manufacturer, part number,"
                " package); converted from a Splanc picker catalog",
            },
            "parts": parts,
        },
        where="converted catalog",
    )


# --- atopile components-API shapes ------------------------------------------------------------


def quantity(lo: float, hi: float, unit: str) -> Dict[str, Any]:
    """An atopile 0.15.8 API quantity: one interval, in base SI units."""
    return {
        "type": "Quantity_Interval_Disjoint",
        "data": {
            "intervals": {
                "type": "Numeric_Interval_Disjoint",
                "data": {
                    "intervals": [{"type": "Numeric_Interval", "data": {"min": lo, "max": hi}}]
                },
            },
            "unit": unit,
        },
    }


def intervals(pset: Any) -> Optional[List[Tuple[float, float]]]:
    """The intervals of an API quantity (None = unconstrained; None bounds are infinite)."""
    if not isinstance(pset, dict):
        return None
    if pset.get("type") not in ("Quantity_Interval_Disjoint", "Quantity_Set_Discrete"):
        return None
    try:
        raw = pset["data"]["intervals"]["data"]["intervals"]
    except (KeyError, TypeError):
        return None
    out = []
    for item in raw if isinstance(raw, list) else []:
        data = item.get("data", {}) if isinstance(item, dict) else {}
        lo = -math.inf if data.get("min") is None else float(data["min"])
        hi = math.inf if data.get("max") is None else float(data["max"])
        out.append((lo, hi))
    return out or None


def strings(value: Any) -> List[str]:
    """Every string element of an API enum or string set, however nested."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key in ("elements", "values", "data", "value", "name"):
                if key in node:
                    walk(node[key])

    walk(value)
    return found


def part_interval(part: Dict[str, Any], name: str) -> Optional[Tuple[float, float]]:
    """The interval a part's parameter occupies (its tolerance band for the main value)."""
    value = part["params"].get(name)
    if value is None:
        return None
    tolerance = part["params"].get("tolerance_pct") if name in TOLERANCED else None
    if tolerance:
        return value * (1 - tolerance / 100.0), value * (1 + tolerance / 100.0)
    return value, value


def component(part: Dict[str, Any]) -> Dict[str, Any]:
    """The atopile ``Component`` for a catalog part."""
    attributes: Dict[str, Any] = {}
    for attribute in _KIND_ATTRIBUTES.get(part["kind"], ()):
        name = _ATTRIBUTE_PARAM.get(attribute)
        band = part_interval(part, name) if name else None
        attributes[attribute] = quantity(band[0], band[1], PARAMS[name][1]) if band else None
    price = part.get("price")
    return {
        "lcsc": lcsc_number(part["lcsc"]),
        "manufacturer_name": part["manufacturer"],
        "part_number": part["mpn"],
        "package": part["package"],
        "datasheet_url": part["datasheet_url"],
        "description": part["description"],
        "is_basic": int(part["basic"]),
        "is_preferred": int(part["preferred"]),
        "stock": part["stock"] if isinstance(part["stock"], int) else 0,
        "price": [{"qFrom": 1, "qTo": None, "price": price if price is not None else 0.0}],
        "attributes": attributes,
    }


class Catalog:
    """The parts of one or more validated catalogs, indexed for the API's lookups.

    When catalogs repeat an LCSC id, the first catalog given wins (a project's own catalog can
    override a shared one by coming first).
    """

    def __init__(self, docs: Iterable[Dict[str, Any]] = ()):
        self.parts: List[Dict[str, Any]] = []
        self._lcsc: Dict[str, Dict[str, Any]] = {}
        self._mfr: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for doc in docs:
            for part in doc["parts"]:
                if part["lcsc"] in self._lcsc:
                    continue
                self._lcsc[part["lcsc"]] = part
                self._mfr.setdefault(_mfr_key(part["manufacturer"], part["mpn"]), []).append(part)
                self.parts.append(part)

    def __len__(self) -> int:
        return len(self.parts)

    def by_lcsc(self, lcsc: Any) -> List[Dict[str, Any]]:
        try:
            part = self._lcsc.get(normalize_lcsc(lcsc))
        except ValueError:
            return []
        return [component(part)] if part else []

    def by_mfr(self, manufacturer: str, mpn: str) -> List[Dict[str, Any]]:
        return [component(p) for p in self._mfr.get(_mfr_key(manufacturer, mpn), [])]

    def query(self, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Candidates for a type query, best first (basic parts, then the closest value)."""
        if endpoint not in ENDPOINTS:
            return []
        kind, attributes = ENDPOINTS[endpoint]
        wanted_packages = {normalize_package(p) for p in strings(params.get("package"))}
        constraints = {}
        for attribute in attributes:
            name = _ATTRIBUTE_PARAM.get(attribute)
            bounds = intervals(params.get(attribute))
            if name and bounds:
                constraints[name] = bounds
        matches = []
        for part in self.parts:
            if part["kind"] != kind:
                continue
            if wanted_packages and normalize_package(part["package"]) not in wanted_packages:
                continue
            if all(_inside(part_interval(part, n), b) for n, b in constraints.items()):
                matches.append(part)
        main = next((n for n in constraints if n in TOLERANCED), None)

        def rank(part: Dict[str, Any]) -> Tuple[int, float, int]:
            distance = 0.0
            if main:
                lo, hi = constraints[main][0]
                if math.isfinite(lo) and math.isfinite(hi):
                    distance = abs(part["params"][main] - (lo + hi) / 2)
            return (0 if part["basic"] else 1, distance, lcsc_number(part["lcsc"]))

        return [component(p) for p in sorted(matches, key=rank)]

    def answer(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
        """One entry of ``POST /v0/query``: an LCSC, a manufacturer part or a type query."""
        if "lcsc" in query:
            return self.by_lcsc(query["lcsc"])
        if query.get("part_number"):
            return self.by_mfr(query.get("manufacturer_name") or "", query["part_number"])
        endpoint = query.get("endpoint")
        return self.query(endpoint, query) if isinstance(endpoint, str) else []


def _mfr_key(manufacturer: str, mpn: str) -> Tuple[str, str]:
    return manufacturer.strip().casefold(), mpn.strip().casefold()


def _inside(band: Optional[Tuple[float, float]], bounds: Sequence[Tuple[float, float]]) -> bool:
    if band is None:
        return False
    lo, hi = band
    eps = 1e-9 * max(1.0, abs(lo), abs(hi))
    return any(b_lo - eps <= lo and hi <= b_hi + eps for b_lo, b_hi in bounds)
