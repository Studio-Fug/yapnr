"""Vendor capability data: profiles, stackups and vendors (``yapnr/fab/data``), validated.

Design: docs/design/fab-and-ordering.md §5. Three kinds of JSON file:

- ``profiles/<name>.json`` (``yapnr-fab-profile-v1``): a process capability. ``engine`` holds the
  ``fab``/``copper``/``qualification`` blocks ``pnr.fab_profile`` uses (or ``{"builtin": true}``
  for ``jlc-pofv``, whose engine values stay Python literals there), ``vendor_limits`` the limits
  KiCad DRC cannot express, ``order_defaults`` what the order card proposes.
- ``stackups/<id>.json`` (``yapnr-fab-stackup-v1``): a dielectric build, top to bottom.
- ``vendors/<vendor>.json`` (``yapnr-fab-vendor-v1``): file sets, layer names, drill format,
  assembly columns and staging pages.

Every value has provenance: the ``src`` of the object holding it (a source key of the file's
``sources``, each with a URL and an access date, optionally followed by a note), ``derived:``
with the reason, or ``as fab.<key>``; a stand-in value is a ``*_prior`` with its own
``*_prior_src``. Unpublished values stay ``null``.

Stdlib only and Python 3.9 compatible: KiCad-side engine workers (KiCad's bundled Python 3.9)
import this module through ``pnr.fab_profile``. JSON, not TOML, for the same reason.
"""

from __future__ import annotations

import copy
import datetime
import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(__file__).resolve().with_name("data")
KINDS = ("profiles", "stackups", "vendors")
SCHEMAS = {
    "profiles": "yapnr-fab-profile-v1",
    "stackups": "yapnr-fab-stackup-v1",
    "vendors": "yapnr-fab-vendor-v1",
}
# Engine profiles that stay Python literals in pnr.fab_profile (byte-identical, §5.4).
BUILTIN_ENGINE = ("jlc-pofv",)
STATUSES = ("active", "draft")
# A source older than this gives a warning on the order card (§11, data refresh).
STALE_DAYS = 180

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COPPER_NAME = re.compile(r"^(F|B|In\d+)\.Cu$")


class DataError(ValueError):
    """A data file is missing or invalid; the message names the file and the problem."""


def _path(kind: str, name: str) -> Path:
    if kind not in KINDS:
        raise DataError(f"unknown data kind {kind!r}")
    if not re.match(r"^[a-z0-9][a-z0-9.\-]*$", name or ""):
        raise DataError(f"invalid {kind[:-1]} name {name!r}")
    return DATA_DIR / kind / f"{name}.json"


def names(kind: str) -> List[str]:
    """The names of every data file of ``kind`` (sorted)."""
    return sorted(p.stem for p in (DATA_DIR / kind).glob("*.json"))


@functools.lru_cache(maxsize=None)
def _load(kind: str, name: str) -> Dict[str, Any]:
    path = _path(kind, name)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        known = ", ".join(names(kind))
        raise DataError(f"no {kind[:-1]} {name!r} (known: {known})") from None
    except ValueError as err:
        raise DataError(f"{kind}/{name}.json: {err}") from None
    problems = validate(kind, name, doc)
    if problems:
        raise DataError(f"{kind}/{name}.json: " + "; ".join(problems))
    return doc


def load(kind: str, name: str) -> Dict[str, Any]:
    """A validated data file (a deep copy)."""
    return copy.deepcopy(_load(kind, name))


def profile(name: str) -> Dict[str, Any]:
    return load("profiles", name)


def stackup(name: str) -> Dict[str, Any]:
    return load("stackups", name)


def vendor(name: str) -> Dict[str, Any]:
    return load("vendors", name)


def file_path(kind: str, name: str) -> Path:
    """The data file itself (for hashing into a bundle manifest)."""
    path = _path(kind, name)
    if not path.is_file():
        raise DataError(f"no {kind[:-1]} {name!r}")
    return path


