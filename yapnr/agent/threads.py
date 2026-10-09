"""Shared focused conversations and canonical note attachments for a project."""

import json
import re
from urllib.parse import quote

from yapnr.agent import workspace
from yapnr.viewer.notes.store import NotesStore

SESSION = re.compile(r"ses_[A-Za-z0-9]+")
FOCUS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def notes(root):
    directory = root / "notes"
    if directory.is_symlink() or root.resolve() not in directory.resolve().parents:
        raise ValueError("Notes must belong to this workspace")
    for name in ("notes.jsonl", "notes.json", "design-notes.md", "notes.lock"):
        if (directory / name).is_symlink():
            raise ValueError("Notes files must belong to this workspace")
    return NotesStore(directory)


def focused(root, identifier=None):
    directory = root / "notes/conversations"
    if directory.is_symlink() or root.resolve() not in directory.resolve().parents:
        raise ValueError("Conversations must belong to this workspace")
    if identifier is not None and not FOCUS.fullmatch(identifier):
        raise ValueError("Invalid focused conversation")
    paths = (
        [directory / (identifier + ".jsonl")] if identifier else sorted(directory.glob("*.jsonl"))
    )
    out = []
    for path in paths:
        if not FOCUS.fullmatch(path.stem):
            continue
        path = workspace.relative_file(root, path.relative_to(root).as_posix())
        rows = []
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    break  # A running Ask turn may have an unfinished final append.
                row = json.loads(line)
                rows.append({k: v for k, v in row.items() if k not in ("owned", "cli_session")})
        if rows:
            out.append(
                {
                    "id": path.stem,
                    "kind": "focused",
                    "title": str(rows[0].get("message", "Focused chat"))[:100],
                    "updated": rows[-1].get("ts"),
                    "turns": rows,
                }
            )
    if identifier is not None:
        if not out:
            raise ValueError("Unknown focused conversation")
        return out[0]
    return [
        {k: v for k, v in item.items() if k != "turns"} | {"turn_count": len(item["turns"])}
        for item in out
    ]


def session_info(api, name, identifier, directory=None):
    if not isinstance(identifier, str) or not SESSION.fullmatch(identifier):
        raise ValueError("Invalid OpenCode session")
    directory = directory or "/projects/" + name
    suffix = "?directory=" + quote(directory, safe="")
    info = api("/session/" + identifier + suffix)
    if info.get("directory") != directory:
        raise ValueError("Conversation belongs to another workspace")
    return info, suffix


def transcript(root, api, name, kind, identifier, directory=None):
    if kind == "focused":
        return focused(root, identifier)
    if kind != "opencode":
        raise ValueError("Invalid conversation kind")
    info, suffix = session_info(api, name, identifier, directory)
    return {
        "id": identifier,
        "kind": kind,
        "title": info.get("title", "Chat"),
        "messages": api("/session/" + identifier + "/message" + suffix),
    }


def save_note(root, api, name, data, directory=None):
    if not isinstance(data.get("body"), str) or not data["body"].strip():
        raise ValueError("A conversation note needs a finding or refinement")
    origin = None
    if data.get("thread"):
        origin = transcript(
            root, api, name, data["thread"]["kind"], data["thread"]["id"], directory
        )
        with workspace.lock(root) as folder:
            digest, path = workspace.blob(folder, workspace.encoded(origin), ".json")
    fields = {
        "title": data["title"],
        "body": data["body"],
        "kind": "observation",
        "tags": ["conversation"],
        "targets": [],
    }
    if origin:
        fields["provenance"] = {"session": origin["id"], "view": "conversation"}
    item = notes(root).create(fields, {"kind": "user"})
    if origin:
        with workspace.lock(root) as folder:
            registry = workspace.read_json(folder / "note-threads.json", {})
            registry[item["id"]] = {
                "thread": {"id": origin["id"], "kind": origin["kind"]},
                "transcript": path,
                "sha256": digest,
            }
            workspace.atomic(folder / "note-threads.json", workspace.encoded(registry))
    workspace.record(
        root,
        {"source": "user-conversation-note", "note": item, "transcript": path if origin else None},
    )
    return item


def attach_note(root, api, name, data, directory=None):
    """Preserve exactly the reviewed note revision; noReply prevents an implicit paid turn."""
    _, suffix = session_info(api, name, data["session"], directory)
    store = notes(root)
    store.refresh()
    item = next((n for n in store.all() if n["id"] == data["note"]), None)
    if item is None or item.get("rev") != data["revision"]:
        raise ValueError("Note changed; review its current revision before attaching")
    links = workspace.read_json(root / ".yapnr/workspace/note-threads.json", {})
    origin = links.get(item["id"])
    if origin is None and FOCUS.fullmatch(str(item.get("provenance", {}).get("session", ""))):
        source = focused(root, item["provenance"]["session"])
        with workspace.lock(root) as folder:
            digest, path = workspace.blob(folder, workspace.encoded(source), ".json")
        origin = {
            "thread": {"id": source["id"], "kind": "focused"},
            "transcript": path,
            "sha256": digest,
        }
    if origin:
        path = workspace.relative_file(root, origin["transcript"])
        if workspace.file_sha(path) != origin["sha256"]:
            raise ValueError("Source transcript integrity failure")
    snapshot = {"schema": "yapnr-conversation-note-v1", "note": item, "source": origin}
    with workspace.lock(root) as folder:
        digest, path = workspace.blob(folder, workspace.encoded(snapshot), ".json")
    text = (
        f"Attached workspace note {item['id']} (revision {item['rev']}, status {item['status']}): {item['title']}\n\n"
        + item["body"]
        + f"\n\nImmutable note record: {path} (SHA-256 {digest}).\n"
        + (
            f"Source thread: {origin['thread']['id']}; full transcript: {origin['transcript']} "
            f"(SHA-256 {origin['sha256']}).\n"
            if origin
            else ""
        )
        + "Attaching this note does not approve requirements or change its status. "
        "Reconcile its findings with the current requirements and evidence before changing the design."
    )
    request = {"noReply": True, "parts": [{"type": "text", "text": text}]}
    messages = api("/session/" + data["session"] + "/message" + suffix)
    previous = next((m["info"] for m in reversed(messages) if m["info"].get("role") == "user"), {})
    for key in ("model", "agent"):
        if previous.get(key):
            request[key] = previous[key]
    if data.get("model"):
        request["model"] = data["model"]
    workspace.record(
        root,
        {
            "source": "user-note-attachment-request",
            "session": data["session"],
            "snapshot": path,
            "request": request,
        },
    )
    result = api("/session/" + data["session"] + "/message" + suffix, request)
    workspace.record(
        root,
        {
            "source": "user-note-attached",
            "session": data["session"],
            "snapshot": path,
            "response": result,
        },
    )
    return {"status": "attached", "note": item["id"], "snapshot": path}
