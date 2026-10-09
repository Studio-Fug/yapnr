"""Workspace experiment attempts and content-based artifact provenance."""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from yapnr.events import record

KINDS = ("discovery", "parts", "schematic", "pnr", "simulation", "validation", "other")
REGISTRY = ".yapnr/workspace/experiments.json"
SCHEMA = "yapnr-experiments-v1"


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="experiment-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read(path, fallback=None, limit=1024 * 1024):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return fallback
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else fallback
    except (OSError, ValueError):
        return fallback


def workspace_root(project):
    project = Path(project).resolve()
    for root in (project, *project.parents):
        if (root / ".yapnr/workflow").is_dir() or (root / "workspace.json").is_file():
            return root
    return project


@contextmanager
def locked(root):
    folder = root / ".yapnr/workspace"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield folder


def reference(root, path, expected=None):
    """A path is a locator, while a hash identifies the exact artifact used."""
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Experiment files must stay inside the workspace")
    item = {"path": path.relative_to(root).as_posix(), "sha256": expected}
    if not path.is_file():
        return {**item, "verification": "missing"}
    if path.stat().st_size > 64 * 1024 * 1024:
        return {**item, "verification": "not checked: file exceeds hash limit"}
    actual = digest(path.read_bytes())
    return {
        **item,
        "sha256": expected or actual,
        "verification": (
            "verified" if expected and expected == actual else "changed" if expected else "observed"
        ),
    }


def load(root):
    path = Path(root) / REGISTRY
    data = (
        read(path, limit=32 * 1024 * 1024) if path.exists() else {"schema": SCHEMA, "attempts": []}
    )
    if data is None:
        raise ValueError("Experiment registry is unreadable")
    if data.get("schema") != SCHEMA or not isinstance(data.get("attempts"), list):
        raise ValueError("Unsupported experiment registry")
    return data


def begin(project, kind, title, inputs=(), parameters=None, parent=None):
    if kind not in KINDS or not title.strip():
        raise ValueError("Experiment needs a supported type and title")
    root = Path(project).resolve()
    refs = [reference(root, path) for path in inputs]
    if any(ref["verification"] != "observed" for ref in refs):
        raise ValueError("Experiment inputs must exist and fit the hash limit")
    with locked(root):
        data = load(root)
        if parent and not any(item["id"] == parent for item in data["attempts"]):
            raise ValueError("Parent experiment does not exist")
        item = {
            "id": "E%06d" % (len(data["attempts"]) + 1),
            "kind": kind,
            "title": title,
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
            "inputs": refs,
            "outputs": [],
            "parameters": parameters or {},
            "parent": parent,
            "origin": "recorded",
        }
        for ref in refs:
            source = root / ref["path"]
            raw = source.read_bytes()
            if digest(raw) != ref["sha256"]:
                raise ValueError("Experiment input changed during capture")
            stored = root / ".yapnr/workspace/objects" / (ref["sha256"] + source.suffix)
            if stored.exists() and (stored.is_symlink() or stored.read_bytes() != raw):
                raise ValueError("Experiment input object is corrupt")
            if not stored.exists():
                atomic(stored, raw)
            ref.update(object_path=stored.relative_to(root).as_posix(), verification="verified")
        data["attempts"].append(item)
        atomic(root / REGISTRY, encoded(data))
    record(root, {"type": "experiment-start", "experiment": item})
    return item


def finish(project, identifier, status, outputs=(), summary=None):
    if status not in ("passed", "failed", "cancelled", "timed out"):
        raise ValueError("Experiment needs a terminal status")
    root = Path(project).resolve()
    refs = [reference(root, path) for path in outputs]
    if any(ref["verification"] != "observed" for ref in refs):
        raise ValueError("Experiment outputs must exist and fit the hash limit")
    with locked(root) as folder:
        data = load(root)
        item = next((item for item in data["attempts"] if item["id"] == identifier), None)
        if not item or item["status"] != "running":
            raise ValueError("Experiment is missing or already completed")
        artifact_path = folder / "artifacts.json"
        artifacts = (
            read(artifact_path, limit=32 * 1024 * 1024)
            if artifact_path.exists()
            else {"schema": "yapnr-artifacts-v1", "artifacts": []}
        )
        if artifacts is None:
            raise ValueError("Artifact registry is unreadable")
        for ref in refs:
            source = root / ref["path"]
            raw = source.read_bytes()
            if digest(raw) != ref["sha256"]:
                raise ValueError("Experiment output changed during capture")
            stored = folder / "objects" / (ref["sha256"] + source.suffix)
            if stored.exists() and stored.read_bytes() != raw:
                raise ValueError("Experiment artifact object is corrupt")
            if not stored.exists():
                atomic(stored, raw)
            kind = (
                "board"
                if source.suffix == ".kicad_pcb"
                else "image" if source.suffix in (".png", ".svg") else "report"
            )
            metadata = {"experiment": identifier}
            title = item["title"] + " · " + source.name
            artifact_id = digest(
                encoded(
                    {"sha256": ref["sha256"], "kind": kind, "title": title, "metadata": metadata}
                )
            )
            artifact = {
                "id": artifact_id,
                "sha256": ref["sha256"],
                "kind": kind,
                "title": title,
                "path": stored.relative_to(root).as_posix(),
                "source_path": ref["path"],
                "metadata": metadata,
            }
            artifacts["artifacts"] = [
                a for a in artifacts["artifacts"] if a["id"] != artifact_id
            ] + [artifact]
            ref.update(artifact=artifact_id, object_path=artifact["path"], verification="verified")
        item.update(
            status=status,
            ended=datetime.now(timezone.utc).isoformat(),
            outputs=refs,
            summary=summary or {},
        )
        atomic(folder / "artifacts.json", encoded(artifacts))
        atomic(root / REGISTRY, encoded(data))
    record(root, {"type": "experiment-finish", "experiment": item})
    return item


