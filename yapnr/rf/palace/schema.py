"""Offline validation of Palace configuration files against Palace's own JSON schema.

``third_party/palace/config-schema.json`` is Palace's ``scripts/schema/config-schema.json``,
vendored unmodified at the commit the task image builds (THIRD_PARTY.md, "Palace configuration
schema"); Palace validates every configuration against the same document at start-up. A small
JSON Schema draft-07 validator (the keywords that schema uses, stdlib only) checks a config here,
so tests and ``yapnr.rf.palace`` catch a misspelt key or a wrong type before a cloud run; the
image's ``palace --dry-run`` remains the final word. Keys the schema marks
``x-palace-deprecated`` are reported too.

A checkout (and Bazel's runfiles) has the schema; an installed wheel does not, and ``validate``
then returns None.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_PATH = Path("third_party") / "palace" / "config-schema.json"
PALACE_COMMIT = "b797ea8060a52241cd9ab1176199f06af8585816"  # awslabs/palace main, 2026-10-02

_CACHE: Dict[str, Dict[str, Any]] = {}


def find_schema() -> Optional[Path]:
    here = Path(__file__)
    for base in (here.parent, here.resolve().parent):
        for parent in base.parents:
            candidate = parent / SCHEMA_PATH
            if candidate.is_file():
                return candidate
    return None


def load(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target = path or find_schema()
    if target is None:
        return None
    key = str(target)
    if key not in _CACHE:
        _CACHE[key] = json.loads(Path(target).read_text())
    return _CACHE[key]


def version(schema: Dict[str, Any]) -> str:
    """The schema's own version, from its ``$id`` (``urn:palace:schema:2-1-0``)."""
    return str(schema.get("$id", "")).rsplit(":", 1)[-1]


def validate(
    config: Dict[str, Any], schema: Optional[Dict[str, Any]] = None, deprecated: bool = True
) -> Optional[List[str]]:
    """Every finding for ``config``; [] when it conforms, None when no schema is available."""
    schema = schema if schema is not None else load()
    if schema is None:
        return None
    errors: List[str] = []
    _check(config, schema, schema, "$", errors, deprecated)
    return errors


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and math.isfinite(v),
}


def _resolve(ref: str, root: Dict[str, Any]) -> Dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref is supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _check(
    v: Any, s: Any, root: Dict[str, Any], where: str, errs: List[str], deprecated: bool
) -> None:
    if s is True or s is None:
        return
    if s is False:
        errs.append(f"{where}: not allowed")
        return
    if "$ref" in s:
        _check(v, _resolve(s["$ref"], root), root, where, errs, deprecated)
    t = s.get("type")
    if t is not None:
        kinds = t if isinstance(t, list) else [t]
        if not any(_TYPES[k](v) for k in kinds):
            errs.append(f"{where}: expected {'/'.join(kinds)}, got {type(v).__name__}")
            return
    if "const" in s and not _equal(v, s["const"]):
        errs.append(f"{where}: must be {s['const']!r}")
    if "enum" in s and not any(_equal(v, e) for e in s["enum"]):
        errs.append(f"{where}: {v!r} not one of {s['enum']}")
    if _TYPES["number"](v):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{where}: {v} < minimum {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{where}: {v} > maximum {s['maximum']}")
        if "exclusiveMinimum" in s and v <= s["exclusiveMinimum"]:
            errs.append(f"{where}: {v} <= exclusive minimum {s['exclusiveMinimum']}")
        if "exclusiveMaximum" in s and v >= s["exclusiveMaximum"]:
            errs.append(f"{where}: {v} >= exclusive maximum {s['exclusiveMaximum']}")
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            errs.append(f"{where}: shorter than {s['minLength']}")
        if "maxLength" in s and len(v) > s["maxLength"]:
            errs.append(f"{where}: longer than {s['maxLength']}")
    if isinstance(v, dict):
        props = s.get("properties", {})
        for key in s.get("required", []):
            if key not in v:
                errs.append(f"{where}: missing required {key!r}")
        for key, item in v.items():
            if key in props:
                if (
                    deprecated
                    and isinstance(props[key], dict)
                    and props[key].get("x-palace-deprecated")
                ):
                    errs.append(f"{where}.{key}: deprecated in Palace's schema")
                _check(item, props[key], root, f"{where}.{key}", errs, deprecated)
            elif "additionalProperties" in s:
                ap = s["additionalProperties"]
                if ap is False:
                    errs.append(f"{where}: unknown key {key!r}")
                elif isinstance(ap, dict):
                    _check(item, ap, root, f"{where}.{key}", errs, deprecated)
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            errs.append(f"{where}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            errs.append(f"{where}: more than {s['maxItems']} items")
        items = s.get("items")
        if isinstance(items, list):
            for i, item in enumerate(v):
                if i < len(items):
                    _check(item, items[i], root, f"{where}[{i}]", errs, deprecated)
                elif s.get("additionalItems") is False:
                    errs.append(f"{where}[{i}]: no additional items allowed")
                elif isinstance(s.get("additionalItems"), dict):
                    _check(item, s["additionalItems"], root, f"{where}[{i}]", errs, deprecated)
        elif isinstance(items, dict):
            for i, item in enumerate(v):
                _check(item, items, root, f"{where}[{i}]", errs, deprecated)
        if "contains" in s and not any(not _sub(item, s["contains"], root) for item in v):
            errs.append(f"{where}: no item matches 'contains'")
    for sub in s.get("allOf", []):
        _check(v, sub, root, where, errs, deprecated)
    if "anyOf" in s and not any(not _sub(v, sub, root) for sub in s["anyOf"]):
        errs.append(f"{where}: matches none of anyOf ({_first(v, s['anyOf'], root)})")
    if "oneOf" in s:
        ok = [i for i, sub in enumerate(s["oneOf"]) if not _sub(v, sub, root)]
        if len(ok) != 1:
            detail = _first(v, s["oneOf"], root) if not ok else f"options {ok}"
            errs.append(f"{where}: must match exactly one of oneOf, matches {len(ok)} ({detail})")
    if "not" in s and not _sub(v, s["not"], root):
        errs.append(f"{where}: matches a 'not' schema")
    if "if" in s:
        if not _sub(v, s["if"], root):
            if "then" in s:
                _check(v, s["then"], root, where, errs, deprecated)
        elif "else" in s:
            _check(v, s["else"], root, where, errs, deprecated)


def _sub(v: Any, s: Any, root: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    _check(v, s, root, "", errs, False)
    return errs


def _first(v: Any, subs: List[Any], root: Dict[str, Any]) -> str:
    best = min((_sub(v, sub, root) for sub in subs), key=len, default=[])
    return best[0].lstrip(": ") if best else ""
