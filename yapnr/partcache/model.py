"""The part cache's records: part manifests, catalog entries, and how they are hashed and checked.

A **part** is one atopile part directory (the ``.ato`` file with its footprint, symbol, 3D model
and notes), stored by content: every file is a blob named by its sha256, and the part's id is
the sha256 of its canonical file list (``part_id``). The facts that identify it (LCSC id,
manufacturer, part number) are read from the ``.ato`` file, so they are fixed by the id too.
Where the files came from (``provenance``) and under what terms (``licence``) are required
metadata stored with the manifest; they are not part of the id.

A **catalog entry** is what the picker needs to choose a part: the ``yapnr-picker-catalog-v1``
record of one LCSC id (kind, package, typed parameters). Entries are mutable facts, replaced by a
newer upload, each with its own provenance.

Stdlib only (the server runs in a minimal container).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

PART_SCHEMA = "yapnr-part-v1"
CACHE_SCHEMA = "yapnr-part-cache-v1"

# File types a part may carry (case-insensitive). Anything else is refused on upload, and each
# file's content must match its type (``check_file_content``). With the server's other rules
# (it serves a file only while a part uses it, and removes files no part took up), the cache
# holds part files, not arbitrary content.
ALLOWED_SUFFIXES = (".ato", ".kicad_mod", ".kicad_sym", ".step", ".stp", ".wrl", ".md", ".txt")
TEXT_SUFFIXES = (".ato", ".kicad_mod", ".kicad_sym", ".md", ".txt")
# What a file of each type starts with (after an optional byte-order mark and white space).
_MAGIC = {
    ".kicad_mod": (b"(footprint", b"(module"),
    ".kicad_sym": (b"(kicad_symbol_lib",),
    ".step": (b"ISO-10303-21",),
    ".stp": (b"ISO-10303-21",),
    ".wrl": (b"#VRML",),
}
CONTENT_HEAD_BYTES = 4096
MAX_FILES = 32
DEFAULT_MAX_FILE_BYTES = 64 << 20

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_]{0,127}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.,+#()&' -]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAIT_RE = re.compile(r"^\s*trait\s+([A-Za-z_][A-Za-z0-9_:]*)\s*<(.*)>\s*$", re.M)
_ARG_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')

LICENCE_KEYS = ("spdx", "notes", "terms_url", "distribution")
# licence.distribution: "shareable" (the default when absent) or "local-only". A local-only part
# is never uploaded to a server, and a server bound beyond loopback refuses a root holding one.
DISTRIBUTIONS = ("shareable", "local-only")
LOCAL_ONLY = "local-only"
PROVENANCE_KEYS = ("source", "generator", "created", "imported_from", "notes")


class InvalidPart(ValueError):
    """A part or catalog entry that the cache refuses; the message says why."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InvalidPart(f"part name {name!r} must be 1-128 of A-Z a-z 0-9 _")
    return name


def check_file_path(path: Any) -> str:
    """A part file name: one path component, an allowed suffix, no traversal."""
    if not isinstance(path, str) or "/" in path or "\\" in path or not _FILE_RE.match(path):
        raise InvalidPart(f"file name {path!r} is not a plain file name")
    if PurePosixPath(path).name != path or path in (".", ".."):
        raise InvalidPart(f"file name {path!r} is not a plain file name")
    if not path.lower().endswith(ALLOWED_SUFFIXES):
        raise InvalidPart(f"file {path!r}: only {', '.join(ALLOWED_SUFFIXES)} files are stored")
    return path


def _suffix(path: str) -> str:
    lower = path.lower()
    return next((s for s in ALLOWED_SUFFIXES if lower.endswith(s)), "")


def needs_full_content(path: str) -> bool:
    """Whether ``check_file_content`` needs the whole file (text types) or only its head."""
    return _suffix(path) in TEXT_SUFFIXES


def check_file_content(path: str, data: bytes) -> None:
    """Refuse a file whose content does not match its type.

    ``data`` is the whole file for text types (``needs_full_content``), else at least its first
    ``CONTENT_HEAD_BYTES`` bytes. Text types must be UTF-8; KiCad files must be S-expressions of
    their kind; STEP and VRML models must start with their format's header.
    """
    suffix = _suffix(path)
    if suffix in TEXT_SUFFIXES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as err:
            raise InvalidPart(f"file {path!r} is not UTF-8 text") from err
    magic = _MAGIC.get(suffix)
    if magic and not data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(magic):
        raise InvalidPart(f"file {path!r} does not start like a {suffix} file")


def is_local_only(manifest: Mapping[str, Any]) -> bool:
    return (manifest.get("licence") or {}).get("distribution") == LOCAL_ONLY


def part_id(name: str, files: Iterable[Mapping[str, Any]]) -> str:
    """The content id of a part: sha256 over its schema, name and (path, sha256, size) list."""
    listing = sorted((f["path"], f["sha256"], int(f["size"])) for f in files)
    return sha256_bytes(canonical_json({"schema": PART_SCHEMA, "name": name, "files": listing}))


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def ato_traits(text: str) -> Dict[str, Dict[str, str]]:
    """The ``trait name<key="value", ...>`` declarations of an atopile part file."""
    traits: Dict[str, Dict[str, str]] = {}
    for match in _TRAIT_RE.finditer(text):
        args = {k: _unescape(v) for k, v in _ARG_RE.findall(match.group(2))}
        traits.setdefault(match.group(1), args)
    return traits