def engine_profile(name: str) -> Dict[str, Any]:
    """The ``pnr.fab_profile`` spec of a data profile: ``{fab, copper, qualification, ...}``.

    Raises KeyError for a name that is not a data profile with its own engine block (unknown
    names, and the built-in ``jlc-pofv`` whose values live in ``pnr.fab_profile``).
    """
    try:
        doc = _load("profiles", name)
    except DataError as err:
        raise KeyError(name) from err
    engine = doc["engine"]
    if engine.get("builtin"):
        raise KeyError(name)
    return {
        "fab": copy.deepcopy(engine["fab"]),
        "copper": copy.deepcopy(engine["copper"]),
        "qualification": engine["qualification"],
        "status": doc["status"],
        "sources": copy.deepcopy(doc["sources"]),
        "provenance": copy.deepcopy(engine["provenance"]),
    }


def engine_profile_names() -> List[str]:
    """Data profiles with their own engine block (selectable as ``PNR_FAB_PROFILE``)."""
    out = []
    for name in names("profiles"):
        try:
            if not _load("profiles", name)["engine"].get("builtin"):
                out.append(name)
        except DataError:
            continue
    return out


# ------------------------------------------------------------------------------- provenance


def source_key(text: Any, sources: Dict[str, Any]) -> Optional[str]:
    """The source key a provenance string cites, "derived" for a derived value, else None.

    Accepted forms: ``"<KEY>"``, ``"<KEY> (note)"``, ``"<KEY> and <KEY2> (note)"``,
    ``"derived: reason"`` and ``"as fab.<key>"`` (the copper block repeating a fab value).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if text.startswith("derived:") and len(text) > len("derived:") + 3:
        return "derived"
    if text.startswith("as fab."):
        return "derived"
    first = re.split(r"[\s(]", text, maxsplit=1)[0]
    return first if first in sources else None


def _check_sources(where: str, sources: Any, problems: List[str]) -> Dict[str, Any]:
    if not isinstance(sources, dict) or not sources:
        problems.append(f"{where}: no sources")
        return {}
    for key, value in sources.items():
        if not isinstance(value, dict):
            problems.append(f"source {key}: not an object")
            continue
        url, accessed = value.get("url"), value.get("accessed")
        if not isinstance(url, str) or not url.startswith("https://"):
            problems.append(f"source {key}: url must be https")
        if not isinstance(accessed, str) or not _DATE.match(accessed):
            problems.append(f"source {key}: accessed must be YYYY-MM-DD")
    return sources


def _need_src(where: str, text: Any, sources: Dict[str, Any], problems: List[str]) -> None:
    if source_key(text, sources) is None:
        problems.append(f"{where}: provenance {text!r} cites no source of this file")


def _positive(where: str, value: Any, problems: List[str], allow_null: bool = False) -> None:
    if value is None and allow_null:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        problems.append(f"{where}: expected a positive number, got {value!r}")


def _numbers(value: Any, prefix: str = ""):
    """(dotted key, number) for every number in nested dicts."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _numbers(inner, f"{prefix}{key}.")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield prefix.rstrip("."), value


# ------------------------------------------------------------------------------- validation


