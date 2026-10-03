"""Offline validation of Batch job JSON against Google's Batch v1 discovery document.

The discovery document (``third_party/googleapis/batch-v1.json``, vendored unmodified) describes
every field of the REST API. ``validate`` walks a job against it: unknown fields, wrong types,
values outside an enum, output-only fields set by the client, malformed int64 and duration
strings. It does not know which fields Batch requires or which combinations it accepts; the
renderer's tests and the first smoke job cover those.

A checkout (and Bazel's runfiles) has the document; an installed wheel does not, and ``plan``
then says the job JSON was not validated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DISCOVERY = Path("third_party") / "googleapis" / "batch-v1.json"
DURATION_RE = re.compile(r"^-?[0-9]+(\.[0-9]{1,9})?s$")
INT_RE = re.compile(r"^-?[0-9]+$")


def find_discovery() -> Optional[Path]:
    here = Path(__file__)
    for base in (here.parent, here.resolve().parent):
        for parent in base.parents:
            candidate = parent / DISCOVERY
            if candidate.is_file():
                return candidate
    return None


_CACHE: Dict[str, Dict[str, Any]] = {}


def load(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The discovery document, or None when this installation does not ship it."""
    target = path or find_discovery()
    if target is None:
        return None
    key = str(target)
    if key not in _CACHE:
        _CACHE[key] = json.loads(Path(target).read_text())
    return _CACHE[key]


def revision(doc: Dict[str, Any]) -> str:
    return "%s %s" % (doc.get("version"), doc.get("revision"))


def validate(value: Any, doc: Dict[str, Any], schema: str = "Job") -> List[str]:
    """Every finding for ``value`` as an instance of ``schema``; empty when it conforms."""
    errors: List[str] = []
    _check(value, {"$ref": schema}, doc["schemas"], schema, errors)
    return errors


def _check(
    value: Any, prop: Dict[str, Any], schemas: Dict[str, Any], where: str, errors: List[str]
):
    if "$ref" in prop:
        schema = schemas[prop["$ref"]]
        if not isinstance(value, dict):
            errors.append("%s: expected an object (%s)" % (where, prop["$ref"]))
            return
        props = schema.get("properties", {})
        for key, item in value.items():
            if key not in props:
                errors.append("%s.%s: not a field of %s" % (where, key, prop["$ref"]))
                continue
            field = props[key]
            if field.get("readOnly"):
                errors.append("%s.%s: output only" % (where, key))
            if field.get("deprecated"):
                errors.append("%s.%s: deprecated" % (where, key))
            _check(item, field, schemas, "%s.%s" % (where, key), errors)
        return
    kind = prop.get("type")
    fmt = prop.get("format")
    if kind == "object":
        if not isinstance(value, dict):
            errors.append("%s: expected an object" % where)
            return
        extra = prop.get("additionalProperties")
        if extra:
            for key, item in value.items():
                _check(item, extra, schemas, "%s[%s]" % (where, key), errors)
        return
    if kind == "array":
        if not isinstance(value, list):
            errors.append("%s: expected an array" % where)
            return
        for index, item in enumerate(value):
            _check(item, prop.get("items", {}), schemas, "%s[%d]" % (where, index), errors)
        return
    if kind == "boolean":
        if not isinstance(value, bool):
            errors.append("%s: expected a boolean" % where)
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("%s: expected an integer" % where)
        elif fmt == "int32" and not -(2**31) <= value < 2**31:
            errors.append("%s: outside int32" % where)
        return
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append("%s: expected a number" % where)
        return
    if kind == "string":
        if fmt in ("int64", "uint64"):
            # Proto3 JSON takes 64-bit integers as numbers or decimal strings.
            ok = (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, str) and INT_RE.match(value)
            )
            if not ok:
                errors.append("%s: expected an %s" % (where, fmt))
            return
        if not isinstance(value, str):
            errors.append("%s: expected a string" % where)
            return
        if fmt == "google-duration" and not DURATION_RE.match(value):
            errors.append("%s: %r is not a duration such as '300s'" % (where, value))
        if "enum" in prop:
            if value not in prop["enum"]:
                errors.append("%s: %r is not one of %s" % (where, value, ", ".join(prop["enum"])))
            else:
                deprecated = prop.get("enumDeprecated") or []
                index = prop["enum"].index(value)
                if index < len(deprecated) and deprecated[index]:
                    errors.append("%s: %r is deprecated" % (where, value))
        return
    errors.append("%s: the discovery document has no type here" % where)
