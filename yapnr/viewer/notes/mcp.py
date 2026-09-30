"""Minimal MCP stdio server: the Ask agent's design-notes tools (JSON-RPC 2.0, one message per line,
stdlib only).

The agent service starts it per turn (``python -m yapnr.viewer.notes.mcp``) through a strict
--mcp-config (server name yapnr_notes, so the CLI names the tools mcp__yapnr_notes__add_note, ...).
Every write goes through the notes store as actor {kind:'agent', session}; provenance comes from the
environment the agent service sets, never from model input: YAPNR_NOTES_DIR (required),
YAPNR_SESSION, YAPNR_TURN, YAPNR_LANE, YAPNR_PHASE, YAPNR_VIEWER_PORT, YAPNR_BOARD_SHA, and
YAPNR_RESOLVER (compact source index: targets must name existing refs/pads/nets/lines) or
YAPNR_GRAPH (graph.json fallback). Authority lives in the notes store (no accept/reject/apply, no
deletes; edits only of notes this conversation (YAPNR_SESSION) wrote, while open/proposed). Per
process (= per turn): at most 10 new notes and 40 comments/updates. Arguments of the wrong type come
back as tool errors the model can fix, not RPC errors. Methods: initialize (protocol version
negotiation: the client's version when supported, else the newest), notifications/*, ping,
tools/list, tools/call; batches (an empty one is an Invalid Request)."""

import json
import os
import re
import sys
from pathlib import Path

from yapnr.viewer.notes import store as notes_store
from yapnr.viewer.notes.store import ITEMS, KINDS, PROPOSALS, STATUSES, NoteNotFound

