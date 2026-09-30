"""Opt-in, observational trace of a place-and-route run (format ``pnr-trace-v1``).

Set ``PNR_TRACE_DIR`` to a directory to record what the engine does: the global placement
iterations, the legalization order, every net the detailed router adds, rips or commits, the
selections among candidates and, from the ladder runner, the native KiCad stages. Unset, each
hook costs one environment lookup (hoisted out of loops) and nothing else is imported. The
engine never reads a trace, and no decision depends on one: hooks use no random number
generator, add no torch operation and mutate no engine state; ids are counters. The renderer
(``pnr.animate``) and the provenance model (``pnr.provenance``) read traces.

Layout of a trace directory::

    run.json               written by an orchestrator (the ladder runner): subject, stages
    header.json            board, stack-up, components with pads, nets, connection count
    streams/<lane>.jsonl   one writer per file (exclusive create), events in emission order
    blobs/<sha256>.json    geometry, content-addressed canonical JSON
    errors.json            only if a recorder disabled itself

A lane is a writer process: ``engine`` (place and route) and ``native`` (the ladder runner's
KiCad stages). A child process takes its lane from ``PNR_TRACE_LANE``; a second writer of a
lane gets ``<lane>.<n>.jsonl``.

Units and frame: integer micrometres in the engine frame of :mod:`pnr.graph` (y up, origin at
the outline's bottom-left corner), angles in degrees counter-clockwise, layers as indices into
``header.copper_layers`` (outer to outer).

Events carry ``seq`` (per stream), ``kind``, ``scope`` (a path-like id such as
``initial-pool/start-05`` or ``round-02/route``, never a file path) and, where known,
``phase`` and ``progress`` (``{done, total, source}`` connections, where ``total`` is
``header.connections_total``, the ratsnest edge count of the unrouted board). Kinds:

========== ==============================================================================
scope_begin ``type``, ``parent``, ``meta``
scope_end   ``type``, ``status`` (ok, illegal, failed, dropped), ``metrics``
poses       ``stage`` (global, legal, round, ...), ``iter``, ``iters``, ``poses`` (inline up
            to 64 parts) or ``poses_blob``: ``[[ref, x, y, rot, side]]``; with rigid bodies
            (below) also ``groups`` and ``group_members``
legal       the legalizer's accepted order: ``order`` (poses in placement order),
            ``backtracks``; with rigid bodies also ``groups`` and ``group_members``
select      ``id``, ``among``, ``chosen`` (an id or a list of ids), ``criterion``, ``scores``
route_begin ``pitch``, ``layers``, ``nets``, ``plane_nets``, ``deferred``, ``max_iters``
net         ``net``, ``op`` (add, rip, commit, drop), ``pass``, ``provisional``,
            ``copper`` (blob), ``groups`` (pin indices joined by the route), ``progress``
route_end   ``nets`` ({net: copper blob}), ``groups``, ``unrouted``, ``deferred``,
            ``progress``
congestion  ``pitch``, ``nx``, ``ny``, ``cells`` (0 to 255), ``inflation``
board       ``stage``, ``copper`` (blob with zones), ``poses``, ``drc`` (counts, ``open_pairs``,
            ``findings``: each violation's first item position), ``progress``
result      the acceptance fields of the ladder's ``result.json``
truncated   ``bytes``, ``level``, ``dropped_kinds``
========== ==============================================================================

Blobs: ``copper`` is ``{"tracks": [[layer, x0, y0, x1, y1, width]], "vias": [[x, y,
diameter, drill]], "zones": [[layer, net, [ring, ...]]]}``; ``poses`` is a list of poses.

Rigid bodies (line groups of :mod:`pnr.place.line_group`, block macros of
:mod:`pnr.hier.macro`): the placer and legalizer then see a macro (``LG00``, ``MB00``)
in place of its members. While :func:`pose_expansion` is installed, ``poses`` and
``legal`` name the members instead (member pose = macro pose plus the rotated member
offset, as the macro expansion poses them; a macro in the legal order becomes its
members in member order) and add ``groups`` (the macros' own rows) and
``group_members`` (``{macro: [member refs]}``). ``header.constraints`` lists the
``line_group``, ``edge_align``, ``group`` and ``row`` constraints of a design that has
any (``kind``, ``refs``, ``hard``, ``name`` and the fields of the kind; lengths in
micrometres); it is absent otherwise.

Bounds: global placement records a snapshot every ``ceil(iters / 24)`` steps and the last one
(``PNR_TRACE_PLACEMENT_EVERY`` overrides); router detail only inside a ``route`` scope opened
by a caller; a trace stops growing at ``PNR_TRACE_MAX_MB`` (default 64): past half of it,
provisional negotiation events are dropped, past 90 % only scopes, selections, boards and
results are written, each step announced by one ``truncated`` event.

Failures never reach the engine: the first recorder error disables recording for the process
and writes ``errors.json``. Stdlib only and parseable by Python 3.9 (KiCad's bundled Python).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import re
from pathlib import Path

SCHEMA = "pnr-trace-v1"
RECORDER_VERSION = 1
ENV_DIR = "PNR_TRACE_DIR"
ENV_LANE = "PNR_TRACE_LANE"
ENV_MAX_MB = "PNR_TRACE_MAX_MB"
ENV_PLACEMENT_EVERY = "PNR_TRACE_PLACEMENT_EVERY"
DEFAULT_MAX_MB = 64.0
PLACEMENT_SNAPSHOTS = 24
INLINE_POSES = 64
ANGLES = (0.0, 90.0, 180.0, 270.0)

SCOPE_TYPES = (
    "round",
    "attempt",
    "pool",
    "start",
    "finalist",
    "route",
    "stage",
    "block",
    "candidate",
)
STATUSES = ("ok", "illegal", "failed", "dropped")
# Phase keys; the renderer maps them to display text (never free text in an overlay).
PHASES = (
    "source",
    "pool",
    "global-placement",
    "legalization",
    "placement",
    "negotiation",
    "commit",
    "rip-up",
    "routed",
    "congestion",
    "selection",
    "writeback",
    "planes",
    "refill",
    "drc",
    "result",
)
# Written even past 90 % of the size budget.
ESSENTIAL = frozenset(("scope_begin", "scope_end", "select", "board", "result", "truncated"))
_LANE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PATHLIKE = re.compile(r"(/[\w.~-]+){2,}|[A-Za-z]:\\")
# KiCad names a custom (.kicad_dru) rule in a finding's description: "(rule 'name' ...".
_DRC_RULE = re.compile(r"\(rule '([A-Za-z0-9_.-]{1,64})'")


def um(value):
    """Millimetres to integer micrometres."""
    return int(round(float(value) * 1000.0))


def angle(value):
    """A rotation normalized to [0, 360), rounded to 1e-3 degrees."""
    a = round(float(value) % 360.0, 3)
    return 0.0 if a >= 360.0 else a


def _clean(value):
    """JSON-safe copy: finite floats rounded to 6 decimals, non-finite ones as None."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [_clean(v) for v in items]
    if hasattr(value, "tolist"):
        return _clean(value.tolist())
    try:
        return _clean(float(value))
    except (TypeError, ValueError):
        return str(value)


