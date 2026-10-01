"""A project's parts lock: which cached parts its build uses, pinned by content id.

``yapnr-parts.lock.json`` sits next to ``ato.yaml``::

    {
      "schema": "yapnr-atopile-parts-lock-v1",
      "parts_dir": "elec/src/parts",
      "parts": [{"name": "Yapnr_Synthetic_SR1K", "id": "<sha256>", "lcsc": "C990000001"}]
    }

Before each build the runner writes every locked part into ``<parts_dir>/<name>/`` of its copy
of the project, after checking each file against the cache's sha256. A project can then keep its
part directories out of version control (they are derived data, possibly under a third party's
terms) and still build reproducibly. The picker answers from the catalog entries of the locked
LCSC ids only, so a type pick can choose only among parts the build can attach offline.

``lock_directory`` writes a lock for part directories a project already has (and can upload
them to a cache first); that is how an existing project moves its parts into a cache.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from yapnr.partcache import model
from yapnr.partcache.client import PartCache, dir_part_id, materialize

LOCK_NAME = "yapnr-parts.lock.json"
LOCK_SCHEMA = "yapnr-atopile-parts-lock-v1"
DEFAULT_PARTS_DIR = "elec/src/parts"


class LockError(ValueError):
    pass


def parts_dir_of(project: Path) -> str:
    """The parts directory of an atopile project (``paths.parts`` in ato.yaml, else default)."""
    text = (project / "ato.yaml").read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        doc = yaml.safe_load(text) or {}
        paths = doc.get("paths") or {}
        if paths.get("parts"):
            return str(paths["parts"])
        if paths.get("src"):
            return f"{paths['src']}/parts"
    except ImportError:
        if re.search(r"^paths:", text, re.M):
            raise LockError("ato.yaml sets paths: reading it needs PyYAML")
    return DEFAULT_PARTS_DIR


def _check_relative(path: str) -> str:
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or ".." in parts:
        raise LockError(f"parts_dir {path!r} must be a relative path inside the project")
    return path


def load(project: Path) -> Optional[Dict[str, Any]]:
    """The project's validated lock, or None if it has none."""
    path = Path(project) / LOCK_NAME
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    return validate(doc, str(path))


def validate(doc: Any, where: str = LOCK_NAME) -> Dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != LOCK_SCHEMA:
        raise LockError(f"{where}: not a {LOCK_SCHEMA} document")
    _check_relative(str(doc.get("parts_dir", "")))
    names = set()
    for entry in doc.get("parts", []):
        if not isinstance(entry, dict) or not model.is_sha256(entry.get("id")):
            raise LockError(f"{where}: each part needs a name and a sha256 id")
        model.check_name(entry.get("name"))
        if entry["name"] in names:
            raise LockError(f"{where}: part {entry['name']} is locked twice")
        names.add(entry["name"])
    return doc


def dump(doc: Dict[str, Any]) -> str:
    parts = sorted(doc["parts"], key=lambda p: p["name"])
    return json.dumps({**doc, "parts": parts}, indent=2, sort_keys=True) + "\n"


def lock_directory(
    project: Path,
    cache: Optional[PartCache] = None,
    upload: bool = False,
    imported_from: str = "project",
    licence_note: Optional[str] = None,
) -> Dict[str, Any]:
    """A lock for the part directories the project has now (uploading them first if asked).

    Without ``upload`` every part must already be in ``cache`` (when one is given), with exactly
    these files.
    """
    from yapnr.partcache.importer import import_part_dirs, part_dirs_under

    project = Path(project)
    parts_dir = parts_dir_of(project)
    dirs = part_dirs_under(project / parts_dir) if (project / parts_dir).is_dir() else []
    if upload:
        if cache is None:
            raise LockError("uploading needs a cache")
        records = import_part_dirs(cache, dirs, imported_from, licence_note=licence_note)
        # (ImportFailed propagates: a lock must cover every part directory.)
    else:
        records = []
        for part_dir in dirs:
            pid = dir_part_id(part_dir)
            record = {"name": part_dir.name, "id": pid}
            if cache is not None:
                manifest = cache.manifest(pid)
                record["lcsc"] = manifest.get("lcsc")
            else:
                name = part_dir.name
                facts = model.ato_facts(name, (part_dir / f"{name}.ato").read_text("utf-8"))
                record["lcsc"] = facts["lcsc"]
            records.append(record)
    entries = [
        {k: v for k, v in (("name", r["name"]), ("id", r["id"]), ("lcsc", r.get("lcsc"))) if v}
        for r in records
    ]
    return {"schema": LOCK_SCHEMA, "parts_dir": parts_dir, "parts": entries}


def materialize_lock(
    project: Path, lock: Dict[str, Any], cache: PartCache, replace: bool = False
) -> List[Path]:
    """Write every locked part into the project; an existing different part is an error."""
    parts_dir = Path(project) / _check_relative(lock["parts_dir"])
    parts_dir.mkdir(parents=True, exist_ok=True)
    return [materialize(cache, e["id"], parts_dir, replace=replace) for e in lock["parts"]]


def locked_lcsc(lock: Dict[str, Any], cache: PartCache) -> List[str]:
    """The LCSC ids of the locked parts (from the lock, else from the part manifests)."""
    out = []
    for entry in lock["parts"]:
        lcsc = entry.get("lcsc") or cache.manifest(entry["id"]).get("lcsc")
        if lcsc:
            out.append(lcsc)
    return sorted(set(out))