def _paths(root):
    paths = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in {".git", ".yapnr", ".ato", "node_modules", ".venv", "venv", "__pycache__"}
            and not (Path(base) / d).is_symlink()
        )
        for name in sorted(names):
            if name in ("result.json", "worker-result.json", "experiment.json"):
                paths.append(Path(base) / name)
                if len(paths) >= 1000:
                    return paths
    return paths


def attempts(project):
    """Project legacy reports into typed attempts; links require content evidence."""
    root = Path(project).resolve()
    runs = load(root)["attempts"]
    covered = {ref["path"] for run in runs for ref in run.get("outputs", [])}
    by_folder = {}
    for path in _paths(root):
        if path.relative_to(root).as_posix() in covered:
            continue
        value = read(path)
        if value is not None:
            by_folder.setdefault(path.parent, {})[path.name] = value
    for folder, files in sorted(by_folder.items()):
        result = files.get("result.json", {})
        meta = files.get("experiment.json", {})
        worker = files.get("worker-result.json", {})
        if "atopile" in result and "returncode" in result:
            kind, data, report = "schematic", result, folder / "result.json"
        elif meta.get("stage") == "EXPERIMENT" and isinstance(meta.get("input_sha256"), dict):
            kind, data, report = "pnr", worker, folder / "experiment.json"
        elif result.get("schema") == "yapnr-simulation-result-v1":
            kind, data, report = "simulation", result, folder / "result.json"
        else:
            continue
        identifier = folder.relative_to(root).as_posix()
        run = {
            "id": identifier,
            "title": identifier,
            "kind": kind,
            "origin": "imported report",
            "status": (
                "timed out"
                if data.get("timed_out")
                else (
                    "passed"
                    if data.get("returncode") == 0
                    else "failed" if "returncode" in data else "unknown"
                )
            ),
            "path": report.relative_to(root).as_posix(),
            "sha256": digest(report.read_bytes()),
            "seconds": data.get("seconds"),
            "started": meta.get("started"),
            "ended": data.get("ended"),
            "parameters": meta.get("budget", {}),
            "inputs": [],
            "outputs": [],
            "parent": None,
        }
        for name, sha in meta.get("input_sha256", {}).items():
            try:
                run["inputs"].append(reference(root, name, sha))
            except (OSError, ValueError):
                continue
        for name, local in result.get("outputs", {}).items():
            if isinstance(local, str):
                try:
                    run["outputs"].append(
                        reference(root, folder / local, result.get("sha256", {}).get(name))
                    )
                except (OSError, ValueError):
                    continue
        if kind == "pnr":
            for name in (
                "routed.kicad_pcb",
                "routed.svg",
                "routes.json",
                "native-legacy/routed.kicad_pcb",
                "native-legacy/routed.svg",
                "worker-result.json",
            ):
                if (folder / name).is_file():
                    run["outputs"].append(reference(root, folder / name))
        run["outputs"].append(reference(root, report))
        runs.append(run)
    for run in list(runs):
        if run["kind"] != "schematic":
            continue
        reports = [ref for ref in run.get("outputs", []) if ref["path"].endswith("/result.json")]
        result = (
            read(root / (reports[0].get("object_path") or reports[0]["path"])) if reports else None
        )
        if not result or "atopile" not in result:
            continue
        folder = root / reports[0]["path"]
        folder = folder.parent
        discovery = result.get("component_discovery")
        if isinstance(discovery, dict) and (folder / "discovery.json").is_file():
            refs = [reference(root, folder / "discovery.json", discovery.get("sha256"))]
            if (folder / "catalog.json").is_file():
                refs.append(reference(root, folder / "catalog.json", result.get("catalog_sha256")))
            runs.append(
                {
                    "id": run["id"] + "/discovery",
                    "title": "Component discovery",
                    "kind": "discovery",
                    "parent": run["id"],
                    "origin": "recorded stage evidence",
                    "status": "failed" if discovery.get("failures") else "passed",
                    "inputs": [],
                    "outputs": refs,
                    "parameters": discovery,
                    "path": (folder / "discovery.json").relative_to(root).as_posix(),
                }
            )
        if any(request.get("method") == "POST" for request in result.get("picker_requests", [])):
            inputs = (
                [reference(root, folder / "catalog.json", result.get("catalog_sha256"))]
                if (folder / "catalog.json").is_file()
                else []
            )
            outputs = [
                ref for ref in run["outputs"] if ref["path"].endswith((".bom.json", ".bom.csv"))
            ]
            runs.append(
                {
                    "id": run["id"] + "/parts",
                    "title": "Part picking",
                    "kind": "parts",
                    "parent": run["id"],
                    "origin": "recorded stage evidence",
                    "status": "passed" if outputs else "failed",
                    "inputs": inputs,
                    "outputs": outputs,
                    "parameters": {
                        "selected_parts": result.get("parts", []),
                        "catalog_candidates": result.get("catalog_parts"),
                    },
                }
            )
    # Native renderer selections are attached to matching recorded output files.
    # The experiment browser itself contains no embedded legacy UI.
    event_dir = root / "viewer-live/events"
    if event_dir.is_dir() and not event_dir.is_symlink():
        for event_path in sorted(event_dir.glob("*.json"))[:1000]:
            event = read(event_path, {})
            source, lane = event.get("source"), event.get("candidate")
            if not isinstance(source, str) or not isinstance(lane, str):
                continue
            try:
                ref = reference(root, source, event.get("board_sha256"))
            except (OSError, ValueError):
                continue
            if ref["verification"] not in ("observed", "verified"):
                continue
            for run in runs:
                if run["kind"] == "pnr" and any(
                    output["path"] == ref["path"] for output in run["outputs"]
                ):
                    run["lane"] = lane
    outputs = {}
    for run in runs:
        for artifact in run.get("outputs", []):
            if artifact.get("sha256") and artifact.get("verification") in ("observed", "verified"):
                outputs.setdefault(artifact["sha256"], []).append((run["id"], artifact["path"]))
    for run in runs:
        run["dependencies"] = [
            {
                "attempt": producer,
                "input": artifact["path"],
                "output": output,
                "sha256": artifact["sha256"],
                "basis": "matching content",
            }
            for artifact in run.get("inputs", [])
            if artifact.get("verification") in ("verified", "observed")
            for producer, output in outputs.get(artifact.get("sha256"), [])
            if producer not in (run["id"], run.get("parent"))
        ]
        run["provenance"] = "recorded inputs" if run.get("inputs") else "inputs not recorded"
    artifacts = read(
        root / ".yapnr/workspace/artifacts.json", {"artifacts": []}, limit=32 * 1024 * 1024
    )["artifacts"]
    by_hash = {artifact["sha256"]: artifact["id"] for artifact in artifacts}
    for run in runs:
        for ref in run.get("inputs", []) + run.get("outputs", []):
            if ref.get("sha256") in by_hash:
                ref.setdefault("artifact", by_hash[ref["sha256"]])
    return runs[:500]


