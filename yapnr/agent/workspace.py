"""Portable workspace indexing, artifact publication and immutable event records."""

import fcntl
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from yapnr.events import record

MANIFEST = "workspace.json"
MAX_BYTES = 8 * 1024**3
MAX_FILES = 100000
KINDS = ("image", "diagram", "schematic", "board", "scene", "document", "report", "source")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class ArchiveReader:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()

    def read(self, size):
        block = self.handle.read(size)
        self.digest.update(block)
        return block


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def exclude_runtime_snapshots(root):
    """Keep journals portable without recursively embedding them in Git diffs."""
    root = Path(root).resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        return
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = root / path
    # The initialized article owns its repository; nested repositories use a scoped rule.
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    relative = root.relative_to(Path(top.stdout.strip()).resolve())
    rule = (
        "/" + ((relative.as_posix() + "/") if relative != Path(".") else "") + ".yapnr/workspace/"
    )
    content = path.read_text() if path.exists() else ""
    if rule not in content.splitlines():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(
            path,
            (
                content.rstrip() + "\n# Avoid recursive agent journal snapshots.\n" + rule + "\n"
            ).encode(),
        )


@contextmanager
def lock(root):
    folder = root / ".yapnr/workspace"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield folder


def relative_file(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Expected a workspace-relative file")
    root = Path(root).resolve()
    path = root / name
    if root not in path.resolve().parents or path.is_symlink() or not path.is_file():
        raise ValueError("File is missing or outside the workspace")
    return path


def blob(folder, data, suffix=""):
    identifier = sha(data)
    path = folder / "objects" / (identifier + suffix)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Content-addressed object is corrupt")
    else:
        atomic(path, data)
    return identifier, path.relative_to(folder.parent.parent).as_posix()


def read_json(path, default):
    return json.loads(path.read_bytes()) if path.is_file() else default


def publish(project, name, kind, title, metadata=None):
    root = Path(project).resolve()
    if kind not in KINDS or not title.strip():
        raise ValueError("Artifact needs a supported kind and title")
    data = relative_file(root, name).read_bytes()
    with lock(root) as folder:
        content_sha, stored = blob(folder, data, Path(name).suffix)
        identifier = sha(
            encoded(
                {"sha256": content_sha, "kind": kind, "title": title, "metadata": metadata or {}}
            )
        )
        registry = read_json(
            folder / "artifacts.json", {"schema": "yapnr-artifacts-v1", "artifacts": []}
        )
        item = {
            "id": identifier,
            "sha256": content_sha,
            "path": stored,
            "source_path": name,
            "kind": kind,
            "title": title,
            "metadata": metadata or {},
        }
        registry["artifacts"] = [a for a in registry["artifacts"] if a["id"] != identifier] + [item]
        atomic(folder / "artifacts.json", encoded(registry))
        return item


def scratchpad(project, text=None, revision=None, source="agent"):
    root = Path(project).resolve()
    with lock(root) as folder:
        path = root / "reports/design.md"
        previous = path.read_text() if path.is_file() else "# Design scratchpad\n"
        current = sha(previous.encode())
        if text is None:
            return {"text": previous, "revision": current}
        if not isinstance(text, str) or len(text.encode()) > 1024**2:
            raise ValueError("Scratchpad exceeds its size budget")
        if revision != current:
            raise ValueError("Scratchpad changed; reconcile the latest revision before saving")
        blob(folder, previous.encode(), ".md")
        blob(folder, text.encode(), ".md")
        atomic(path, text.encode())
    record(
        root,
        {
            "source": source + "-scratchpad",
            "previous_revision": current,
            "revision": sha(text.encode()),
            "text": text,
        },
    )
    return {"text": text, "revision": sha(text.encode())}


def scan(root):
    files, total = [], 0
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("Materialize workspace symlinks before exporting")
        if (
            not path.is_file()
            or name == MANIFEST
            or name in (".yapnr/workspace/lock", ".yapnr/workflow/lock")
        ):
            continue
        if name.startswith(".yapnr/workspace/write-"):
            raise ValueError("An unfinished workspace write prevents a consistent snapshot")
        size = path.stat().st_size
        total += size
        if total > MAX_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Workspace exceeds archive size or file-count budget")
        files.append(
            {
                "path": name,
                "sha256": file_sha(path),
                "bytes": size,
                "mode": path.stat().st_mode & 0o777,
            }
        )
    return files


def index(project):
    root = Path(project).resolve()
    with lock(root) as folder:
        if not (folder / "conversation.jsonl").exists():
            atomic(folder / "conversation.jsonl", b"")
        if not (folder / "recipes.json").exists():
            atomic(folder / "recipes.json", encoded({"schema": "yapnr-recipes-v1", "recipes": []}))
        files = scan(root)
        artifacts = read_json(folder / "artifacts.json", {"artifacts": []})["artifacts"]
        manifest = {
            "schema": "yapnr-workspace-v1",
            "files": files,
            "artifacts": artifacts,
            "conversation": ".yapnr/workspace/conversation.jsonl",
            "harness": (
                ".yapnr/workspace/harness.json" if (folder / "harness.json").is_file() else None
            ),
            "opencode_sessions": ".yapnr/workspace/opencode",
            "thread_state": (
                ".yapnr/workspace/thread-state.json"
                if (folder / "thread-state.json").is_file()
                else None
            ),
            "notes": "notes/notes.jsonl" if (root / "notes/notes.jsonl").is_file() else None,
            "focused_conversations": "notes/conversations",
            "note_threads": (
                ".yapnr/workspace/note-threads.json"
                if (folder / "note-threads.json").is_file()
                else None
            ),
            "workflow": (
                ".yapnr/workflow/state.json"
                if (root / ".yapnr/workflow/state.json").is_file()
                else None
            ),
            "artifact_reviews": (
                ".yapnr/workspace/reviews.json" if (folder / "reviews.json").is_file() else None
            ),
            "requirements_models": [
                p.relative_to(root).as_posix()
                for p in sorted((root / "requirements").rglob("*.y*ml"))
                if p.is_file()
            ],
            "requirements": {
                key: name if (root / name).is_file() else None
                for key, name in (
                    ("specification", "requirements/manifest.md"),
                    ("risks", "requirements/risks.md"),
                )
            },
            "reproduction": {
                "policy": "recorded-input-replay",
                "recipes": ".yapnr/workspace/recipes.json",
                "status": "unverified",
            },
            "excluded": [".yapnr/workspace/lock", ".yapnr/workflow/lock"],
        }
        atomic(root / MANIFEST, encoded(manifest))
        return manifest


def export(project, archive):
    root = Path(project).resolve()
    destination = Path(archive).resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Write the archive outside the workspace")
    if destination.exists():
        raise ValueError("Archive destination already exists")
    # Freeze the indexed bytes; reject concurrent writes instead of delivering a torn snapshot.
    manifest = index(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tar.gz")
    os.close(fd)
    try:
        with open(temporary, "wb") as raw, gzip.GzipFile(
            fileobj=raw, mode="wb", filename="", mtime=0
        ) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for item in [
                    {"path": MANIFEST, "sha256": sha(encoded(manifest)), "mode": 0o644}
                ] + manifest["files"]:
                    path = relative_file(root, item["path"])
                    if file_sha(path) != item["sha256"]:
                        raise ValueError("Workspace changed during export")
                    info = tarfile.TarInfo(item["path"])
                    info.size, info.mode, info.mtime = path.stat().st_size, item["mode"], 0
                    with path.open("rb") as content:
                        reader = ArchiveReader(content)
                        tar.addfile(info, reader)
                        if reader.digest.hexdigest() != item["sha256"]:
                            raise ValueError("Workspace changed during export")
        if scan(root) != manifest["files"]:
            raise ValueError("Workspace changed during export")
        os.replace(temporary, destination)
        return {"archive_sha256": file_sha(destination), "files": len(manifest["files"])}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_archive(archive, project):
    destination = Path(project).resolve()
    if destination.exists():
        raise ValueError("Import destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=destination.parent, prefix="workspace-import-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members, total = [], 0
            for member in tar:
                total += member.size
                if len(members) >= MAX_FILES + 1 or total > MAX_BYTES:
                    raise ValueError("Archive exceeds size or file-count budget")
                members.append(member)
            names = [m.name for m in members]
            if len(set(names)) != len(names) or MANIFEST not in names:
                raise ValueError("Archive has duplicate entries or no manifest")
            for member in members:
                path = Path(member.name)
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or path.as_posix() != member.name
                ):
                    raise ValueError("Archive contains unsafe paths or special files")
            if tar.getmember(MANIFEST).size > 64 * 1024**2:
                raise ValueError("Archive manifest exceeds its size budget")
            manifest = json.load(tar.extractfile(MANIFEST))
            if not isinstance(manifest, dict) or manifest.get("schema") != "yapnr-workspace-v1":
                raise ValueError("Unsupported workspace manifest")
            expected = {f["path"]: f for f in manifest["files"]}
            if len(expected) != len(manifest["files"]) or set(names) != set(expected) | {MANIFEST}:
                raise ValueError("Archive members do not match its manifest")
            for member in members:
                path = temporary / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                if member.name != MANIFEST and (
                    file_sha(path) != expected[member.name]["sha256"]
                    or path.stat().st_size != expected[member.name]["bytes"]
                ):
                    raise ValueError("Archive artifact hash or size mismatch")
                path.chmod(
                    (expected[member.name]["mode"] if member.name != MANIFEST else 0o644) & 0o777
                )
        os.rename(temporary, destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def run(args):
    try:
        if args.action == "manifest":
            result = index(args.project)
        elif args.action == "publish":
            metadata = (
                json.loads(relative_file(Path(args.project).resolve(), args.metadata).read_bytes())
                if args.metadata
                else {}
            )
            if not isinstance(metadata, dict):
                raise ValueError("Artifact metadata must be an object")
            result = publish(args.project, args.artifact, args.kind, args.title, metadata)
        elif args.action == "export":
            result = export(args.project, args.archive)
        elif args.action == "scratchpad":
            text = (
                relative_file(Path(args.project).resolve(), args.file).read_text()
                if args.file
                else None
            )
            result = scratchpad(args.project, text, args.revision)
        else:
            result = import_archive(args.archive, args.project)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(
            "Workspace operation failed: "
            + (
                str(error)
                if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError)
                else "invalid or unavailable workspace input"
            ),
            file=sys.stderr,
        )
        return 2


def register(commands):
    parser = commands.add_parser(
        "workspace", help="artifact manifest and portable workspace archives"
    )
    subs = parser.add_subparsers(dest="action", required=True)
    for action in ("manifest", "publish", "export", "import", "scratchpad"):
        child = subs.add_parser(action)
        child.add_argument("--project", default=".")
        if action == "publish":
            child.add_argument("--artifact", required=True)
            child.add_argument("--kind", choices=KINDS, required=True)
            child.add_argument("--title", required=True)
            child.add_argument(
                "--metadata",
                default="",
                help="workspace-relative metadata JSON, including scene seed or viewer URL",
            )
        if action in ("export", "import"):
            child.add_argument("--archive", required=True)
        if action == "scratchpad":
            child.add_argument(
                "--file", default="", help="replacement text in a workspace-relative file"
            )
            child.add_argument(
                "--revision", default="", help="current document hash required for edits"
            )
        child.set_defaults(func=run)