def ato_facts(name: str, text: str) -> Dict[str, Any]:
    """What the part's ``.ato`` says about it: identity, referenced files, generation source."""
    traits = ato_traits(text)
    atomic = traits.get("is_atomic_part")
    if atomic is None:
        raise InvalidPart(f"{name}.ato has no is_atomic_part trait")
    if not re.search(r"^component\s+[A-Za-z_][A-Za-z0-9_]*\s*:", text, re.M):
        raise InvalidPart(f"{name}.ato defines no component")
    picked = traits.get("has_part_picked::by_supplier", {})
    lcsc = None
    if picked.get("supplier_id", "").lower() == "lcsc" and picked.get("supplier_partno"):
        lcsc = picked["supplier_partno"].strip().upper()
        if not re.match(r"^C[1-9][0-9]{0,11}$", lcsc):
            raise InvalidPart(f"{name}.ato: supplier_partno {lcsc!r} is not an LCSC id")
    generated = traits.get("is_auto_generated")
    return {
        "lcsc": lcsc,
        "manufacturer": atomic.get("manufacturer") or picked.get("manufacturer") or "",
        "mpn": atomic.get("partnumber") or picked.get("partno") or "",
        "references": [atomic[k] for k in ("footprint", "symbol") if atomic.get(k)],
        "optional_references": [atomic["model"]] if atomic.get("model") else [],
        "generated_from": generated.get("source") if generated else None,
        "generated_at": generated.get("date") if generated else None,
    }


def check_text_map(value: Any, keys: Tuple[str, ...], what: str, required: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise InvalidPart(f"{what} must be an object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise InvalidPart(f"{what}: unknown keys {unknown}")
    out = {}
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, str) or len(item) > 2000:
            raise InvalidPart(f"{what}.{key} must be a string (at most 2000 characters)")
        out[key] = item
    if not out.get(required):
        raise InvalidPart(f"{what}.{required} is required")
    return out


def check_manifest_request(
    doc: Any, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> Dict[str, Any]:
    """Validate an upload request (name, files, provenance, licence) before blobs are looked at."""
    if not isinstance(doc, dict):
        raise InvalidPart("a part upload must be a JSON object")
    unknown = sorted(set(doc) - {"schema", "name", "files", "provenance", "licence"})
    if unknown:
        raise InvalidPart(f"unknown keys {unknown}")
    if doc.get("schema", PART_SCHEMA) != PART_SCHEMA:
        raise InvalidPart(f"schema must be {PART_SCHEMA!r}")
    name = check_name(doc.get("name"))
    files = doc.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise InvalidPart(f"files must be a list of 1 to {MAX_FILES} entries")
    seen = set()
    clean: List[Dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise InvalidPart("each file must be {path, sha256, size}")
        path = check_file_path(entry["path"])
        if path.lower() in seen:
            raise InvalidPart(f"file {path!r} repeats")
        seen.add(path.lower())
        if not is_sha256(entry["sha256"]):
            raise InvalidPart(f"file {path!r}: sha256 must be 64 lower-case hex digits")
        size = entry["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= max_file_bytes:
            raise InvalidPart(f"file {path!r}: size must be 0..{max_file_bytes} bytes")
        clean.append({"path": path, "sha256": entry["sha256"], "size": size})
    if f"{name}.ato" not in {f["path"] for f in clean}:
        raise InvalidPart(f"a part named {name} must include {name}.ato")
    provenance = check_text_map(doc.get("provenance"), PROVENANCE_KEYS, "provenance", "source")
    licence = check_text_map(doc.get("licence"), LICENCE_KEYS, "licence", "spdx")
    if licence.get("distribution", DISTRIBUTIONS[0]) not in DISTRIBUTIONS:
        raise InvalidPart(f"licence.distribution must be one of {', '.join(DISTRIBUTIONS)}")
    return {
        "name": name,
        "files": sorted(clean, key=lambda f: f["path"]),
        "provenance": provenance,
        "licence": licence,
    }


def build_manifest(
    request: Dict[str, Any], ato_text: str, uploaded: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """The stored manifest of a validated upload whose ``.ato`` text is ``ato_text``."""
    facts = ato_facts(request["name"], ato_text)
    names = {f["path"] for f in request["files"]}
    missing = [ref for ref in facts["references"] if ref not in names]
    if missing:
        raise InvalidPart(f"{request['name']}.ato references files that are missing: {missing}")
    warnings = [
        f"the 3D model {ref} that the .ato names is not part of the upload"
        for ref in facts["optional_references"]
        if ref not in names
    ]
    manifest = {
        "schema": PART_SCHEMA,
        "id": part_id(request["name"], request["files"]),
        "name": request["name"],
        "lcsc": facts["lcsc"],
        "manufacturer": facts["manufacturer"],
        "mpn": facts["mpn"],
        "generated_from": facts["generated_from"],
        "files": request["files"],
        "provenance": request["provenance"],
        "licence": request["licence"],
    }
    if warnings:
        manifest["warnings"] = warnings
    if uploaded:
        manifest["uploaded"] = dict(uploaded)
    return manifest


def check_manifest(doc: Any) -> Dict[str, Any]:
    """Re-check a stored or downloaded manifest, including that its id matches its files."""
    request = check_manifest_request(
        (
            {k: doc.get(k) for k in ("schema", "name", "files", "provenance", "licence")}
            if isinstance(doc, dict)
            else doc
        ),
        max_file_bytes=1 << 62,
    )
    if doc.get("id") != part_id(request["name"], request["files"]):
        raise InvalidPart(f"manifest id {doc.get('id')!r} does not match its files")
    return doc


def summary(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The listing form of a manifest."""
    return {
        "id": manifest["id"],
        "name": manifest["name"],
        "lcsc": manifest.get("lcsc"),
        "manufacturer": manifest.get("manufacturer"),
        "mpn": manifest.get("mpn"),
        "size": sum(f["size"] for f in manifest["files"]),
        "uploaded_at": (manifest.get("uploaded") or {}).get("at"),
        "licence": (manifest.get("licence") or {}).get("spdx"),
    }