SERVER = "yapnr_notes"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LIMITS = dict(create=10, write=40)
MAX_LINE = 2 << 20
INSTRUCTIONS = (
    "Design notes for the board in this viewer. Record what the user and you conclude "
    "(observations, questions, requirements, decisions the user "
    "states, todos, proposals) so the design loop can act on it. You can only propose: "
    'a design change stays "proposed" until the user accepts or rejects '
    "it in the viewer Notes tab; you can never accept, reject, apply, resolve or delete "
    "notes, and must never say a note was accepted or applied."
)
ITEM = {
    "type": "object",
    "description": "Board item, same schema as the viewer context: component {kind,ref}, net {kind,name}, "
    "pad {kind,ref,pad}, "
    "source {kind,file,line,end} (atopile path relative to src, e.g. power.ato), region "
    "{kind,lane,bbox,refs,nets}, group {kind,id,label,refs,nets}, lane {kind,lane}.",
    "properties": {
        "kind": {"type": "string", "enum": list(ITEMS)},
        "ref": {"type": "string"},
        "name": {"type": "string"},
        "pad": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer", "minimum": 1},
        "end": {"type": "integer", "minimum": 1},
        "lane": {"type": "string"},
        "id": {"type": "string"},
        "label": {"type": "string"},
        "refs": {"type": "array", "items": {"type": "string"}},
        "nets": {"type": "array", "items": {"type": "string"}},
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
    },
    "required": ["kind"],
}
PROPOSAL = {
    "type": "object",
    "description": "A proposed design change for the user to decide on.",
    "properties": {
        "type": {"type": "string", "enum": list(PROPOSALS)},
        "summary": {
            "type": "string",
            "description": "What to change and why, one paragraph (<= 2000 characters; details go in body or diff).",
        },
        "diff": {
            "type": "string",
            "description": "Optional unified diff or exact replacement text.",
        },
    },
    "required": ["type", "summary"],
}
SOURCES = {
    "type": "array",
    "description": "Web pages the note relies on (required for anything taken from the web).",
    "items": {
        "type": "object",
        "properties": {"url": {"type": "string"}, "title": {"type": "string"}},
        "required": ["url"],
    },
}
FIELDS = {
    "title": {"type": "string", "description": "One line, <= 120 characters."},
    "body": {
        "type": "string",
        "description": "Markdown, <= 8000 characters; use [[ref:C17]] / [[net:hv]] / [[src:file.ato:12]] chips.",
    },
    "kind": {"type": "string", "enum": list(KINDS)},
    "targets": {
        "type": "array",
        "items": ITEM,
        "description": "Every board item the note concerns (exact refs / netlist net names / atopile lines).",
    },
    "tags": {"type": "array", "items": {"type": "string"}},
    "proposal": PROPOSAL,
    "sources": SOURCES,
}
ID = {"type": "string", "pattern": "^N-[0-9]{4,6}$", "description": "Note id, e.g. N-0007"}
TOOLS = [
    dict(
        name="add_note",
        description="Record a design note from this conversation (observation, question, requirement, "
        "decision the user stated, todo, or proposal). "
        "Give targets for every board item it concerns and sources for anything from the "
        "web. A design change (atopile source, PnR annotation, constraint, engine) "
        'must be kind "proposal" with a proposal object; it stays "proposed" until the user decides in the Notes tab.',
        inputSchema={"type": "object", "properties": FIELDS, "required": ["title"]},
    ),
    dict(
        name="list_notes",
        description=(
            "Search existing design notes (newest first): free text, a component ref or net name "
            "among the targets, status, kind, author."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ref": {"type": "string"},
                "net": {"type": "string"},
                "status": {"type": "string", "enum": list(STATUSES)},
                "kind": {"type": "string", "enum": list(KINDS)},
                "author": {"type": "string", "enum": ["user", "agent"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    ),
    dict(
        name="get_note",
        description="One note in full: body, targets, proposal, sources, comments, status history fields.",
        inputSchema={"type": "object", "properties": {"id": ID}, "required": ["id"]},
    ),
    dict(
        name="comment_note",
        description=(
            "Add a comment to any note (new evidence, an answer to a question, a caveat). Use "
            "this for notes you may not edit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": ID,
                "text": {"type": "string", "description": "<= 4000 characters"},
            },
            "required": ["id", "text"],
        },
    ),
    dict(
        name="update_note",
        description="Edit a note written in this conversation while it is still open or proposed (title, "
        "body, kind, tags, targets, proposal, sources). "
        "Status cannot be changed here; notes by the user, from other conversations or already "
        "decided cannot be edited (comment instead).",
        inputSchema={"type": "object", "properties": {"id": ID, **FIELDS}, "required": ["id"]},
    ),
]
NAMES = {t["name"] for t in TOOLS}
ARG_TYPES = dict(
    query=str,
    ref=str,
    net=str,
    status=str,
    kind=str,
    author=str,
    id=str,
    text=str,
    title=str,
    body=str,
    tags=list,
    targets=list,
    sources=list,
    proposal=dict,
)
TYPE_NAMES = {str: "a string", list: "an array", dict: "an object"}


def env_context(env=None):
    """(actor, provenance, notes dir, resolver source) from the per-turn environment."""
    e = os.environ if env is None else env
    d = e.get("YAPNR_NOTES_DIR")
    if not d:
        raise SystemExit("YAPNR_NOTES_DIR is not set")
    sess = e.get("YAPNR_SESSION") or None
    turn = e.get("YAPNR_TURN")
    port = e.get("YAPNR_VIEWER_PORT")
    prov = dict(
        session=sess,
        turn=int(turn) if turn and turn.isdigit() else None,
        lane=e.get("YAPNR_LANE") or None,
        phase=e.get("YAPNR_PHASE") or None,
        board_sha=(e.get("YAPNR_BOARD_SHA") or "").lower() or None,
        viewer_port=int(port) if port and port.isdigit() else None,
    )
    if prov["board_sha"] and not re.fullmatch(r"[0-9a-f]{8,64}", prov["board_sha"]):
        prov["board_sha"] = None
    prov = {k: v for k, v in prov.items() if v is not None}
    actor = dict(
        kind="agent",
        **({"session": sess} if sess else {}),
        **({"turn": prov["turn"]} if "turn" in prov else {}),
    )
    res = next(
        (p for p in (e.get("YAPNR_RESOLVER"), e.get("YAPNR_GRAPH")) if p and Path(p).is_file()),
        None,
    )
    return actor, prov, d, res


def brief(n, body=240):
    t = [notes_store.describe(x) for x in n.get("targets") or []]
    return dict(
        id=n["id"],
        kind=n["kind"],
        status=n["status"],
        author=n["author"],
        title=n["title"],
        updated=n.get("updated"),
        targets=t[:12] + ([f"+{len(t)-12} more"] if len(t) > 12 else []),
        **(
            {"proposal": n["proposal"].get("type") + ": " + n["proposal"].get("summary", "")[:200]}
            if n.get("proposal")
            else {}
        ),
        body=(n.get("body") or "")[:body] + ("…" if len(n.get("body") or "") > body else ""),
        comments=len(n.get("comments") or []),
    )


class Server:
    def __init__(self, env=None, store=None):
        self.actor, self.prov, d, res = env_context(env)
        self.counts = dict(create=0, write=0)
        try:
            resolver = notes_store.resolver_for(res) if res else None
        except (OSError, ValueError, AttributeError, TypeError):
            resolver = None
        self.store = store or notes_store.NotesStore(d, resolver=resolver)

    def call(self, name, a):
        """-> (text, is_error). Tool errors go back to the model as text it can act on."""
        if not isinstance(a, dict):
            return "arguments must be an object", True
        bad = [
            f"{k} must be {TYPE_NAMES[ARG_TYPES[k]]}"
            for k, v in a.items()
            if k in ARG_TYPES
            and v is not None
            and not isinstance(v, ARG_TYPES[k])
            and not (k == "proposal" and name == "update_note" and v is None)
        ]
        if "limit" in a and a["limit"] is not None and type(a["limit"]) is not int:
            bad.append("limit must be an integer")
        if bad:
            return "invalid: " + "; ".join(bad), True
        s = self.store

        def pick(*ks):
            return {k: a[k] for k in ks if k in a}

        if name in ("add_note", "update_note") and a.get("status") not in (
            None,
            "open",
            "proposed",
        ):
            return (
                (
                    f"not allowed: only the user can mark a note {a['status']} (viewer Notes tab); you "
                    "can only record notes and proposals"
                ),
                True,
            )
        if name == "update_note" and "status" in a:
            return (
                'not allowed: the assistant cannot change a note status; attach a proposal to make it "proposed"',
                True,
            )
        try:
            if name == "add_note":
                if self.counts["create"] >= LIMITS["create"]:
                    return f"limit reached: at most {LIMITS['create']} new notes per answer", True
                n = s.create(pick(*FIELDS), self.actor, provenance=self.prov)
                self.counts["create"] += 1
                return (
                    json.dumps(
                        dict(
                            id=n["id"],
                            status=n["status"],
                            kind=n["kind"],
                            title=n["title"],
                            targets=len(n["targets"]),
                            note=(
                                "Recorded. The user sees it in the Notes tab; only the user can accept, reject or "
                                "apply it."
                            ),
                        ),
                        ensure_ascii=False,
                    ),
                    False,
                )
            if name == "list_notes":
                lim = a.get("limit", 20)
                lim = lim if type(lim) is int and 1 <= lim <= 100 else 20
                got = s.find(
                    query=a.get("query"),
                    ref=a.get("ref"),
                    net=a.get("net"),
                    status=a.get("status"),
                    kind=a.get("kind"),
                    author=a.get("author"),
                    limit=lim,
                )
                return (
                    json.dumps(
                        dict(rev=s.rev, count=len(got), notes=[brief(n) for n in got]),
                        ensure_ascii=False,
                    ),
                    False,
                )
            if name == "get_note":
                return json.dumps(s.get(str(a.get("id"))), ensure_ascii=False), False
            if name in ("comment_note", "update_note"):
                if self.counts["write"] >= LIMITS["write"]:
                    return (
                        f"limit reached: at most {LIMITS['write']} comments/updates per answer",
                        True,
                    )
                nid = str(a.get("id"))
                n = (
                    s.comment(nid, a.get("text"), self.actor)
                    if name == "comment_note"
                    else s.update(nid, pick(*FIELDS), self.actor)
                )
                self.counts["write"] += 1
                return (
                    json.dumps(
                        dict(
                            id=n["id"],
                            status=n["status"],
                            title=n["title"],
                            comments=len(n.get("comments") or []),
                            updated=n.get("updated"),
                        ),
                        ensure_ascii=False,
                    ),
                    False,
                )
            return f"unknown tool {name}", True
        except NoteNotFound as ex:
            return str(ex), True
        except PermissionError as ex:
            return "not allowed: " + str(ex), True
        except (ValueError, TypeError, AttributeError, KeyError) as ex:
            return "invalid: " + str(ex), True

    def handle(self, m):
        """One JSON-RPC message -> response dict or None (notifications, client responses)."""
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
            return dict(
                jsonrpc="2.0",
                id=m.get("id") if isinstance(m, dict) else None,
                error=dict(code=-32600, message="invalid request"),
            )
        method, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if method is None:
            return None  # a response to something we never send
        if "id" not in m:
            return None  # notifications/initialized, notifications/cancelled, ...

        def ok(r):
            return dict(jsonrpc="2.0", id=mid, result=r)

        if method == "initialize":
            v = p.get("protocolVersion") if isinstance(p, dict) else None
            return ok(
                dict(
                    protocolVersion=v if v in VERSIONS else VERSIONS[0],
                    capabilities=dict(tools=dict(listChanged=False)),
                    serverInfo=dict(name="yapnr-notes", version="1.0.0"),
                    instructions=INSTRUCTIONS,
                )
            )
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok(dict(tools=TOOLS))
        if method == "tools/call":
            name = p.get("name") if isinstance(p, dict) else None
            if name not in NAMES:
                return dict(
                    jsonrpc="2.0", id=mid, error=dict(code=-32602, message=f"unknown tool {name}")
                )
            t, err = self.call(name, p.get("arguments") or {})
            return ok(dict(content=[dict(type="text", text=t)], isError=err))
        return dict(
            jsonrpc="2.0", id=mid, error=dict(code=-32601, message=f"method not found: {method}")
        )

    def serve(self, rd=None, wr=None):
        rd = rd or sys.stdin.buffer
        wr = wr or sys.stdout
        for raw in rd:
            if len(raw) > MAX_LINE:
                out = dict(
                    jsonrpc="2.0", id=None, error=dict(code=-32600, message="message too large")
                )
            elif not raw.strip():
                continue
            else:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    out = dict(
                        jsonrpc="2.0", id=None, error=dict(code=-32700, message="parse error")
                    )
                else:
                    try:
                        if msg == []:
                            out = dict(
                                jsonrpc="2.0",
                                id=None,
                                error=dict(code=-32600, message="invalid request: empty batch"),
                            )
                        else:
                            out = (
                                [r for r in (self.handle(x) for x in msg) if r] or None
                                if isinstance(msg, list)
                                else self.handle(msg)
                            )
                    except Exception as ex:
                        out = dict(
                            jsonrpc="2.0",
                            id=msg.get("id") if isinstance(msg, dict) else None,
                            error=dict(code=-32603, message=f"{type(ex).__name__}: {ex}"[:300]),
                        )
            if out is not None:
                wr.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
                wr.flush()


if __name__ == "__main__":
    try:
        Server().serve()
    except KeyboardInterrupt:
        pass