def canonical(obj):
    """Canonical JSON bytes (sorted keys, no spaces): the blob encoding."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def copper_layer_names(count):
    """Copper layer names of an ``count``-layer board, outer to outer."""
    count = int(count)
    if count <= 1:
        return ["F.Cu"]
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def graph_poses(graph):
    """``[[ref, x, y, rot, side]]`` of a :class:`pnr.graph.BoardGraph`, sorted by ref."""
    return [
        [c.ref, um(c.pos[0]), um(c.pos[1]), angle(c.rot), c.side]
        for c in sorted(graph.components, key=lambda c: c.ref)
    ]


def _guarded(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.active:
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 - diagnostics never fail the engine
            self._disable(method.__name__, error)
            return None

    return wrapper


class Recorder:
    """One process's writer of one lane of a trace directory."""

    def __init__(self, root, lane="engine", max_mb=None):
        if not _LANE.match(str(lane)):
            raise ValueError("invalid trace lane name")
        self.root = Path(root)
        self.lane = str(lane)
        self.key = (str(root), os.getpid())
        self.active = True
        self.seq = 0
        self.stack = []
        self.used = {}
        self.route = None
        self.layers = None
        self.refs = None
        self.pins = {}
        self.written = 0
        self.level = 0
        self.dropped = {}
        self.expanders = []
        self._blobs = set()
        if max_mb is None:
            max_mb = float(os.environ.get(ENV_MAX_MB) or DEFAULT_MAX_MB)
        self.max_bytes = max(1, int(float(max_mb) * 1024 * 1024))
        (self.root / "streams").mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(exist_ok=True)
        self.stream_name, self._stream = self._open_stream()
        self._load_header()

    # --- files --------------------------------------------------------------------------
    def _open_stream(self):
        folder = self.root / "streams"
        for n in range(1000):
            name = self.lane + (".jsonl" if n == 0 else ".%d.jsonl" % n)
            try:
                fd = os.open(str(folder / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            return name, os.fdopen(fd, "w", encoding="utf-8")
        raise RuntimeError("no free trace stream name")

    def _load_header(self):
        path = self.root / "header.json"
        if path.is_file():
            self._adopt_header(json.loads(path.read_text()))

    def _adopt_header(self, header):
        self.layers = list(header.get("copper_layers") or [])
        self.refs = sorted(c["ref"] for c in header.get("components", []))
        self.pins = {n["name"]: [tuple(p) for p in n["pins"]] for n in header.get("nets", [])}

    def _disable(self, where, error):
        self.active = False
        self.route = None
        record_error(self.root, self.lane, where, error, stream=self.stream_name)
        with contextlib.suppress(Exception):
            self._stream.close()

    def close(self):
        """Close the stream (the recorder stays usable for nothing else)."""
        if self.active:
            self.active = False
            self._stream.close()

    # --- low level ----------------------------------------------------------------------
    @property
    def scope_id(self):
        return self.stack[-1]["id"] if self.stack else ""

    def _budget(self, kind, droppable=False):
        """True if an event of ``kind`` may be written at the current size (``droppable``:
        a provisional event, the first to go)."""
        if self.level < 2 and self.written > 0.9 * self.max_bytes:
            self._truncate(2)
        elif self.level < 1 and self.written > 0.5 * self.max_bytes:
            self._truncate(1)
        if kind in ESSENTIAL:
            return True
        if self.level >= 2 or (self.level >= 1 and droppable):
            self.dropped[kind] = self.dropped.get(kind, 0) + 1
            return False
        return True

    def _truncate(self, level):
        self.level = level
        self._write(
            dict(kind="truncated", level=level, bytes=self.written, dropped_kinds=self.dropped)
        )

    def _write(self, event):
        event = dict(event, seq=self.seq, scope=event.get("scope", self.scope_id))
        self.seq += 1
        line = json.dumps(_clean(event), sort_keys=True, separators=(",", ":"), allow_nan=False)
        self._stream.write(line + "\n")
        self._stream.flush()
        self.written += len(line) + 1

    def _emit(self, kind, droppable=False, **fields):
        if not self._budget(kind, droppable):
            return False
        fields["kind"] = kind
        self._write(fields)
        return True

    @_guarded
    def blob(self, obj):
        """Store ``obj`` as a content-addressed blob; return its SHA-256 name."""
        data = canonical(obj)
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self._blobs:
            self._blobs.add(digest)
            path = self.root / "blobs" / (digest + ".json")
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return digest
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            self.written += len(data)
        return digest

    @_guarded
    def event(self, kind, **fields):
        """Write a generic event of ``kind`` (see the module docstring)."""
        return self._emit(kind, **fields)

    # --- header -------------------------------------------------------------------------
    @_guarded
    def begin_board(self, graph, constraints=None, rules=None):
        """Write ``header.json`` once per trace; a later call must name the same parts."""
        header = board_header(graph, constraints, rules)
        path = self.root / "header.json"
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            existing = json.loads(path.read_text())
            refs = sorted(c["ref"] for c in existing.get("components", []))
            if refs != sorted(c["ref"] for c in header["components"]):
                raise ValueError("trace header names other components")
            self._adopt_header(existing)
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(header, indent=1, sort_keys=True) + "\n")
        self._adopt_header(header)
        return True

    # --- scopes -------------------------------------------------------------------------
    def _new_id(self, name):
        if "/" in name or not name:
            raise ValueError("scope names are single path components")
        base = self.scope_id + "/" + name if self.stack else name
        n = self.used.get(base, 0) + 1
        self.used[base] = n
        return base if n == 1 else "%s~%d" % (base, n)

    @_guarded
    def enter(self, name, type, **meta):
        """Open a scope ``name`` (one path component) of ``type`` inside the current one."""
        if type not in SCOPE_TYPES:
            raise ValueError("unknown scope type")
        parent = self.scope_id
        scope = dict(id=self._new_id(str(name)), type=type, metrics={}, status=None)
        self.stack.append(scope)
        self._emit("scope_begin", type=type, parent=parent, meta=meta)
        return scope["id"]

    @_guarded
    def section(self, name, type, **meta):
        """Close an open scope of the same ``type`` (and what it holds), then open ``name``."""
        self._close_type(type)
        return self.enter(name, type, **meta)

    @_guarded
    def leave(self, scope_id=None, type=None, status=None, **metrics):
        """Close the scope ``scope_id`` (or the innermost of ``type``) and its inner scopes."""
        for index in range(len(self.stack) - 1, -1, -1):
            scope = self.stack[index]
            if (scope_id is None or scope["id"] == scope_id) and (
                type is None or scope["type"] == type
            ):
                break
        else:
            return False
        while len(self.stack) > index:
            inner = self.stack[-1]
            if len(self.stack) == index + 1:
                inner["metrics"].update(metrics)
                inner["status"] = status or inner["status"]
            self._pop()
        return True

    def _close_type(self, type):
        if any(s["type"] == type for s in self.stack):
            self.leave(type=type)

    def _pop(self):
        scope = self.stack[-1]
        status = scope["status"] or "ok"
        if status not in STATUSES:
            status = "failed"
        self._emit("scope_end", type=scope["type"], status=status, metrics=scope["metrics"])
        self.stack.pop()
        if scope["type"] == "route":
            self.route = None

    @_guarded
    def note(self, status=None, **metrics):
        """Add metrics (and a status) to the current scope's ``scope_end``."""
        if self.stack:
            self.stack[-1]["metrics"].update(metrics)
            if status is not None:
                self.stack[-1]["status"] = status

    def in_route_scope(self):
        return bool(self.active and self.stack and self.stack[-1]["type"] == "route")

    def resolve(self, name):
        """A scope id from a name relative to the current scope (``/`` prefix: absolute)."""
        name = str(name)
        if name.startswith("/"):
            return name[1:]
        return self.scope_id + "/" + name if self.stack else name

    # --- placement ----------------------------------------------------------------------
    def _expand(self, rows):
        """Rows through the installed pose expanders, innermost first: ``(rows, groups,
        members)`` (:func:`pose_expansion`)."""
        groups, members = [], {}
        for expander in reversed(self.expanders):
            rows, more, names = expander(rows)
            groups += [list(g) for g in more]
            members.update(names)
        return rows, sorted(groups), dict(sorted(members.items()))

    @_guarded
    def poses(self, stage, poses, iter=None, iters=None, phase="placement", **fields):
        """A pose set: a :class:`BoardGraph` or ``[[ref, x, y, rot, side]]``."""
        if hasattr(poses, "components"):
            poses = graph_poses(poses)
        if self.expanders:
            poses, groups, members = self._expand(poses)
            poses = sorted(poses)
            fields.update(groups=groups, group_members=members)
        fields.update(stage=stage, iter=iter, iters=iters, phase=phase)
        if len(poses) <= INLINE_POSES:
            fields["poses"] = poses
        else:
            fields["poses_blob"] = self.blob(poses)
        return self._emit("poses", **fields)

    @_guarded
    def legal(self, order, placed, backtracks=0):
        """The legalizer's accepted placement order (refs) and the legal ``placed`` graph."""
        rows = graph_poses(placed)
        extra = {}
        if self.expanders:
            rows, groups, members = self._expand(rows)
            order = [m for r in order for m in members.get(r, [r])]
            extra = dict(groups=groups, group_members=members)
        by_ref = {p[0]: p for p in rows}
        ordered = [by_ref[r] for r in order if r in by_ref]
        seen = set(order)
        ordered = [p for r, p in sorted(by_ref.items()) if r not in seen] + ordered
        if len(ordered) > INLINE_POSES:
            return self._emit(
                "legal",
                order_blob=self.blob(ordered),
                backtracks=backtracks,
                phase="legalization",
                **extra,
            )
        return self._emit(
            "legal", order=ordered, backtracks=backtracks, phase="legalization", **extra
        )

    # --- selections, congestion, native boards, results --------------------------------
    @_guarded
    def select(self, name, among, chosen, criterion, scores=None):
        """A ranking that picked ``chosen`` (an id or a list) from ``among``."""
        sid = self.resolve(name)
        among = [self.resolve(a) for a in among]
        if isinstance(chosen, (list, tuple)):
            chosen = [self.resolve(c) for c in chosen]
        elif chosen is not None:
            chosen = self.resolve(chosen)
        scores = {self.resolve(k): v for k, v in (scores or {}).items()}
        return self._emit(
            "select",
            id=sid,
            among=among,
            chosen=chosen,
            criterion=criterion,
            scores=scores,
            phase="selection",
        )

    @_guarded
    def congestion(self, cells, pitch, inflation=None):
        """Accumulated routing pressure on a coarse grid (``cells[i][j]``, i along x)."""
        if hasattr(cells, "tolist"):
            cells = cells.tolist()
        peak = max((max(row) for row in cells if row), default=0.0)
        scale = 255.0 / peak if peak > 0 else 0.0
        quantized = [[int(round(max(0.0, v) * scale)) for v in row] for row in cells]
        return self._emit(
            "congestion",
            pitch=um(pitch),
            nx=len(quantized),
            ny=len(quantized[0]) if quantized else 0,
            cells=quantized,
            inflation={k: round(float(v), 3) for k, v in sorted((inflation or {}).items())},
            phase="congestion",
        )

    @_guarded
    def board(self, stage, parsed, drc=None, total=None):
        """A saved board (``pnr.trace_board.read``) with its KiCad DRC report."""
        fields = dict(stage=stage, phase=stage if stage in PHASES else "drc")
        fields["copper"] = self.blob(parsed["copper"])
        fields["poses"] = parsed["poses"]
        if drc is not None:
            summary = drc_summary(drc, parsed["frame"])
            fields["drc"] = summary
            if total is not None:
                done = max(0, int(total) - summary["unconnected"])
                fields["progress"] = dict(done=done, total=int(total), source="kicad")
        return self._emit("board", **fields)

    @_guarded
    def result(self, **fields):
        return self._emit("result", phase="result", **fields)


def record_error(root, lane, where, error, stream=None):
    """Write ``errors.json`` (or ``errors.<n>.json``): why a recorder disabled itself. The
    message is kept path-free."""
    message = _PATHLIKE.sub("<path>", str(error))[:500]
    record = dict(lane=lane, stream=stream, where=where, error=type(error).__name__)
    record["message"] = message
    with contextlib.suppress(Exception):
        Path(root).mkdir(parents=True, exist_ok=True)
        for n in range(1000):
            name = "errors.json" if n == 0 else "errors.%d.json" % n
            try:
                fd = os.open(str(Path(root) / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            break


class _Disabled:
    """Placeholder for a trace directory that could not be opened in this process."""

    active = False

    def __init__(self, key):
        self.key = key


_RECORDER = None


def current():
    """This process's recorder for ``PNR_TRACE_DIR``, or None when tracing is off."""
    global _RECORDER
    root = os.environ.get(ENV_DIR)
    if not root:
        return None
    recorder = _RECORDER
    if recorder is None or recorder.key != (root, os.getpid()):
        try:
            recorder = Recorder(root, lane=os.environ.get(ENV_LANE) or "engine")
        except Exception:  # noqa: BLE001 - an unusable trace directory disables tracing
            recorder = _Disabled((root, os.getpid()))
        _RECORDER = recorder
    return recorder if recorder.active else None


class _Scope:
    def __init__(self, recorder, name, type, meta):
        self.recorder, self.name, self.type, self.meta = recorder, name, type, meta
        self.id = None

    def __enter__(self):
        self.id = self.recorder.enter(self.name, self.type, **self.meta)
        return self.id

    def __exit__(self, exc_type, exc, tb):
        if self.id is not None and self.recorder.active:
            status = None
            if exc_type is not None:
                illegal = exc_type.__name__ == "LegalizationError"
                status = "illegal" if illegal else "failed"
            self.recorder.leave(self.id, status=status)
        return False


_NULL = contextlib.nullcontext()


def scope(name, type, **meta):
    """A context manager for a scope; a shared no-op when tracing is off."""
    recorder = current()
    if recorder is None:
        return _NULL
    return _Scope(recorder, name, type, meta)


def note(status=None, **metrics):
    recorder = current()
    if recorder is not None:
        recorder.note(status=status, **metrics)


def select(name, among, chosen, criterion, scores=None):
    recorder = current()
    if recorder is not None:
        recorder.select(name, among, chosen, criterion, scores)


class _Expansion:
    def __init__(self, recorder, expander):
        self.recorder, self.expander = recorder, expander

    def __enter__(self):
        self.recorder.expanders.append(self.expander)
        return self.expander

    def __exit__(self, exc_type, exc, tb):
        with contextlib.suppress(ValueError):
            self.recorder.expanders.remove(self.expander)
        return False


def pose_expansion(expander):
    """A context manager that makes ``poses`` and ``legal`` events name a rigid body's
    members while it is open (see the module docstring); a shared no-op when tracing is
    off. ``expander(rows)`` returns ``(rows, groups, members)``, e.g.
    :meth:`pnr.hier.macro.MacroPlan.trace_rows`. Recording only: the engine's own
    poses are never touched."""
    recorder = current()
    if recorder is None:
        return _NULL
    return _Expansion(recorder, expander)


def route_hook():
    """The router-side hook of an open, traced ``route`` scope, or None."""
    recorder = current()
    return recorder.route if recorder is not None else None


class PlacementTracer:
    """Snapshots of a global placement optimizer (positions and arg-max rotations)."""

    def __init__(self, recorder, components, iters, every=None):
        self.recorder = recorder
        self.refs = [c.ref for c in components]
        self.sides = [c.side for c in components]
        self.iters = int(iters)
        if every is None:
            every = int(os.environ.get(ENV_PLACEMENT_EVERY) or 0)
        if every <= 0:
            every = max(1, int(math.ceil(self.iters / float(PLACEMENT_SNAPSHOTS))))
        self.every = every

    def due(self, step):
        return self.recorder.active and (step % self.every == 0 or step == self.iters - 1)

    def snapshot(self, step, pos, probs):
        try:
            self._snapshot(step, pos, probs)
        except Exception as error:  # noqa: BLE001 - diagnostics never fail the engine
            self.recorder._disable("placement_snapshot", error)

    def _snapshot(self, step, pos, probs):
        xy = pos.detach().tolist()
        rot = [ANGLES[max(range(len(row)), key=row.__getitem__)] for row in probs.detach().tolist()]
        poses = [
            [ref, um(x), um(y), a, side]
            for ref, (x, y), a, side in zip(self.refs, xy, rot, self.sides)
        ]
        self.recorder.poses(
            "global", sorted(poses), iter=int(step), iters=self.iters, phase="global-placement"
        )

    def finish(self, positions, rotations):
        if not self.recorder.active:
            return
        try:
            self._finish(positions, rotations)
        except Exception as error:  # noqa: BLE001 - diagnostics never fail the engine
            self.recorder._disable("placement_finish", error)

    def _finish(self, positions, rotations):
        poses = [
            [ref, um(positions[ref][0]), um(positions[ref][1]), angle(rotations[ref]), side]
            for ref, side in zip(self.refs, self.sides)
        ]
        self.recorder.poses(
            "global", sorted(poses), iter=self.iters, iters=self.iters, phase="global-placement"
        )


def placement_tracer(components, iters):
    """A :class:`PlacementTracer` for one global placement, or None when tracing is off."""
    recorder = current()
    if recorder is None:
        return None
    try:
        return PlacementTracer(recorder, components, iters)
    except Exception as error:  # noqa: BLE001 - diagnostics never fail the engine
        recorder._disable("placement_tracer", error)
        return None


def legal(order, placed, backtracks=0):
    recorder = current()
    if recorder is not None:
        recorder.legal(order, placed, backtracks)


def board_event(stage, board_path, drc_report=None):
    """A ``board`` event for a saved ``.kicad_pcb`` (phase captures of full iterations)."""
    recorder = current()
    if recorder is None:
        return
    try:
        from pnr.trace_board import read

        parsed = read(board_path)
    except Exception as error:  # noqa: BLE001
        recorder._disable("board_event", error)
        return
    total = None
    header = recorder.root / "header.json"
    if header.is_file():
        with contextlib.suppress(Exception):
            total = json.loads(header.read_text()).get("connections_total")
    recorder.board(stage, parsed, drc_report, total)


def drc_rules(report):
    """KiCad DRC findings by the custom rule they break, or by type when KiCad names none."""
    counts = {}
    for item in report.get("violations") or []:
        match = _DRC_RULE.search(str(item.get("description", "")))
        key = match.group(1) if match else str(item.get("type", "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def drc_summary(report, frame):
    """Counts (by type and by rule) and open pairs (engine frame) of a KiCad DRC JSON report."""
    left, bottom = frame
    unconnected = report.get("unconnected_items") or []
    violations = report.get("violations") or []
    by_type = {}
    findings = []
    for item in violations:
        key = str(item.get("type", "unknown"))
        by_type[key] = by_type.get(key, 0) + 1
        where = [i["pos"] for i in item.get("items", []) if isinstance(i.get("pos"), dict)]
        if where:
            findings.append([um(float(where[0]["x"]) - left), um(bottom - float(where[0]["y"]))])
    pairs = []
    for item in unconnected:
        points = [
            (um(float(i["pos"]["x"]) - left), um(bottom - float(i["pos"]["y"])))
            for i in item.get("items", [])
            if isinstance(i.get("pos"), dict)
        ]
        if len(points) >= 2:
            pairs.append([points[0][0], points[0][1], points[1][0], points[1][1]])
    return dict(
        unconnected=len(unconnected),
        violations=len(violations),
        by_type=by_type,
        by_rule=drc_rules(report),
        open_pairs=pairs,
        findings=sorted(findings),
    )


def _pad_shape(pad):
    if pad.land_corner is not None:
        short = min(pad.size) / 2.0 if min(pad.size) > 0 else 0.0
        if short > 0 and pad.land_corner >= short - 1e-6:
            return "circle" if abs(pad.size[0] - pad.size[1]) < 1e-6 else "oval"
        return "roundrect" if pad.land_corner > 0 else "rect"
    if pad.through_hole and abs(pad.size[0] - pad.size[1]) < 1e-6:
        return "circle"
    return "rect"


HEADER_CONSTRAINTS = ("line_group", "edge_align", "group", "row")


def header_constraints(constraints):
    """``header.constraints``: the placement relations a renderer can highlight."""
    out = []
    for con in getattr(constraints, "constraints", None) or []:
        kind = getattr(con, "kind", None)
        if kind not in HEADER_CONSTRAINTS:
            continue
        params = con.params or {}
        entry = dict(
            kind=kind,
            refs=list(con.refs),
            hard=getattr(con.enforcement, "value", None) == "hard",
        )
        if con.name:
            entry["name"] = str(con.name)
        if kind in ("edge_align", "line_group", "row"):
            entry["edge"] = params.get("edge") or "none"
        lengths = dict(
            tolerance_um=params.get("tolerance_mm") if kind == "edge_align" else None,
            pitch_um=params.get("pitch_mm"),
            gap_um=params.get("gap_mm"),
            radius_um=params.get("radius_mm") if kind == "group" else None,
        )
        entry.update({k: um(v) for k, v in lengths.items() if v is not None})
        if kind == "line_group":
            entry["rot"] = angle(params.get("rot") or 0.0)
        if kind == "group" and params.get("anchor"):
            entry["anchor"] = str(params["anchor"])
        out.append(entry)
    return out


def board_header(graph, constraints=None, rules=None):
    """The ``header.json`` document of a board graph under its constraints and rules."""
    board = getattr(constraints, "board", None)
    width = getattr(board, "width", None) or (graph.outline.width if graph.outline else 0.0)
    height = getattr(board, "height", None) or (graph.outline.height if graph.outline else 0.0)
    polygon = None
    if graph.outline is not None and graph.outline.polygon and len(graph.outline.polygon) > 4:
        polygon = [[um(x), um(y)] for x, y in graph.outline.polygon]
    count = int(getattr(board, "layers", 0) or (rules or {}).get("layers") or 2)
    rules = rules or {}
    default_width = float((rules.get("fab") or {}).get("track_width_mm") or 0.15)
    widths, planes = {}, {}
    for cls in rules.get("net_classes", []) or []:
        for name in cls.get("nets", []) or []:
            if cls.get("plane_layer"):
                planes[name] = cls["plane_layer"]
            elif cls.get("width_mm"):
                widths[name] = float(cls["width_mm"])
    fixed = set()
    for con in getattr(constraints, "constraints", None) or []:
        if getattr(con, "kind", None) == "fixed":
            fixed.update(con.refs)
    components = []
    for c in sorted(graph.components, key=lambda c: c.ref):
        pads = []
        for p in c.pads:
            drill = [um(d) for d in p.drill_size] if p.through_hole else None
            corner = um(p.land_corner) if p.land_corner is not None else None
            shape = _pad_shape(p)
            pads.append(
                dict(
                    name=p.name,
                    net=p.net,
                    offset=[um(p.offset[0]), um(p.offset[1])],
                    size=[um(p.size[0]), um(p.size[1])],
                    shape=shape,
                    corner=corner,
                    drill=drill,
                    angle=0.0,
                )
            )
        components.append(
            dict(
                ref=c.ref,
                footprint=c.footprint,
                courtyard=[um(c.courtyard[0]), um(c.courtyard[1])],
                fixed=bool(c.ref in fixed or c.locked),
                pos=[um(c.pos[0]), um(c.pos[1])],
                rot=angle(c.rot),
                side=c.side,
                pads=pads,
            )
        )
    nets, total = [], 0
    for n in sorted(graph.nets, key=lambda n: n.name):
        pins = [[r, p] for r, p in n.pins]
        total += max(0, len(pins) - 1)
        nets.append(
            dict(
                name=n.name,
                pins=pins,
                width=um(widths.get(n.name, default_width)),
                plane=planes.get(n.name),
            )
        )
    header = dict(
        schema=SCHEMA,
        recorder=RECORDER_VERSION,
        outline=dict(w=um(width), h=um(height), polygon=polygon),
        copper_layers=copper_layer_names(count),
        plane_layers=dict(sorted(planes.items())),
        components=components,
        nets=nets,
        connections_total=total,
    )
    relations = header_constraints(constraints)
    if relations:
        header["constraints"] = relations
    return header


def write_run(root, document):
    """Write ``run.json`` (the orchestrator's description of a traced run)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "run.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
