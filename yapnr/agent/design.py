"""Read live article sources and a complete exported netlist, never partial writes."""

import hashlib
import json
import os
from pathlib import Path

from yapnr.agent import workspace

SKIP = {".git", ".yapnr", ".ato", "node_modules", ".venv", "venv", "__pycache__"}


def snapshot(project):
    root = Path(project).resolve()
    sources, graphs, files = [], [], []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            path = Path(base) / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if len(files) < 4096:
                files.append({"path": path.relative_to(root).as_posix(), "bytes": size})
            if path.suffix != ".ato" and name != "graph.json":
                continue
            relative = path.relative_to(root).as_posix()
            try:
                path = workspace.relative_file(root, relative)
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                if path.suffix == ".ato" and len(sources) < 128:
                    sources.append(
                        {
                            "path": relative,
                            "sha256": sha,
                            "text": raw.decode("utf-8", "replace"),
                            "modified": path.stat().st_mtime_ns,
                        }
                    )
                elif name == "graph.json":
                    data = json.loads(raw)
                    if (
                        isinstance(data, dict)
                        and isinstance(data.get("components"), list)
                        and isinstance(data.get("nets"), list)
                    ):
                        graphs.append(
                            {
                                "path": relative,
                                "sha256": sha,
                                "graph": data,
                                "modified": path.stat().st_mtime_ns,
                            }
                        )
            except (OSError, ValueError):
                continue  # Atomic exporters may be replacing this file while we read.
    config = workspace.read_json(root / ".yapnr/workspace/design.json", {})
    selected = next((g for g in graphs if g["path"] == config.get("graph")), None)
    if not selected and len(graphs) == 1:
        selected = graphs[0]
    reason = "Waiting for the first complete exported graph.json."
    if len(graphs) > 1 and not selected:
        reason = "Multiple netlists: select the active graph in .yapnr/workspace/design.json."
    changed = bool(selected and any(f["modified"] > selected["modified"] for f in sources))
    return {
        "source_newer": changed,
        "sources": sources,
        "files": files,
        "schematic": selected,
        "status": "ready" if selected else "waiting",
        "reason": None if selected else reason,
    }


def update(project, name, text, expected):
    root = Path(project).resolve()
    if (
        not isinstance(name, str)
        or Path(name).suffix != ".ato"
        or any(p in SKIP for p in Path(name).parts)
    ):
        raise ValueError("Edit a project atopile source, not a dependency")
    if not isinstance(text, str) or len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError("Source exceeds the size limit")
    with workspace.lock(root) as folder:
        path = workspace.relative_file(root, name)
        previous = path.read_bytes()
        if workspace.sha(previous) != expected:
            raise ValueError("Source changed concurrently; review the current source before saving")
        workspace.blob(folder, previous, ".ato")
        workspace.atomic(path, text.encode())
        result = {"path": name, "sha256": workspace.file_sha(path), "previous_sha256": expected}
    workspace.record(root, {"source": "user-source-edit", **result})
    return result


def read_source(project, name):
    """Bounded, read-only article file access; edits retain the narrower ato contract."""
    path = workspace.relative_file(project, name)
    if any(part in SKIP for part in Path(name).parts):
        raise ValueError("Managed source is available through the reference browser")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("File exceeds the source browser limit")
    raw = path.read_bytes()
    if b"\0" in raw:
        raise ValueError("Binary file: use the artifact viewer")
    return {
        "path": name,
        "text": raw.decode("utf-8"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified": path.stat().st_mtime_ns,
    }


def experiments(project):
    from yapnr.experiments import attempts

    return attempts(project)


def search(project, query):
    query = str(query).strip().casefold()[:200]
    if not query:
        return []
    data = snapshot(project)
    results = []
    for source in data["sources"]:
        if query in source["path"].casefold():
            results.append(
                {
                    "kind": "source",
                    "file": source["path"],
                    "line": 1,
                    "text": source["path"],
                    "sha256": source["sha256"],
                }
            )
        for line, text in enumerate(source["text"].splitlines(), 1):
            if query in text.casefold():
                results.append(
                    {
                        "kind": "source",
                        "file": source["path"],
                        "line": line,
                        "text": text.strip()[:240],
                        "sha256": source["sha256"],
                    }
                )
                if len(results) >= 100:
                    return results
    return results[:100]