def validate(kind: str, name: str, doc: Any) -> List[str]:
    """Every problem of one data file (empty when valid). Cross-file references are checked by
    :func:`check_all`."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    if doc.get("schema") != SCHEMAS[kind]:
        problems.append(f"schema must be {SCHEMAS[kind]}")
    sources = _check_sources(name, doc.get("sources"), problems)
    {"profiles": _validate_profile, "stackups": _validate_stackup, "vendors": _validate_vendor}[
        kind
    ](name, doc, sources, problems)
    return problems


def _validate_profile(name: str, doc: Dict, sources: Dict, problems: List[str]) -> None:
    if doc.get("name") != name:
        problems.append("name must equal the file name")
    if doc.get("status") not in STATUSES:
        problems.append(f"status must be one of {STATUSES}")
    for key in ("vendor", "service", "title"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            problems.append(f"{key} missing")
    if not isinstance(doc.get("copper_layers"), int) or doc["copper_layers"] < 1:
        problems.append("copper_layers must be a positive integer")
    stackups = doc.get("stackups") or {}
    allowed = stackups.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        problems.append("stackups.allowed must list at least one stackup")
    elif stackups.get("default") is not None and stackups["default"] not in allowed:
        problems.append("stackups.default must be one of stackups.allowed (or null)")
    engine = doc.get("engine")
    if not isinstance(engine, dict):
        problems.append("engine missing")
    elif engine.get("builtin"):
        if name not in BUILTIN_ENGINE:
            problems.append(f"only {BUILTIN_ENGINE} may use the built-in engine block")
    else:
        for block in ("fab", "copper"):
            values = engine.get(block)
            prov = (engine.get("provenance") or {}).get(block) or {}
            if not isinstance(values, dict) or not values:
                problems.append(f"engine.{block} missing")
                continue
            for key in values:
                if key not in prov:
                    problems.append(f"engine.{block}.{key}: no provenance")
                    continue
                text = prov[key]
                if isinstance(text, str) and text.startswith("as fab."):
                    ref = text[len("as fab.") :]
                    if ref not in (engine.get("fab") or {}):
                        problems.append(f"engine.{block}.{key}: {text!r} names no fab key")
                    elif engine["fab"][ref] != values[key]:
                        problems.append(f"engine.{block}.{key} differs from fab.{ref}")
                else:
                    _need_src(f"engine.{block}.{key}", text, sources, problems)
            for key in prov:
                if key not in values:
                    problems.append(f"engine.provenance.{block}.{key}: no such value")
            for dotted, number in _numbers(values):
                if number <= 0:
                    problems.append(f"engine.{block}.{dotted}: must be positive")
        if not isinstance(engine.get("qualification"), str):
            problems.append("engine.qualification missing")
        fab = engine.get("fab") or {}
        for key in (
            "clearance_mm",
            "edge_clearance_mm",
            "hole_clearance_mm",
            "min_through_drill_mm",
            "via_annular_mm",
            "via_diameter_mm",
            "via_drill_mm",
            "smd_pad_clearance_mm",
            "component_pth_min_drill_mm",
            "npth_hole_clearance_mm",
            "pth_hole_clearance_mm",
            "pth_hole_to_hole_mm",
            "min_npth_drill_mm",
            "hole_to_edge_mm",
        ):
            if key not in fab:
                problems.append(f"engine.fab.{key} missing (pnr.fab_profile needs it)")
        for cls, via in (fab.get("via_classes") or {}).items():
            ring = (via.get("diameter_mm", 0) - via.get("drill_mm", 0)) / 2
            if fab.get("via_annular_mm") and ring < fab["via_annular_mm"] - 1e-9:
                problems.append(f"engine.fab.via_classes.{cls}: ring {ring:.4f} under the minimum")
            if (
                fab.get("min_through_drill_mm")
                and via.get("drill_mm", 0) < fab["min_through_drill_mm"] - 1e-9
                and not via.get("filled")
            ):
                problems.append(f"engine.fab.via_classes.{cls}: drill under the minimum")
    limits = doc.get("vendor_limits")
    if not isinstance(limits, dict):
        problems.append("vendor_limits missing")
    else:
        for key, value in limits.items():
            if key == "alternates":
                for i, alt in enumerate(value or []):
                    if not isinstance(alt, dict) or "stackup" not in alt:
                        problems.append(f"vendor_limits.alternates[{i}]: needs a stackup")
                    else:
                        _need_src(
                            f"vendor_limits.alternates[{i}]", alt.get("src"), sources, problems
                        )
                continue
            if not isinstance(value, dict):
                problems.append(f"vendor_limits.{key}: not an object")
                continue
            _need_src(f"vendor_limits.{key}", value.get("src"), sources, problems)
        qty = limits.get("qty") or {}
        if not isinstance(qty.get("multiple"), int) or not isinstance(qty.get("min"), int):
            problems.append("vendor_limits.qty needs integer multiple and min")
    defaults = doc.get("order_defaults")
    if not isinstance(defaults, dict) or defaults.get("service") not in (defaults or {}).get(
        "services", {}
    ):
        problems.append("order_defaults.service must name one of order_defaults.services")
    pricing = doc.get("pricing")
    if pricing is not None:
        _need_src("pricing", pricing.get("src"), sources, problems)
        services = (defaults or {}).get("services", {})
        for service, price in (pricing.get("per_sq_in") or {}).items():
            if service not in services:
                problems.append(f"pricing.per_sq_in.{service}: no such service")
            _positive(f"pricing.per_sq_in.{service}", price, problems)


def _validate_stackup(name: str, doc: Dict, sources: Dict, problems: List[str]) -> None:
    if doc.get("id") != name:
        problems.append("id must equal the file name")
    for key in ("vendor", "title", "vendor_name", "checkout"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            problems.append(f"{key} missing")
    thickness = doc.get("thickness_mm") or {}
    _positive("thickness_mm.value", thickness.get("value"), problems)
    _need_src("thickness_mm", thickness.get("src"), sources, problems)
    layers = doc.get("layers")
    if not isinstance(layers, list) or len(layers) < 3:
        problems.append("layers must list copper and dielectric layers top to bottom")
        return
    copper: List[str] = []
    previous = None
    for i, layer in enumerate(layers):
        where = f"layers[{i}]"
        kind = layer.get("kind") if isinstance(layer, dict) else None
        if kind not in ("mask", "copper", "dielectric"):
            problems.append(f"{where}: kind must be mask, copper or dielectric")
            continue
        _need_src(where, layer.get("src"), sources, problems)
        for key in list(layer):
            if key.endswith("_prior"):
                _need_src(f"{where}.{key}", layer.get(key + "_src"), sources, problems)
                _positive(f"{where}.{key}", layer[key], problems)
        if kind == "mask":
            _positive(f"{where}.thickness_mm", layer.get("thickness_mm"), problems, True)
            _positive(f"{where}.er", layer.get("er"), problems, True)
            if layer.get("side") not in ("F", "B"):
                problems.append(f"{where}: mask side must be F or B")
        elif kind == "copper":
            _positive(f"{where}.thickness_mm", layer.get("thickness_mm"), problems)
            if not _COPPER_NAME.match(str(layer.get("name"))):
                problems.append(f"{where}: copper name must be F.Cu, In<n>.Cu or B.Cu")
            copper.append(str(layer.get("name")))
            if previous == "copper":
                problems.append(f"{where}: two copper layers without a dielectric between")
        else:
            _positive(f"{where}.thickness_mm", layer.get("thickness_mm"), problems)
            _positive(f"{where}.er", layer.get("er"), problems, True)
            _positive(f"{where}.df", layer.get("df"), problems, True)
            _positive(f"{where}.er_freq_hz", layer.get("er_freq_hz"), problems, True)
            if layer.get("type") not in ("core", "prepreg"):
                problems.append(f"{where}: dielectric type must be core or prepreg")
        previous = kind
    expected = (
        ["F.Cu"] + [f"In{i}.Cu" for i in range(1, len(copper) - 1)] + ["B.Cu"]
        if len(copper) >= 2
        else []
    )
    if copper != expected:
        problems.append(f"copper layers {copper} must be F.Cu, In1.Cu.. B.Cu in order")


def _validate_vendor(name: str, doc: Dict, sources: Dict, problems: List[str]) -> None:
    if doc.get("vendor") != name:
        problems.append("vendor must equal the file name")
    for key in ("title", "country"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            problems.append(f"{key} missing")
    if not isinstance(doc.get("profiles_by_layers"), dict) or not doc["profiles_by_layers"]:
        problems.append("profiles_by_layers missing")
    gerbers = doc.get("gerbers") or {}
    if not isinstance(gerbers.get("layers"), list) or "Edge.Cuts" not in gerbers["layers"]:
        problems.append("gerbers.layers must include Edge.Cuts")
    _need_src("gerbers", gerbers.get("src"), sources, problems)
    drill = doc.get("drill") or {}
    if drill.get("units") not in ("in", "mm") or drill.get("zeros") not in (
        "decimal",
        "suppressleading",
        "suppresstrailing",
        "keep",
    ):
        problems.append("drill needs units (in/mm) and zeros")
    if drill.get("oval") not in ("alternate", "route"):
        problems.append("drill.oval must be alternate or route")
    names_ = drill.get("names") or {}
    if drill.get("separate_th"):
        if not {"pth", "npth"} <= set(names_):
            problems.append("drill.names needs pth and npth")
    elif "merged" not in names_:
        problems.append("drill.names needs merged")
    _need_src("drill", drill.get("src"), sources, problems)
    staging = doc.get("staging") or {}
    manual = staging.get("manual") or {}
    if not str(manual.get("url", "")).startswith("https://"):
        problems.append("staging.manual.url must be https")
    for mech, spec in staging.items():
        _need_src(f"staging.{mech}", spec.get("src"), sources, problems)
        if mech == "import_url" and "{url}" not in spec.get("template", ""):
            problems.append("staging.import_url.template needs {url}")
    assembly = doc.get("assembly")
    if assembly is not None:
        for part in ("bom", "cpl"):
            spec = assembly.get(part) or {}
            if not spec.get("columns") or not spec.get("file"):
                problems.append(f"assembly.{part} needs file and columns")
            _need_src(f"assembly.{part}", spec.get("src"), sources, problems)
        if assembly.get("part_key") not in ("lcsc", "mpn"):
            problems.append("assembly.part_key must be lcsc or mpn")


def check_all() -> List[str]:
    """Every problem in the data, cross-file references included (the data test runs this)."""
    problems: List[str] = []
    docs: Dict[str, Dict[str, Dict]] = {}
    for kind in KINDS:
        docs[kind] = {}
        for name in names(kind):
            try:
                docs[kind][name] = load(kind, name)
            except DataError as err:
                problems.append(str(err))
    for name, doc in docs["profiles"].items():
        if doc["vendor"] not in docs["vendors"]:
            problems.append(f"profile {name}: no vendor {doc['vendor']!r}")
        for sid in doc["stackups"]["allowed"]:
            st = docs["stackups"].get(sid)
            if st is None:
                problems.append(f"profile {name}: no stackup {sid!r}")
                continue
            if st["vendor"] != doc["vendor"]:
                problems.append(f"profile {name}: stackup {sid} is another vendor's")
            n_cu = sum(1 for layer in st["layers"] if layer["kind"] == "copper")
            if n_cu != doc["copper_layers"]:
                problems.append(f"profile {name}: stackup {sid} has {n_cu} copper layers")
        for alt in doc["vendor_limits"].get("alternates") or []:
            if alt["stackup"] not in doc["stackups"]["allowed"]:
                problems.append(f"profile {name}: alternate {alt['stackup']} not allowed")
        engine = doc["engine"]
        if not engine.get("builtin"):
            copper = engine["copper"]
            for sid in doc["stackups"]["allowed"]:
                st = docs["stackups"].get(sid)
                if st is None:
                    continue
                inner = [
                    layer["thickness_mm"]
                    for layer in st["layers"]
                    if layer["kind"] == "copper" and layer["name"].startswith("In")
                ]
                if inner and "inner_copper_um" in copper:
                    # The engine models no more copper than the stackup has (stricter is fine).
                    if copper["inner_copper_um"] > min(inner) * 1000 + 0.05:
                        problems.append(f"profile {name}: inner copper above stackup {sid}'s")
    for name, doc in docs["vendors"].items():
        for layers, pname in list(doc["profiles_by_layers"].items()) + list(
            (doc.get("profiles_in_pad") or {}).items()
        ):
            prof = docs["profiles"].get(pname)
            if prof is None:
                problems.append(f"vendor {name}: no profile {pname!r}")
            elif prof["vendor"] != name or str(prof["copper_layers"]) != str(layers):
                problems.append(f"vendor {name}: profile {pname} does not match {layers} layers")
    return problems


def all_sources() -> List[Dict[str, Any]]:
    """Every source cited by the data: key, url, accessed and the files citing it."""
    found: Dict[tuple, Dict[str, Any]] = {}
    for kind in KINDS:
        for name in names(kind):
            for key, src in load(kind, name)["sources"].items():
                entry = found.setdefault(
                    (key, src["url"], src["accessed"]), dict(key=key, **src, files=[])
                )
                entry["files"].append(f"{kind}/{name}.json")
    return sorted(found.values(), key=lambda e: (e["key"], e["accessed"]))


def oldest_access(doc: Dict[str, Any]) -> str:
    """The oldest access date among a data file's sources."""
    return min(src["accessed"] for src in doc["sources"].values())


def stale_sources(docs, today: Optional[datetime.date] = None, days: int = STALE_DAYS) -> List[str]:
    """Source keys (with dates) older than ``days`` among the given data files."""
    today = today or datetime.date.today()
    out = set()
    for doc in docs:
        for key, src in doc["sources"].items():
            accessed = datetime.date.fromisoformat(src["accessed"])
            if (today - accessed).days > days:
                out.add(f"{key} ({src['accessed']})")
    return sorted(out)
