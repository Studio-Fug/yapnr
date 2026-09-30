"""Design notes shared by the viewer servers, the Ask agent's MCP notes server (notes_mcp.py) and the design loop.

<dir>/notes.jsonl      append-only event log, the source of truth: one line per change
                       {rev, op:create|update|comment|delete, id, ts, actor:{kind:user|agent, session?, remote?}, fields}
<dir>/notes.json       materialized {schema, rev, updated, notes:[...]} (rewritten atomically after every write)
<dir>/design-notes.md  export for the design loop: Accepted (to apply) / Proposed / Open / Applied / Rejected / Resolved
Writers hold an fcntl lock on <dir>/notes.lock, catch up with lines other processes appended, drop a torn last line left by a
crash (it was never acknowledged), append one line (one write + fsync) and rewrite the derived files (tmp + fsync + rename).
Readers consume complete lines only, so two viewers and any number of per-turn MCP servers can share one directory.

Authority (enforced here, whatever the client): an agent creates notes (status open, or proposed when it is a proposal),
comments on any note and edits title/body/kind/tags/targets/proposal/sources/related links of notes written in its own
conversation (same session) while they are open or proposed. Only a user sets accepted/rejected/applied/resolved (or reopens),
deletes, or edits other notes; over HTTP, accepting or applying needs the rev the user saw (expect_rev), so an edit made
meanwhile is never accepted unseen. Every update records updated_by {kind, session?}.
Text is normalized on the way in (every Unicode line break becomes \n or, in one-line fields, a space; C1 controls and bidi
overrides are dropped) and design-notes.md renders one-line fields flat and bodies as quotes, so no note can forge a heading.
Targets use the Ask context item schema; with a resolver (SourceService, a source index or graph.json) an agent's refs, pads,
nets and source lines must exist.

CLI for the engineering loop: python notes_store.py report [--status accepted] [--kind proposal] [--json] [--dir DIR]
(an empty or not yet used store prints empty groups, or [] with --json, and exits 0).
"""

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HIER = HERE.parent
DEFAULT_DIR = HIER / "notes"
DEFAULT_GRAPH = HIER / "inputs10b/graph.json"
SCHEMA = "splanc-notes-v1"
KINDS = ("observation", "question", "requirement", "decision", "todo", "proposal")
STATUSES = ("open", "proposed", "accepted", "rejected", "applied", "resolved")
USER_ONLY = ("accepted", "rejected", "applied", "resolved")
PROPOSALS = ("ato", "pnr-annotation", "constraint", "engine", "other")
LINKS = ("applied_in", "related")
ITEMS = ("component", "net", "pad", "region", "source", "group", "lane", "event", "probe")
AGENT_FIELDS = ("title", "body", "kind", "tags", "targets", "proposal", "sources", "links")
USER_FIELDS = AGENT_FIELDS + ("status", "provenance")
MAX = dict(
    title=120,
    body=8000,
    targets=40,
    tags=20,
    tag=40,
    sources=20,
    url=2000,
    label=200,
    comment=4000,
    comments=1000,
    agent_comments=200,
    links=40,
    ref=300,
    summary=2000,
    diff=20000,
    names=400,
    notes=5000,
)
OPS = ("create", "update", "comment", "delete")
GROUPS = (
    ("accepted", "Accepted (to apply)"),
    ("proposed", "Proposed (awaiting decision)"),
    ("open", "Open questions / observations"),
    ("applied", "Applied"),
    ("rejected", "Rejected"),
    ("resolved", "Resolved"),
)
ID = re.compile(r"N-\d{4,6}")
NAME = re.compile(
    "[^\\x00-\\x1f\\x7f-\\x9f\\u061c\\u200e\\u200f\\u2028-\\u202e\\u2066-\\u2069\\[\\]]{1,200}"
)
FILE = re.compile(r"[A-Za-z0-9_./-]{1,300}\.ato")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:/+#-]*")


class NoteNotFound(LookupError):
    pass


class NoteConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def copy(n):
    return json.loads(json.dumps(n))


def num(i):
    return int(i[2:]) if isinstance(i, str) and ID.fullmatch(i) else 0