def live_attempts(state):
    """Native telemetry is data for this browser, not another browser embedded in it."""
    result = []
    lanes = state.get("lanes", {})
    for identifier, lane in lanes.items():
        progress = lane.get("progress", {})
        ancestors = identifier.split("/")
        parent = next(
            (
                "route:" + "/".join(ancestors[:size])
                for size in range(len(ancestors) - 1, 0, -1)
                if "/".join(ancestors[:size]) in lanes
            ),
            None,
        )
        status = progress.get("state", "unknown")
        result.append(
            {
                "id": "route:" + identifier,
                "title": identifier,
                "kind": "discovery" if identifier.endswith("/search") else "pnr",
                "status": "finished" if status == "done" else status,
                "parent": parent,
                "lane": identifier,
                "inputs": [],
                "outputs": [],
                "dependencies": [],
                "provenance": "Inputs not recorded in this telemetry stream",
                "parameters": {"phase": lane.get("phase"), "progress": progress.get("fraction")},
                "origin": "native telemetry",
            }
        )
    return result[:500]


def register(commands):
    top = commands.add_parser(
        "experiment", help="record typed build, routing and simulation attempts"
    )
    subs = top.add_subparsers(dest="action", required=True)
    start = subs.add_parser("start")
    start.add_argument("--kind", choices=KINDS, required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--input", action="append", default=[])
    start.add_argument("--parent")
    start.add_argument("--parameters", help="workspace-relative JSON with seeds/tool pins/settings")
    end = subs.add_parser("finish")
    end.add_argument("id")
    end.add_argument(
        "--status", choices=("passed", "failed", "cancelled", "timed out"), required=True
    )
    end.add_argument("--output", action="append", default=[])
    subs.add_parser("list")
    for child in subs.choices.values():
        child.add_argument("--project", default=".")
        child.set_defaults(func=run_command)


def run_command(args):
    root = Path(args.project).resolve()
    try:
        if args.action == "start":
            params = {}
            if args.parameters:
                ref = reference(root, args.parameters)
                params = read(root / ref["path"])
                if params is None:
                    raise ValueError("Parameters must be a JSON object")
            result = begin(root, args.kind, args.title, args.input, params, args.parent)
        elif args.action == "finish":
            result = finish(root, args.id, args.status, args.output)
        else:
            result = {"attempts": attempts(root)}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError):
        print("Experiment record failed: invalid or unavailable workspace input")
        return 2