# every line boundary str.splitlines() knows (a reader splitting design-notes.md on these must not see a forged line), and
# characters that hide or reorder text: C0/C1 controls, bidi embeddings/overrides/isolates, marks
BREAKS = re.compile("\r\n|[\r\n\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
HIDDEN = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def text(v, n, what, oneline=False, empty=False):
    if not isinstance(v, str):
        raise ValueError(f"{what} must be a string")
    v = HIDDEN.sub("", BREAKS.sub("\n", v))
    if oneline:
        v = re.sub(r"[\n\t]+", " ", v).strip()
    if not empty and not v.strip():
        raise ValueError(f"{what} is empty")
    if len(v) > n:
        raise ValueError(f"{what} is longer than {n} characters")
    return v


# ------------------------------------------------------------------ targets
def item(it, resolver=None):
    """One target in the Ask context item schema, normalized to its known fields. With a resolver the refs, pads, nets and
    source lines must exist (agent writes: a hallucinated C999 is refused with a message the model can act on).
    """
    if not isinstance(it, dict) or it.get("kind") not in ITEMS:
        raise ValueError("target kind must be one of " + ", ".join(ITEMS))
    k = it["kind"]
    out = dict(kind=k)

    def name(f, req=True):
        v = it.get(f)
        if v is None and not req:
            return
        if f == "pad" and type(v) is int:
            v = str(v)
        if not (isinstance(v, str) and NAME.fullmatch(v)):
            raise ValueError(f"{k} target needs a valid {f}")
        out[f] = v

    def names(f):
        v = it.get(f)
        if v is None:
            return
        if not (
            isinstance(v, list)
            and len(v) <= MAX["names"]
            and all(isinstance(x, str) and NAME.fullmatch(x) for x in v)
        ):
            raise ValueError(f'{k} target: {f} must be a list of at most {MAX["names"]} names')
        out[f] = list(dict.fromkeys(v))

    def label(f):
        v = it.get(f)
        if v is None:
            return
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError(f"{k} target: invalid {f}")
        out[f] = (
            text(str(v), MAX["label"], f"{k} {f}", oneline=True).replace("[", "(").replace("]", ")")
        )

    if k == "component":
        name("ref")
    elif k == "net":
        name("name")
    elif k == "pad":
        name("ref")
        name("pad")
    elif k == "source":
        f, a, b = it.get("file"), it.get("line"), it.get("end")
        if not (
            isinstance(f, str)
            and FILE.fullmatch(f)
            and not f.startswith("/")
            and ".." not in f.split("/")
        ):
            raise ValueError("source target needs a .ato path relative to the atopile src folder")
        if type(a) is not int or a < 1 or (b is not None and (type(b) is not int or b < a)):
            raise ValueError("source target needs line >= 1 (and end >= line)")
        out.update(file=f, line=a)
        b is not None and b != a and out.update(end=b)
    elif k in ("region", "group"):
        label("lane")
        label("id")
        label("label")
        names("refs")
        names("nets")
        if k == "region" and it.get("bbox") is not None:
            b = it["bbox"]
            if not (
                isinstance(b, list)
                and len(b) == 4
                and all(type(x) in (int, float) and math.isfinite(x) and abs(x) < 1e5 for x in b)
            ):
                raise ValueError("region bbox must be [x0, y0, x1, y1] in mm")
            out["bbox"] = [round(float(x), 4) for x in b]
    elif k == "lane":
        name("lane")
    elif k in ("event", "probe"):
        name("id" if k == "event" else "label")
        name("lane", False)
        if k == "event":
            name("event_kind", False)
        if it.get("summary") is not None:
            out["summary"] = text(it["summary"], 4000, f"{k} summary", empty=True)
    if resolver is not None:
        check(out, resolver)
    return out


def check(it, r):
    k, bad = it["kind"], []
    if k in ("component", "pad"):
        c = r.component(it["ref"])
        if c is None:
            bad.append("component " + it["ref"])
        elif (
            k == "pad"
            and isinstance(c.get("pins"), dict)
            and c["pins"]
            and it["pad"] not in c["pins"]
        ):
            bad.append(f"pad {it['ref']}.{it['pad']}")
    elif k == "net" and r.net(it["name"]) is None:
        bad.append("net " + it["name"])
    elif k == "source":
        n = r.lines(it["file"])
        if n is None:
            bad.append("source file " + it["file"])
        elif max(it["line"], it.get("end") or 0) > n:
            bad.append(
                f"line {it['file']}:{max(it['line'],it.get('end') or 0)} (file has {n} lines)"
            )
    elif k in ("region", "group"):
        bad += ["component " + x for x in it.get("refs", []) if r.component(x) is None] + [
            "net " + x for x in it.get("nets", []) if r.net(x) is None
        ]
    if bad:
        raise ValueError(
            "unknown "
            + ", ".join(bad[:6])
            + (f" (+{len(bad)-6} more)" if len(bad) > 6 else "")
            + ": use exact component refs, pad numbers, netlist net names and atopile file/line numbers"
        )


class IndexResolver:
    """component(ref) / net(name) / lines(file) over a source index {components, nets, files} (the /api/source/index shape or
    compact() of it) or a callable returning one (SourceService.index). lines() is inf when the index has no file table.
    """

    def __init__(self, index):
        self._ix = index
        self._files = None

    def ix(self):
        return (self._ix() if callable(self._ix) else self._ix) or {}

    def component(self, ref):
        v = (self.ix().get("components") or {}).get(ref)
        return v if isinstance(v, dict) else None

    def net(self, name):
        v = (self.ix().get("nets") or {}).get(name)
        return v if isinstance(v, dict) else None

    def lines(self, f):
        ix = self.ix()
        c = self._files
        if c is None or c[0] is not ix:
            fl = ix.get("files")
            c = self._files = (
                ix,
                (
                    {x["path"]: x.get("lines") for x in fl if isinstance(x, dict) and x.get("path")}
                    if isinstance(fl, list)
                    else fl if isinstance(fl, dict) else None
                ),
            )
        if c[1] is None:
            return math.inf
        v = c[1].get(f)
        return None if v is None else v if type(v) is int else math.inf


def compact(ix):
    """The resolver facts of a source index (a few hundred kB less): what agent_service hands the MCP server per index sha."""
    comps = {}
    for r, c in (ix.get("components") or {}).items():
        if isinstance(c, dict):
            comps[r] = dict(
                type=c.get("type"),
                instance=c.get("instance") or c.get("address"),
                file=c.get("file"),
                line=c.get("line"),
                pins={
                    str(p): dict(pin=v.get("pin"), net=v.get("net"))
                    for p, v in (c.get("pins") or {}).items()
                    if isinstance(v, dict)
                },
            )
    nets = {
        n: dict(title=v.get("title"), kind=v.get("kind"))
        for n, v in (ix.get("nets") or {}).items()
        if isinstance(v, dict)
    }
    fl = ix.get("files")
    files = (
        {x["path"]: x.get("lines") for x in fl if isinstance(x, dict) and x.get("path")}
        if isinstance(fl, list)
        else fl
    )
    return dict(
        schema="splanc-notes-resolver-v1",
        sha=ix.get("sha"),
        components=comps,
        nets=nets,
        **({"files": files} if files is not None else {}),
    )


def graph_index(path):
    """Resolver index from the netlist graph.json alone (no types, titles or source lines)."""
    g = json.loads(Path(path).read_text())
    comps = {}
    for c in g.get("components") or []:
        if isinstance(c, dict) and c.get("ref"):
            comps[c["ref"]] = dict(
                type=None,
                instance=str(c.get("address") or "").removesuffix("._p") or None,
                pins={
                    str(p.get("name")): dict(net=p.get("net"))
                    for p in c.get("pads") or []
                    if isinstance(p, dict)
                },
            )
    return dict(
        components=comps,
        nets={
            n["name"]: dict(title=None)
            for n in g.get("nets") or []
            if isinstance(n, dict) and n.get("name")
        },
    )


def resolver_for(obj):
    """None | resolver | SourceService-like (index()) | index dict | path to an index/compact/graph JSON -> resolver or None."""
    if obj is None or all(hasattr(obj, m) for m in ("component", "net", "lines")):
        return obj
    if isinstance(obj, dict):
        return IndexResolver(obj)
    if not isinstance(obj, (str, os.PathLike)):
        return IndexResolver(obj.index)  # SourceService-like
    p = Path(obj)
    d = json.loads(p.read_text())
    return IndexResolver(graph_index(p) if isinstance(d.get("components"), list) else d)


def describe(it, r=None):
    """One-line human description of a target (design-notes.md, report)."""
    k = it.get("kind")
    g = lambda f, *a: (getattr(r, f)(*a) if r is not None else None)

    def comp(ref):
        c = g("component", ref)
        if not c:
            return ref
        where = f" ({c['file']}:{c['line']})" if c.get("file") and c.get("line") else ""
        return f"{ref}: {' '.join(str(x) for x in (c.get('type'),c.get('instance')) if x) or '?'}{where}"

    def net(name):
        n = g("net", name)
        t = (n or {}).get("title")
        return f"net {name}" + (f": {t}" if t and t != name else "")

    if k == "component":
        return comp(it["ref"])
    if k == "net":
        return net(it["name"])
    if k == "pad":
        c = g("component", it["ref"]) or {}
        p = (c.get("pins") or {}).get(it["pad"]) or {}
        return (
            f"pad {it['ref']}.{it['pad']}"
            + (f" ({p['pin']})" if p.get("pin") and p["pin"] != it["pad"] else "")
            + (f" -> net {p['net']}" if p.get("net") else "")
        )
    if k == "source":
        return f"source {it['file']}:{it['line']}" + (f"-{it['end']}" if it.get("end") else "")
    if k in ("region", "group"):
        refs, nets = it.get("refs") or [], it.get("nets") or []
        head = (
            " ".join(
                x
                for x in (
                    "region",
                    it.get("lane"),
                    f"bbox {it['bbox']} mm" if it.get("bbox") else None,
                )
                if x
            )
            if k == "region"
            else f"group {it.get('label') or it.get('id') or ''}".rstrip()
        )
        return (
            head
            + (
                f"; parts {', '.join(refs[:30])}"
                + (f" (+{len(refs)-30})" if len(refs) > 30 else "")
                if refs
                else ""
            )
            + (
                f"; nets {', '.join(nets[:20])}" + (f" (+{len(nets)-20})" if len(nets) > 20 else "")
                if nets
                else ""
            )
        )
    if k == "lane":
        return f"lane {it['lane']}"
    if k == "event":
        return (
            f"event {it['id']}"
            + (f" ({it['event_kind']})" if it.get("event_kind") else "")
            + (f" lane {it['lane']}" if it.get("lane") else "")
        )
    if k == "probe":
        return f"probe {it['label']}" + (f" lane {it['lane']}" if it.get("lane") else "")
    return str(k)


def short_target(it):
    """Compact label: C17, net hv, U5.13, system_5v.ato:112-118, region (12 parts), group Hot loop, lane x."""
    k = it.get("kind")
    if k == "component":
        return str(it.get("ref"))
    if k == "net":
        return f"net {it.get('name')}"
    if k == "pad":
        return f"{it.get('ref')}.{it.get('pad')}"
    if k == "source":
        return f"{it.get('file')}:{it.get('line')}" + (f"-{it['end']}" if it.get("end") else "")
    if k == "region":
        return f"region ({len(it.get('refs') or [])} parts)"
    if k == "group":
        return f"group {it.get('label') or it.get('id') or ''}".rstrip()
    if k == "lane":
        return f"lane {it.get('lane')}"
    return f"{k} {it.get('id') or it.get('label') or ''}".rstrip()


def target_keys(items, r=None):
    """(refs, nets, source ranges, lanes) an item list touches; a pad also touches its part and (with a resolver) its net."""
    refs, nets, src, lanes = set(), set(), [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        k = it.get("kind")
        if k == "component":
            refs.add(it.get("ref"))
        elif k == "net":
            nets.add(it.get("name"))
        elif k == "pad":
            refs.add(it.get("ref"))
            c = r.component(it.get("ref")) if r is not None else None
            n = (
                ((c or {}).get("pins") or {}).get(str(it.get("pad")))
                if isinstance(c, dict)
                else None
            )
            if isinstance(n, dict) and n.get("net"):
                nets.add(n["net"])
        elif k in ("region", "group"):
            refs |= set(it.get("refs") or [])
            nets |= set(it.get("nets") or [])
        elif k == "source" and type(it.get("line")) is int:
            src.append((it.get("file"), it["line"], it.get("end") or it["line"]))
        elif k == "lane":
            lanes.add(it.get("lane"))
    return refs, nets, src, lanes


# ------------------------------------------------------------------ store
def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def bad_event(e):
    """Why a notes.jsonl line cannot be applied (None: fine). The log is ours, but a hand edit or another tool must not take the
    store (and with it both viewers and every agent turn) down."""
    if not isinstance(e, dict):
        return "not an object"
    op, i, f = e.get("op"), e.get("id"), e.get("fields")
    if op not in OPS:
        return f"unknown op {op!r}"
    if not (isinstance(i, str) and ID.fullmatch(i)):
        return f"invalid note id {i!r}"
    if op == "delete":
        return None
    if not isinstance(f, dict):
        return "fields must be an object"
    lists = ("targets", "tags", "sources", "comments", "links")
    if op == "create":
        if not (
            isinstance(f.get("title"), str)
            and f.get("status") in STATUSES
            and f.get("kind") in KINDS
            and f.get("author") in ("user", "agent")
        ):
            return "a create needs title, status, kind and author"
        if any(not isinstance(f.get(k, []), list) for k in lists):
            return "targets, tags, sources, comments and links must be lists"
        if not isinstance(f.get("provenance", {}), dict) or not isinstance(f.get("body", ""), str):
            return "invalid provenance or body"
    elif op == "update":
        if any(k in f for k in ("id", "author", "created", "rev", "comments")):
            return "an update cannot change id, author, created, rev or comments"
        if any(k in f and f[k] is None for k in ("title", "status", "kind", "body")):
            return "title, status, kind and body cannot be removed"
        if "status" in f and f["status"] not in STATUSES or "kind" in f and f["kind"] not in KINDS:
            return "invalid status or kind"
        if (
            "title" in f
            and not isinstance(f["title"], str)
            or "body" in f
            and not isinstance(f["body"], str)
        ):
            return "title and body must be strings"
        if any(f.get(k) is not None and not isinstance(f[k], list) for k in lists):
            return "targets, tags, sources and links must be lists"
        if (
            f.get("proposal") is not None
            and not isinstance(f["proposal"], dict)
            or f.get("provenance") is not None
            and not isinstance(f["provenance"], dict)
        ):
            return "invalid proposal or provenance"
    elif not isinstance(f.get("text"), str):
        return "a comment needs text"
    return None


class NotesStore:
    def __init__(self, directory=None, resolver=None, export=True):
        self.dir = Path(directory or DEFAULT_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "notes.jsonl"
        self.json_path = self.dir / "notes.json"
        self.md_path = self.dir / "design-notes.md"
        self.lock_path = self.dir / "notes.lock"
        self.resolver = resolver_for(resolver)
        self.export = export
        self.tlock = threading.RLock()
        self.skipped = []
        self._reset()
        if (
            export and self.log.exists()
        ):  # a writer died between its log line and the derived files: bring them up to date
            try:
                ok = json.loads(self.json_path.read_text()).get("rev") == self.refresh()
            except (OSError, ValueError, AttributeError):
                ok = False
            if not ok:
                with self._locked():
                    self._derive()

    def _reset(self):
        (
            self.notes,
            self.deleted,
            self.touched,
            self.rev,
            self.max_id,
            self.offset,
            self.ino,
            self._payload,
        ) = ({}, {}, {}, 0, 0, 0, None, None)

    # -------------------------------------------------------------- log
    def refresh(self):
        """Apply complete lines other processes appended (cheap stat when nothing changed)."""
        with self.tlock:
            try:
                st = os.stat(self.log)
            except FileNotFoundError:
                if self.rev or self.offset:
                    print(
                        f"notes: {self.log} disappeared: the store reads as empty until something writes it again",
                        file=sys.stderr,
                        flush=True,
                    )
                    self._reset()
                return self.rev
            if st.st_ino != self.ino or st.st_size < self.offset:
                self._reset()
                self.ino = st.st_ino
            if st.st_size == self.offset:
                return self.rev
            with open(self.log, "rb") as f:
                f.seek(self.offset)
                data = f.read(st.st_size - self.offset)
            end = data.rfind(b"\n")
            if end < 0:
                return self.rev
            for line in data[: end + 1].splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # a torn line a later writer terminated
                why = bad_event(e)
                r0 = self.rev
                if why is None:
                    try:
                        self._apply(e)
                    except (ValueError, TypeError, AttributeError, KeyError) as ex:
                        why = f"{type(ex).__name__}: {ex}"
                if (
                    why is not None
                ):  # valid JSON of the wrong shape (a hand edit, another tool): skipped and reported, never fatal
                    self.rev = (
                        max(r0 + 1, e["rev"])
                        if isinstance(e, dict) and type(e.get("rev")) is int
                        else r0 + 1
                    )
                    if isinstance(e, dict):
                        self.max_id = max(
                            self.max_id, num(e.get("id"))
                        )  # an id seen in the log is never handed out again
                    self.skipped = [*self.skipped, dict(rev=self.rev, error=str(why)[:200])][-20:]
                    self._payload = None
                    print(
                        f"notes: skipped a malformed line in {self.log} (rev {self.rev}): {str(why)[:200]}",
                        file=sys.stderr,
                        flush=True,
                    )
            self.offset += end + 1
            return self.rev

    def _apply(self, e):
        op, i, f = e.get("op"), e.get("id"), e.get("fields") or {}
        self.rev = max(self.rev + 1, e["rev"]) if type(e.get("rev")) is int else self.rev + 1
        self.max_id = max(self.max_id, num(i))
        self._payload = None
        if op == "create":
            self.notes[i] = dict(f, id=i, rev=self.rev)
            self.deleted.pop(i, None)
        elif op in ("update", "comment") and i in self.notes:
            n = dict(self.notes[i])
            if op == "update":
                for k, v in f.items():
                    if v is None:
                        n.pop(k, None)
                    else:
                        n[k] = v
            else:
                n["comments"] = [*n.get("comments", []), f]
            n.update(updated=e.get("ts") or n.get("updated"), rev=self.rev)
            a = e.get("actor") if isinstance(e.get("actor"), dict) else {}
            if op == "update":
                n["updated_by"] = {k: str(a[k]) for k in ("kind", "session") if a.get(k)}
            if op == "update" and "status" in f:
                n.update(status_at=e.get("ts"), status_by=a.get("kind"))
            self.notes[i] = n
        elif op == "delete":
            self.notes.pop(i, None)
            self.deleted[i] = self.rev
        self.touched[i] = self.rev

    @contextlib.contextmanager
    def _locked(self):
        with self.tlock:
            self.dir.mkdir(
                parents=True, exist_ok=True
            )  # recreated if someone removed the folder while a viewer runs
            with open(self.lock_path, "a+") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    self.refresh()
                    yield
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)

    def _append(self, op, i, fields, actor, derive=True):
        """Under _locked(): one event line, then the derived files (derive=False: another line follows in the same lock). Returns the event."""
        e = dict(rev=self.rev + 1, op=op, id=i, ts=now(), actor=actor, fields=fields)
        line = (json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.log, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            st = os.fstat(fd)
            if self.ino is None:
                self.ino = st.st_ino  # first event: refresh() found no log
            if st.st_size > self.offset:
                os.ftruncate(fd, self.offset)  # torn tail from a crashed writer: never acknowledged
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        self.offset += len(line)
        self._apply(e)
        if self.export and derive:
            self._derive()
        return e

    def _derive(self):
        notes = self.all()
        write_atomic(
            self.json_path,
            json.dumps(
                dict(schema=SCHEMA, rev=self.rev, updated=now(), notes=notes),
                ensure_ascii=False,
                indent=1,
            ).encode(),
        )
        write_atomic(self.md_path, self.markdown(notes).encode())

    # -------------------------------------------------------------- reads
    def all(self):
        with self.tlock:
            self.refresh()
            return [self.notes[i] for i in sorted(self.notes, key=num)]

    def get(self, nid):
        with self.tlock:
            self.refresh()
            n = self.notes.get(nid)
            if n is None:
                raise NoteNotFound(f"no note {nid}")
            return copy(n)

    def listing(self, since=None):
        """{rev, unchanged:true} when since == rev, else {rev, notes, changed?, deleted?} (changed/deleted: ids touched after since)."""
        with self.tlock:
            self.refresh()
            if since is not None and since == self.rev:
                return dict(rev=self.rev, unchanged=True)
            out = dict(rev=self.rev, notes=self.all())
            if type(since) is int and 0 <= since < self.rev:
                out.update(
                    changed=sorted(
                        (i for i, r in self.touched.items() if r > since and i in self.notes),
                        key=num,
                    ),
                    deleted=sorted((i for i, r in self.deleted.items() if r > since), key=num),
                )
            return out

    def payload(self, since=None):
        """GET /api/notes?since=REV body (bytes). The notes array is serialized once per rev; only the small header varies with since."""
        with self.tlock:
            self.refresh()
            if since == self.rev:
                return json.dumps(
                    dict(rev=self.rev, unchanged=True), separators=(",", ":")
                ).encode()
            if self._payload is None or self._payload[0] != self.rev:
                self._payload = (
                    self.rev,
                    json.dumps(self.all(), ensure_ascii=False, separators=(",", ":")).encode(),
                )
            head = dict(rev=self.rev, **({"skipped": len(self.skipped)} if self.skipped else {}))
            if type(since) is int and 0 <= since < self.rev:
                head.update(
                    changed=sorted(
                        (i for i, r in self.touched.items() if r > since and i in self.notes),
                        key=num,
                    ),
                    deleted=sorted((i for i, r in self.deleted.items() if r > since), key=num),
                )
            return (
                json.dumps(head, separators=(",", ":")).encode()[:-1]
                + b',"notes":'
                + self._payload[1]
                + b"}"
            )

    def export_data(self, fmt="md"):
        """(bytes, content type) for GET /api/notes/export?format=md|json."""
        if fmt == "json":
            return (
                json.dumps(
                    dict(
                        schema=SCHEMA,
                        rev=self.refresh(),
                        exported=now(),
                        notes=[
                            dict(
                                n,
                                targets_resolved=[
                                    describe(t, self.resolver) for t in n.get("targets") or []
                                ],
                            )
                            for n in self.all()
                        ],
                    ),
                    ensure_ascii=False,
                    indent=1,
                ).encode(),
                "application/json; charset=utf-8",
            )
        if fmt == "md":
            return self.markdown().encode(), "text/markdown; charset=utf-8"
        raise ValueError("format must be md or json")

    def find(self, query=None, ref=None, net=None, status=None, kind=None, author=None, limit=20):
        st = [status] if isinstance(status, str) else status
        kd = [kind] if isinstance(kind, str) else kind
        q = (query or "").lower().split()
        out = []
        for n in reversed(self.all()):
            if (
                st
                and n["status"] not in st
                or kd
                and n["kind"] not in kd
                or author
                and n["author"] != author
            ):
                continue
            if ref or net:
                refs, nets, _, _ = target_keys(n.get("targets"), self.resolver)
                if ref and ref not in refs or net and net not in nets:
                    continue
            if q:
                hay = " ".join(
                    [
                        n["id"],
                        n["title"],
                        n.get("body", ""),
                        " ".join(n.get("tags", [])),
                        (n.get("proposal") or {}).get("summary", ""),
                        *(c.get("text", "") for c in n.get("comments", [])),
                    ]
                ).lower()
                if not all(w in hay for w in q):
                    continue
            out.append(n)
            if len(out) >= limit:
                break
        return out

    def relevant(self, selection, lane=None, limit=8, lane_limit=5):
        """([(note, why)] whose targets meet the selection, [recent open/proposed notes of the lane]) for the Ask dossier."""
        refs, nets, src, lanes = target_keys(selection, self.resolver)
        hits = []
        pri = {"accepted": 0, "proposed": 1, "open": 2, "applied": 3, "resolved": 4, "rejected": 5}
        notes = self.all()
        if refs or nets or src or lanes:
            for n in notes:
                r2, n2, s2, l2 = target_keys(n.get("targets"), self.resolver)
                why = [
                    *sorted(refs & r2),
                    *(f"net {x}" for x in sorted(nets & n2)),
                    *(
                        f"{f}:{a}"
                        for f, a, b in s2
                        if any(f == g and a <= d and c <= b for g, c, d in src)
                    ),
                    *(f"lane {x}" for x in sorted(lanes & l2)),
                ]
                if why:
                    hits.append((n, why))
        hits.sort(key=lambda h: (-len(h[1]), pri.get(h[0]["status"], 9), -num(h[0]["id"])))
        hits = hits[:limit]
        seen = {n["id"] for n, _ in hits}
        recent = [
            n
            for n in reversed(notes)
            if lane
            and n["status"] in ("open", "proposed")
            and n["id"] not in seen
            and (
                (n.get("provenance") or {}).get("lane") == lane
                or lane in target_keys(n.get("targets"))[3]
            )
        ][:lane_limit]
        return hits, recent

    # -------------------------------------------------------------- writes
    @staticmethod
    def actor(a):
        if not isinstance(a, dict) or a.get("kind") not in ("user", "agent"):
            raise PermissionError("unknown actor")
        out = dict(kind=a["kind"])
        for k in ("session", "turn", "remote", "name"):
            if a.get(k) is not None:
                out[k] = (
                    a[k]
                    if k == "turn" and type(a[k]) is int
                    else text(str(a[k]), 100, "actor " + k, oneline=True)
                )
        return out

    def _norm(self, f, agent):
        if not isinstance(f, dict):
            raise ValueError("note fields must be an object")
        out = {}
        for k, v in f.items():
            if k == "title":
                out[k] = text(v, MAX["title"], "title", oneline=True)
            elif k == "body":
                out[k] = text(v, MAX["body"], "body", empty=True)
            elif k in ("kind", "status"):
                allowed = KINDS if k == "kind" else STATUSES
                if v not in allowed:
                    raise ValueError(f"{k} must be one of " + ", ".join(allowed))
                out[k] = v
            elif k == "targets":
                if not isinstance(v, list) or len(v) > MAX["targets"]:
                    raise ValueError(f'targets must be a list of at most {MAX["targets"]} items')
                ts = {}
                for x in v:
                    x = item(x, self.resolver if agent else None)
                    ts.setdefault(json.dumps(x, sort_keys=True), x)
                out[k] = list(ts.values())
            elif k == "tags":
                if not isinstance(v, list) or len(v) > MAX["tags"]:
                    raise ValueError(f'tags must be a list of at most {MAX["tags"]} words')
                tags = {}
                for t in v:
                    t = text(t, MAX["tag"], "tag", oneline=True)
                    if not TAG.fullmatch(t):
                        raise ValueError(f"invalid tag {t!r} (letters, digits, space and _.:/+#-)")
                    tags.setdefault(t.lower(), t)
                out[k] = list(tags.values())
            elif k == "proposal":
                if v is None:
                    out[k] = None
                    continue
                if not isinstance(v, dict) or v.get("type") not in PROPOSALS:
                    raise ValueError("proposal.type must be one of " + ", ".join(PROPOSALS))
                extra = set(v) - {"type", "summary", "diff"}
                if extra:
                    raise ValueError("unknown proposal field " + ", ".join(sorted(extra)))
                p = dict(
                    type=v["type"],
                    summary=text(
                        v.get("summary"), MAX["summary"], "proposal.summary", oneline=True
                    ),
                )  # one paragraph: details go in body/diff
                if v.get("diff") not in (None, ""):
                    p["diff"] = text(v["diff"], MAX["diff"], "proposal.diff")
                out[k] = p
            elif k == "sources":
                if not isinstance(v, list) or len(v) > MAX["sources"]:
                    raise ValueError(
                        f'sources must be a list of at most {MAX["sources"]} {{url, title}}'
                    )
                srcs = {}
                for s in v:
                    s = {"url": s} if isinstance(s, str) else s
                    if not isinstance(s, dict):
                        raise ValueError("source must be {url, title}")
                    u = text(s.get("url"), MAX["url"], "source url", oneline=True)
                    try:
                        p = urlsplit(u)
                    except ValueError:
                        p = None
                    if not p or p.scheme not in ("http", "https") or not p.hostname or " " in u:
                        raise ValueError(f"source url must be an http(s) URL: {u[:80]}")
                    srcs.setdefault(
                        u,
                        dict(
                            url=u,
                            title=text(
                                s.get("title") or p.hostname,
                                MAX["label"],
                                "source title",
                                oneline=True,
                            ),
                        ),
                    )
                out[k] = list(srcs.values())
            elif k == "links":
                if not isinstance(v, list) or len(v) > MAX["links"]:
                    raise ValueError(f'links must be a list of at most {MAX["links"]}')
                ls = []
                for x in v:
                    if not isinstance(x, dict) or x.get("kind") not in LINKS:
                        raise ValueError("link kind must be one of " + ", ".join(LINKS))
                    if agent and x["kind"] != "related":
                        raise PermissionError(
                            "only the user records where a note was applied (applied_in links)"
                        )
                    ls.append(
                        dict(
                            kind=x["kind"],
                            ref=text(x.get("ref"), MAX["ref"], "link ref", oneline=True),
                            **(
                                {"note": text(x["note"], 500, "link note", oneline=True)}
                                if x.get("note")
                                else {}
                            ),
                        )
                    )
                out[k] = ls
            elif k == "provenance":
                out[k] = self._prov(v)
            else:
                raise ValueError(f"unknown note field {k!r}")
        return out

    @staticmethod
    def _prov(v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("provenance must be an object")
        out = {}
        for k, x in v.items():
            if x is None or x == "":
                continue
            if k in ("turn", "viewer_port"):
                if type(x) is not int or not 0 <= x < 1 << 31:
                    raise ValueError(f"provenance.{k} must be an integer")
                out[k] = x
            elif k == "board_sha":
                if not (isinstance(x, str) and re.fullmatch(r"[0-9a-f]{8,64}", x)):
                    raise ValueError("provenance.board_sha must be hex")
                out[k] = x
            elif k in ("session", "lane", "phase", "view"):
                out[k] = text(str(x), 200, "provenance." + k, oneline=True)
            else:
                raise ValueError(f"unknown provenance field {k!r}")
        return out

    def create(self, fields, actor, provenance=None):
        """New note; returns it. provenance= is trusted caller data (the MCP server's per-turn environment), never model input."""
        actor = self.actor(actor)
        agent = actor["kind"] == "agent"
        if not isinstance(fields, dict):
            raise ValueError("note must be an object")
        fields = dict(fields)
        if agent:
            if fields.get("status") not in (None, "open", "proposed"):
                raise PermissionError(
                    f"only the user can mark a note {fields['status']} (Notes tab); record it as open or as a proposal"
                )
            fields.pop("status", None)
            bad = [k for k in fields if k not in AGENT_FIELDS]
            if bad:
                raise ValueError("unknown note field " + ", ".join(map(repr, bad)))
        f = self._norm(fields, agent)
        if "title" not in f:
            raise ValueError("title is required")
        if f.get("proposal") is None:
            f.pop("proposal", None)
        kind = f.setdefault("kind", "proposal" if f.get("proposal") else "observation")
        f.setdefault("status", "proposed" if kind == "proposal" or f.get("proposal") else "open")
        prov = self._prov(provenance) if provenance is not None else f.pop("provenance", {})
        f.pop("provenance", None)
        if agent and actor.get("session"):
            prov.setdefault(
                "session", actor["session"]
            )  # ownership: the conversation that wrote it
        with self._locked():
            if len(self.notes) >= MAX["notes"]:
                raise ValueError(f'the notes store is full ({MAX["notes"]} notes)')
            i = f"N-{self.max_id+1:04d}"
            ts = now()
            note = dict(
                id=i,
                created=ts,
                updated=ts,
                author=actor["kind"],
                kind=kind,
                title=f["title"],
                body=f.get("body", ""),
                targets=f.get("targets", []),
                tags=f.get("tags", []),
                status=f["status"],
                **({"proposal": f["proposal"]} if f.get("proposal") else {}),
                sources=f.get("sources", []),
                provenance=prov,
                comments=[],
                links=f.get("links", []),
            )
            self._append("create", i, note, actor)
            return copy(self.notes[i])

    def update(self, nid, fields, actor, expect_rev=None, comment=None, need_rev=False):
        """Edit fields and/or add a comment in one locked step (both validated before anything is written). expect_rev: 409
        (NoteConflict) when the note changed since; need_rev: a user accepting or applying must send it (HTTP).
        """
        actor = self.actor(actor)
        agent = actor["kind"] == "agent"
        if not isinstance(fields, dict) or (not fields and comment is None):
            raise ValueError("nothing to update")
        if expect_rev is not None and type(expect_rev) is not int:
            raise ValueError("expect_rev must be an integer")
        if agent and fields:
            if "status" in fields:
                raise PermissionError(
                    "the assistant cannot change a note's status: only the user accepts, rejects, applies or resolves notes (Notes tab)"
                )
            bad = [k for k in fields if k not in AGENT_FIELDS]
            if bad:
                raise PermissionError("the assistant cannot change " + ", ".join(bad))
        f = self._norm(fields, agent) if fields else {}
        if "title" in f and not f["title"]:
            raise ValueError("title is empty")
        c = None
        if comment is not None:
            c = dict(ts=None, author=actor["kind"], text=text(comment, MAX["comment"], "comment"))
            if actor.get("session"):
                c["session"] = actor["session"]
        with self._locked():
            n = self.notes.get(nid)
            if n is None:
                raise NoteNotFound(f"no note {nid}")
            if expect_rev is not None and n.get("rev") != expect_rev:
                raise NoteConflict(
                    f'{nid} changed since you loaded it (rev {n.get("rev")}); reload and retry'
                )
            if (
                need_rev
                and expect_rev is None
                and f.get("status") in ("accepted", "applied")
                and f["status"] != n.get("status")
            ):
                raise ValueError(
                    f'expect_rev is required to mark a note {f["status"]}: send the rev you reviewed (the Notes tab does)'
                )
            changes = {}
            if f:
                if agent:
                    if n["author"] != "agent":
                        raise PermissionError(
                            f"{nid} was written by the user: add a comment instead (comment_note)"
                        )
                    own = (n.get("provenance") or {}).get("session")
                    if not own or own != actor.get("session"):
                        raise PermissionError(
                            f"{nid} was recorded in another conversation: add a comment instead (comment_note)"
                        )
                    if n["status"] not in ("open", "proposed"):
                        raise PermissionError(
                            f"{nid} is {n['status']}: only the user can change it now; add a comment instead"
                        )
                    m = {**n, **{k: v for k, v in f.items() if v is not None}}
                    "proposal" in f and f["proposal"] is None and m.pop("proposal", None)
                    f["status"] = (
                        "proposed" if m["kind"] == "proposal" or m.get("proposal") else "open"
                    )
                changes = {k: v for k, v in f.items() if (k in n if v is None else n.get(k) != v)}
            if c is not None:
                cs = n.get("comments") or []
                if len(cs) >= MAX["comments"]:
                    raise ValueError(f'{nid} already has {MAX["comments"]} comments')
                if (
                    agent
                    and sum(1 for x in cs if isinstance(x, dict) and x.get("author") == "agent")
                    >= MAX["agent_comments"]
                ):
                    raise ValueError(
                        f'{nid} already has {MAX["agent_comments"]} assistant comments'
                    )
            if changes:
                self._append("update", nid, changes, actor, derive=c is None)
            if c is not None:
                c["ts"] = now()
                self._append("comment", nid, c, actor)
            return copy(self.notes[nid])

    def comment(self, nid, body, actor):
        return self.update(nid, {}, actor, comment=body)

    def delete(self, nid, actor):
        actor = self.actor(actor)
        if actor["kind"] != "user":
            raise PermissionError("only the user can delete notes")
        with self._locked():
            if nid not in self.notes:
                raise NoteNotFound(f"no note {nid}")
            self._append("delete", nid, {}, actor)
            return dict(id=nid, deleted=True, rev=self.rev)

    def request(self, nid, body, actor):
        """POST /api/notes/<id> body: note fields at top level or under 'fields' (status included), optional 'comment' text and
        'expect_rev' (409 when the note changed meanwhile; required to accept or apply). Fields and comment are applied together or
        not at all. Returns the note."""
        if not isinstance(body, dict):
            raise ValueError("invalid body")
        fields = dict(body.get("fields") or {}) if isinstance(body.get("fields"), dict) else {}
        fields.update({k: v for k, v in body.items() if k in USER_FIELDS})
        unknown = [
            k for k in body if k not in USER_FIELDS and k not in ("fields", "comment", "expect_rev")
        ]
        if unknown:
            raise ValueError("unknown field " + ", ".join(map(repr, unknown)))
        if not fields and body.get("comment") is None:
            raise ValueError("nothing to do")
        return self.update(
            nid,
            fields,
            actor,
            expect_rev=body.get("expect_rev"),
            comment=body.get("comment"),
            need_rev=True,
        )

    # -------------------------------------------------------------- export
    def markdown(self, notes=None, statuses=None):
        notes = self.all() if notes is None else notes
        r = self.resolver
        out = [
            "# Splanc Mini design notes",
            "",
            f"Generated {now()} from notes.jsonl rev {self.rev}: {len(notes)} note(s). Only a person sets accepted, rejected, applied or "
            "resolved (viewer Notes tab); the assistant records observations, questions and proposals. Apply the accepted notes, then mark them applied in the viewer.",
            "",
        ]
        for st, head in GROUPS:
            if statuses and st not in statuses:
                continue
            g = [n for n in notes if n.get("status") == st]
            out += [f"## {head} ({len(g)})", ""]
            if not g:
                out += ["_none_", ""]
            for n in g:
                out += note_md(n, r)
        return "\n".join(out).rstrip() + "\n"


def fence(s, ch="`"):
    run = max((len(m) for m in re.findall(re.escape(ch) + "+", s)), default=0)
    return ch * max(3, run + 1)


def one(v):
    """A value that must stay on its markdown line: line breaks flattened, hidden characters dropped (also for older notes)."""
    return HIDDEN.sub("", BREAKS.sub(" ", "" if v is None else str(v)))


def note_md(n, r=None):
    """One note in design-notes.md. Only one-line fields go on list lines (flattened by one()); the diff is a fence and the body text,
    both inside a quote (every line starts with '>'), so nothing a note says can start a heading or list line of its own.
    """
    p = n.get("provenance") or {}
    out = [f"### {one(n.get('id'))} · {one(n.get('kind'))} · {one(n.get('title'))}", ""]
    sess = lambda x: f" (session {one(x)[:8]}"
    who = one(n.get("author", "?")) + (
        sess(p["session"]) + (f", turn {one(p['turn'])}" if p.get("turn") else "") + ")"
        if p.get("session")
        else ""
    )
    ub = n.get("updated_by") if isinstance(n.get("updated_by"), dict) else {}
    out.append(
        f"- status **{one(n.get('status'))}**"
        + (
            f" (set by {one(n['status_by'])} {one(n.get('status_at'))})"
            if n.get("status_by")
            else ""
        )
        + f" · author {who} · created {one(n.get('created'))} · updated {one(n.get('updated'))}"
        + (
            f" by {one(ub.get('kind'))}" + (sess(ub["session"]) + ")" if ub.get("session") else "")
            if ub.get("kind")
            else ""
        )
    )
    prov = [
        f'{lbl} {one(str(p[k])[:16] if k=="board_sha" else p[k])}'
        for k, lbl in (
            ("lane", "lane"),
            ("phase", "phase"),
            ("board_sha", "board"),
            ("viewer_port", "viewer port"),
        )
        if p.get(k) not in (None, "")
    ]
    if prov:
        out.append("- context: " + " · ".join(prov))
    ts = n.get("targets") or []
    if ts:
        out += ["- targets:", *(f"  - {one(describe(t,r))}" for t in ts)]
    if n.get("tags"):
        out.append("- tags: " + ", ".join(one(t) for t in n["tags"]))
    pr = n.get("proposal")
    if pr:
        out.append(f"- proposal ({one(pr.get('type'))}): {one(pr.get('summary'))}")
        if pr.get("diff"):
            d = HIDDEN.sub("", BREAKS.sub("\n", str(pr["diff"]))).rstrip("\n")
            fc = fence(d)
            out += ["", *("> " + x if x else ">" for x in [fc + "diff", *d.split("\n"), fc]), ""]
    if n.get("sources"):
        out.append(
            "- sources: "
            + ", ".join(
                f"[{one(s.get('title','')).replace(']',')').replace('[','(')}]({one(s.get('url','')).replace(')','%29').replace(' ','%20')})"
                for s in n["sources"]
            )
        )
    for l in n.get("links") or []:
        out.append(
            f"- {one(l.get('kind'))}: `{one(l.get('ref')).replace('`',chr(39))}`"
            + (f" ({one(l['note'])})" if l.get("note") else "")
        )
    body = HIDDEN.sub("", n.get("body") or "").strip()
    if body:
        out += ["", *("> " + x if x.strip() else ">" for x in BREAKS.split(body))]
    cs = n.get("comments") or []
    if cs:
        out += [
            "",
            "Comments:",
            *(
                f"- {one(c.get('ts'))} {one(c.get('author'))}: " + one(c.get("text", ""))
                for c in cs
            ),
        ]
    return out + [""]


# ------------------------------------------------------------------ CLI
def default_resolver():
    """Newest cached viewer source index (types, titles, source lines) under the hier output, else graph.json."""
    idx = sorted(
        HIER.glob("*/source/source-index-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for p in idx + [DEFAULT_GRAPH]:
        try:
            return resolver_for(p)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Splanc design notes (read-only reports; status changes are made by a person in the viewer)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser(
        "report", help="notes for the design loop (default: every status, grouped markdown)"
    )
    r.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    r.add_argument("--status", action="append", choices=STATUSES)
    r.add_argument("--kind", action="append", choices=KINDS)
    r.add_argument("--json", action="store_true", help="JSON list with resolved targets")
    r.add_argument(
        "--index",
        type=Path,
        help="source index / graph.json used to describe targets (default: newest viewer source index)",
    )
    a = ap.parse_args(argv)
    if not a.dir.is_dir():
        print(f"no notes folder {a.dir}", file=sys.stderr)
        return 1  # a typo; an unused store just prints empty groups
    s = NotesStore(a.dir, resolver=a.index or default_resolver(), export=False)
    notes = [
        n
        for n in s.all()
        if (not a.status or n["status"] in a.status) and (not a.kind or n["kind"] in a.kind)
    ]
    if a.json:
        print(
            json.dumps(
                [
                    dict(
                        n,
                        targets_resolved=[describe(t, s.resolver) for t in n.get("targets") or []],
                    )
                    for n in notes
                ],
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        sys.stdout.write(s.markdown(notes, statuses=a.status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
