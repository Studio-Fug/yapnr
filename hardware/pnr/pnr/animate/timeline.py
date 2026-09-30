"""The timeline: a storyboard's scenes as frames, each a :class:`View` with a duration.

A :class:`View` is everything one frame shows (poses, copper by net and state, the ratsnest's
pad groups or KiCad's open pairs, overlays, the camera, cards and montage tiles); the renderer
draws it. Frames carry their own durations, so a hold costs one frame. Motion uses
ease-in-out cubic. Scene durations follow the design table and are scaled down together when
their sum exceeds ``max_seconds``; ``pacing="showcase"`` gives placement, legalization and
routing more time (docs/design/constraint-and-hier-animations.md, section 6.5).

Rigid bodies (a line group or a block macro, recorded as ``groups`` rows with
``group_members``) move as one: between two snapshots the body's centre is interpolated
linearly and its angle along the shorter arc, and its members are posed from that pose, never
one by one (which would shrink a line mid-turn). A half-turn has no shorter arc, and the
placer's rotation is a snapped four-way choice, so a body recorded at two opposite angles
flips at the middle of the interval instead of sweeping through angles it never had; the
showcase pacing does the same for single parts. The legalizer's order places a body in one
step. ``marks`` records where each scene begins (frame index and scene type) without changing
the frames, so a comparison can synchronize two runs by phase.

The showcase pacing also widens the camera over a global placement whose recorded poses leave
the board (the board then zooms back in after legalization), and with ``replay_pool`` a pool's
shortlist montage first replays every start's recorded global placement in its tile.
"""

from __future__ import annotations

import copy
import math

from .storyboard import score_key
from .theme import MONTAGE_TEXT, PHASE_TEXT, criterion_text

TITLE_S = 1.0
SOURCE_S = 0.8
MONTAGE_IN_S, MONTAGE_HOLD_S, MONTAGE_ZOOM_S = 0.4, 1.0, 0.6
ATTEMPT_S = 0.3
GLOBAL_S = (2.0, 3.0)
FLY_IN_S = 0.4
LEGAL_PART_S, LEGAL_MAX_S = 0.08, 1.6
MOVE_S = 0.6
ROUTE_S = (3.0, 8.0)
ROUTE_NEGOTIATION_SHARE = 0.4
ROUTE_HOLD_S = 0.5
CONGESTION_S = 0.8
# Scene timing presets: None reproduces the ladder's animations frame for frame.
PACING = {
    None: dict(
        global_s=GLOBAL_S, legal_part_s=LEGAL_PART_S, legal_max_s=LEGAL_MAX_S, route_s=ROUTE_S
    ),
    "showcase": dict(
        global_s=(4.0, 6.0),
        legal_part_s=0.12,
        legal_max_s=2.4,
        route_s=(3.0, 6.0),
        flip_half_turns=True,  # single parts too (rigid bodies always flip)
        follow_offboard=True,  # widen the camera over poses that leave the board
    ),
}
ZOOM_S = 0.5  # back to the board after a placement shown with a widened camera
REPLAY_S = 4.0  # a pool's starts replayed in their montage tiles (replay_pool)
REPLAY_LEGAL_S = 0.6
NATIVE_S = {"writeback": 0.6, "planes": 1.2, "refill": 0.4}
NATIVE_EMPTY_S = 0.3
END_S = 2.8
FLASH_FRAMES = 2
RIP_FRAMES = 3


def ease(t):
    """Ease-in-out cubic on [0, 1]."""
    t = min(1.0, max(0.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_angle(a, b, t, flip=False):
    """``a`` to ``b`` along the shorter arc; with ``flip`` a half-turn (which has no shorter
    arc) switches from ``a`` to ``b`` at the middle instead of sweeping."""
    d = (b - a + 180.0) % 360.0 - 180.0
    if flip and abs(abs(d) - 180.0) < 1e-6:
        return (a if t < 0.5 else b) % 360.0
    return (a + d * t) % 360.0


def lerp_poses(a, b, t, flip=False):
    out = {}
    for ref, pb in b.items():
        pa = a.get(ref, pb)
        out[ref] = (
            lerp(pa[0], pb[0], t),
            lerp(pa[1], pb[1], t),
            lerp_angle(pa[2], pb[2], t, flip),
            pb[3] if t >= 0.5 else pa[3],
        )
    for ref, pa in a.items():
        out.setdefault(ref, pa)
    return out


def lerp_rect(a, b, t):
    return tuple(lerp(x, y, t) for x, y in zip(a, b))


def rotate(x, y, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def event_bodies(event):
    """``{body: (x, y, rot, side)}`` of an event's rigid bodies (its ``groups`` rows)."""
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in event.get("groups") or []}


def rigid_offsets(trace, events):
    """``{body: [(ref, dx, dy, drot)]}``: each member's pose in its body's frame, from the
    first event that records the body (members follow their body rigidly)."""
    out = {}
    for event in events:
        members = event.get("group_members") or {}
        if not members:
            continue
        bodies = event_bodies(event)
        poses = None
        for name in sorted(members):
            if name in out or name not in bodies:
                continue
            if poses is None:
                poses = event_poses(trace, event)
            bx, by, br, _side = bodies[name]
            rows = []
            for ref in members[name]:
                if ref not in poses:
                    continue
                x, y, rot, _s = poses[ref]
                dx, dy = rotate(x - bx, y - by, -br)
                rows.append((ref, dx, dy, (rot - br) % 360.0))
            out[name] = rows
    return out


def pose_members(body, offsets):
    """The members' poses under a body pose."""
    x, y, rot, side = body
    out = {}
    for ref, dx, dy, drot in offsets:
        ox, oy = rotate(dx, dy, rot)
        out[ref] = (x + ox, y + oy, (rot + drot) % 360.0, side)
    return out


def lerp_body(a, b, t):
    """A rigid body's pose between two recorded poses: centre linear, angle on the shorter
    arc; a half-turn flips at the middle (the placer records only the snapped angles)."""
    side = b[3] if t >= 0.5 else a[3]
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp_angle(a[2], b[2], t, True), side)


class View:
    """What one frame shows. Copper dicts are shared between frames and never mutated."""

    __slots__ = (
        "poses",
        "committed",
        "provisional",
        "flash",
        "ripped",
        "native",
        "native_mix",
        "zone_reveal",
        "groups",
        "open_pairs",
        "heat",
        "marked",
        "failed",
        "progress",
        "ghost",
        "findings",
        "phase",
        "caption",
        "step",
        "camera",
        "card",
        "montage",
        "blend",
        "bodies",
        "fixed",
    )

    def __init__(self, **fields):
        self.poses = {}
        self.committed = {}
        self.provisional = {}
        self.flash = {}
        self.ripped = {}
        self.native = None
        self.native_mix = 0.0
        self.zone_reveal = 1.0
        self.groups = {}
        self.open_pairs = None
        self.heat = None
        self.marked = ()
        self.failed = False
        self.progress = (0, 0, "none")
        self.ghost = None  # provisional (negotiation) progress, drawn behind the bar
        self.findings = ()  # KiCad DRC finding positions (native frames)
        self.phase = "source"
        self.caption = ""
        self.step = None
        self.camera = (0, 0, 1, 1)
        self.card = None
        self.montage = None
        self.blend = None
        self.bodies = None  # rigid bodies' own poses (line groups, block macros)
        self.fixed = None  # copper a route keeps as it is (a hierarchical knit's blocks)
        for key, value in fields.items():
            setattr(self, key, value)

    def copy(self, **changes):
        view = copy.copy(self)
        for key, value in changes.items():
            setattr(view, key, value)
        return view


def header_poses(header):
    return {c["ref"]: (c["pos"][0], c["pos"][1], c["rot"], c["side"]) for c in header["components"]}


def event_poses(trace, event):
    rows = event.get("poses")
    if rows is None and event.get("poses_blob"):
        rows = trace.blob(event["poses_blob"])
    if rows is None:
        rows = event.get("order") or trace.blob(event.get("order_blob"))
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows or []}


def outline_camera(header):
    w, h = header["outline"]["w"], header["outline"]["h"]
    m = 0.04 * max(w, h)
    return (-m, -m, w + m, h + m)


def bounds_camera(header, poses):
    x0, y0, x1, y1 = outline_camera(header)
    size = {c["ref"]: c["courtyard"] for c in header["components"]}
    for ref, (x, y, _rot, _side) in poses.items():
        half = max(size.get(ref, (0, 0))) / 2.0 + 500
        x0, y0 = min(x0, x - half), min(y0, y - half)
        x1, y1 = max(x1, x + half), max(y1, y + half)
    m = 0.03 * max(x1 - x0, y1 - y0)
    return (x0 - m, y0 - m, x1 + m, y1 + m)


def offboard(header, poses):
    """True when a part's courtyard reaches past the outline camera (it would be cut off or
    not drawn at all)."""
    x0, y0, x1, y1 = outline_camera(header)
    size = {c["ref"]: c["courtyard"] for c in header["components"]}
    for ref, (x, y, _rot, _side) in poses.items():
        half = max(size.get(ref, (0, 0))) / 2.0
        if x - half < x0 or y - half < y0 or x + half > x1 or y + half > y1:
            return True
    return False


def span_camera(header, poses_list):
    """The outline camera, or when any of ``poses_list`` leaves it, a camera over all of them
    (one camera for a whole replay, so the board never jumps)."""
    if not any(offboard(header, poses) for poses in poses_list):
        return outline_camera(header)
    rects = [bounds_camera(header, poses) for poses in poses_list]
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def sample(poses, bodies, offsets, u, flip=False):
    """``(poses, bodies, index)`` at ``u`` in [0, len - 1] along recorded snapshots: parts
    interpolated between the two snapshots around ``u``, rigid bodies as one (members posed from
    the body), ``index`` the nearest snapshot."""
    i = min(len(poses) - 2, int(math.floor(u))) if len(poses) > 1 else 0
    frac = u - i if len(poses) > 1 else 1.0
    j = min(i + 1, len(poses) - 1)
    current = lerp_poses(poses[i], poses[j], frac, flip)
    now = {}
    for name in sorted(offsets):
        a, b = bodies[i].get(name), bodies[j].get(name)
        if a is None or b is None:
            continue
        now[name] = lerp_body(a, b, frac)
        current.update(pose_members(now[name], offsets[name]))
    return current, now, min(len(poses) - 1, int(round(u)))


class Timeline:
    """Frames ``[(View, milliseconds)]`` of a storyboard over its trace."""

    def __init__(
        self,
        trace,
        storyboard,
        frame_ms=60,
        max_seconds=22.0,
        pacing=None,
        replay_pool=False,
        follow_offboard=None,
    ):
        self.trace = trace
        self.board = storyboard
        self.header = trace.header
        self.frame_ms = int(frame_ms)
        self.total = self.header.get("connections_total", 0)
        self.frames = []
        self.marks = []  # [(frame index, scene type)] where each scene begins
        self.base_groups = {}  # pad groups joined before placement (a knit's block copper)
        if pacing not in PACING:
            raise ValueError("unknown pacing %r" % (pacing,))
        self.pacing = pacing
        preset = PACING[pacing]
        self.global_s, self.route_s = preset["global_s"], preset["route_s"]
        self.legal_part_s, self.legal_max_s = preset["legal_part_s"], preset["legal_max_s"]
        self.flip = preset.get("flip_half_turns", False)
        self.follow_offboard = (
            preset.get("follow_offboard", False) if follow_offboard is None else follow_offboard
        )
        self.replay_pool = bool(replay_pool)
        self._cameras = {}
        nominal = sum(self.duration(s) for s in storyboard["scenes"])
        self.scale = min(1.0, float(max_seconds) / nominal) if nominal > 0 else 1.0
        source = header_poses(self.header)
        self.view = View(
            poses=source,
            camera=bounds_camera(self.header, source),
            progress=(0, self.total, "none"),
        )
        scenes = storyboard["scenes"]
        for index, scene in enumerate(scenes):
            following = scenes[index + 1] if index + 1 < len(scenes) else None
            self.marks.append((len(self.frames), scene["type"]))
            getattr(self, "_" + scene["type"].replace("-", "_"))(scene, following)

    # --- durations ----------------------------------------------------------------------
    def duration(self, scene):
        kind = scene["type"]
        if kind == "title":
            return TITLE_S
        if kind == "source":
            return SOURCE_S
        if kind == "montage":
            replay = REPLAY_S + REPLAY_LEGAL_S if self._replayed(scene) else 0.0
            return MONTAGE_IN_S + MONTAGE_HOLD_S + MONTAGE_ZOOM_S + replay
        if kind == "attempts":
            return ATTEMPT_S * len(scene["scopes"])
        if kind == "congestion":
            return CONGESTION_S
        if kind == "placement":
            parts = len(self._legal_units(scene["scope"]))
            zoom = ZOOM_S if self._widened(scene["scope"]) else 0.0
            seconds = self._global_seconds(scene["scope"]) + FLY_IN_S + zoom
            return seconds + self._legal_seconds(parts)
        if kind == "move":
            return MOVE_S
        if kind == "route":
            return self._route_seconds(scene["scope"])
        if kind == "native":
            event = self._native_event(scene)
            zones = event and self.trace.blob(event["copper"]).get("zones")
            if scene["stage"] in ("planes", "refill") and not zones:
                return NATIVE_EMPTY_S
            return NATIVE_S.get(scene["stage"], 0.5)
        if kind == "end":
            return END_S
        return 0.0

    def _placement_camera(self, scope):
        """The camera of a global placement: the outline, or (``follow_offboard``) a camera
        over every recorded snapshot when any of them leaves the board."""
        if scope not in self._cameras:
            camera = outline_camera(self.header)
            if self.follow_offboard:
                snaps = self._snapshots(scope)
                camera = span_camera(self.header, [event_poses(self.trace, e) for e in snaps])
            self._cameras[scope] = camera
        return self._cameras[scope]

    def _widened(self, scope):
        return self._placement_camera(scope) != outline_camera(self.header)

    def _replayed(self, scene):
        """True for a montage whose tiles are starts with global placement snapshots, when
        ``replay_pool`` is set: it replays them before it holds."""
        if not self.replay_pool or not scene.get("tiles"):
            return False
        for tile in scene["tiles"]:
            scope = self.trace.scopes.get(tile["node"])
            if scope is None or scope.type != "start" or len(self._snapshots(tile["node"])) < 2:
                return False
        return True

    def _global_seconds(self, scope):
        snaps = self._snapshots(scope)
        if len(snaps) < 3:
            return 0.3 * len(snaps)  # nothing to interpolate
        return lerp(self.global_s[0], self.global_s[1], min(1.0, len(snaps) / 25.0))

    def _legal_seconds(self, parts):
        return min(self.legal_max_s, self.legal_part_s * parts)

    def _route_events(self, scope):
        return [e for e in self.trace.scopes[scope].events if e["kind"] in ("net", "route_end")]

    def _route_seconds(self, scope):
        n = len(self._route_events(scope))
        share = min(1.0, math.log10(max(n, 10) / 10.0) / 2.0)
        return lerp(self.route_s[0], self.route_s[1], share)

    def frames_for(self, seconds):
        return max(1, int(round(seconds * self.scale * 1000.0 / self.frame_ms)))

    # --- output -------------------------------------------------------------------------
    def emit(self, view=None, ms=None):
        view = view or self.view
        ms = self.frame_ms if ms is None else int(ms)
        self.frames.append((view, max(10, ms)))

    def hold(self, seconds, view=None):
        ms = int(round(seconds * self.scale * 1000.0 / 10.0)) * 10
        self.emit(view, max(self.frame_ms, ms))

    def _decay(self):
        flash = {n: k - 1 for n, k in self.view.flash.items() if k > 1}
        ripped = {n: (c, k - 1) for n, (c, k) in self.view.ripped.items() if k > 1}
        self.view = self.view.copy(flash=flash, ripped=ripped)

    # --- scenes -------------------------------------------------------------------------
    def final_view(self):
        """The end state (the last saved board, else the last route)."""
        boards = [e for e in self.trace.events("native") if e["kind"] == "board"]
        camera = outline_camera(self.header)
        if boards:
            event = boards[-1]
            poses = {r[0]: (r[1], r[2], r[3], r[4]) for r in event.get("poses", [])}
            copper = self.trace.blob(event["copper"])
            return View(
                poses=poses or self.view.poses,
                native=copper,
                native_mix=1.0,
                camera=camera,
                open_pairs=[],
            )
        ends = [e for e in self.trace.events() if e["kind"] == "route_end"]
        if ends:
            event = ends[-1]
            committed = {n: self.trace.blob(d) for n, d in event.get("nets", {}).items()}
            poses = [e for e in self.trace.events() if e["kind"] in ("poses", "legal")]
            last = event_poses(self.trace, poses[-1]) if poses else self.view.poses
            return View(
                poses=last, committed=committed, groups=event.get("groups", {}), camera=camera
            )
        return None

    def _title(self, scene, following):
        # The backdrop is the unplaced board the timeline starts from, never the result.
        card = dict(kind="title", backdrop=self.view)
        self.hold(TITLE_S, self.view.copy(card=card, phase="title"))

    def _source(self, scene, following):
        self.view = self.view.copy(phase="source", card=None)
        self.hold(SOURCE_S)

    def _tile_view(self, tile):
        node = tile["node"]
        scope = self.trace.scopes.get(node)
        base = View(camera=outline_camera(self.header), phase="selection")
        if scope is None:
            return base
        if scope.type == "board":  # a saved board (a coarse run's candidate)
            boards = [e for e in scope.events if e["kind"] == "board"]
            if boards:
                poses = {r[0]: (r[1], r[2], r[3], r[4]) for r in boards[-1].get("poses", [])}
                copper = self.trace.blob(boards[-1]["copper"])
                return base.copy(
                    poses=poses or self._final_poses(node),
                    native=copper,
                    native_mix=1.0,
                    open_pairs=[],
                )
        if scope.type == "route":
            start = scope.meta.get("start")
            poses_scope = node.rsplit("/", 1)[0] + "/" + start if start else scope.parent
            poses = self._final_poses(poses_scope)
            end = [e for e in scope.events if e["kind"] == "route_end"]
            committed = {}
            groups = {}
            if end:
                committed = {n: self.trace.blob(d) for n, d in end[-1]["nets"].items()}
                groups = end[-1].get("groups", {})
            return base.copy(poses=poses, committed=committed, groups=groups)
        return base.copy(poses=self._final_poses(node))

    def _montage(self, scene, following):
        tiles = [dict(t, view=self._tile_view(t)) for t in scene["tiles"]]
        chosen = next((t for t in tiles if t["chosen"]), tiles[-1])
        caption = scene.get("caption") or montage_caption(
            scene["criterion"], scene["among"], scene["selected"]
        )
        grouped = following is not None and following["type"] == "montage"

        def frame(alpha, zoom, shown=None, step=None, text=None):
            montage = dict(
                tiles=shown or tiles,
                alpha=alpha,
                zoom=zoom,
                chosen=chosen["node"],
                criterion=scene["criterion"],
                more=scene["more"],
            )
            return self.view.copy(
                montage=montage, phase="selection", caption=text or caption, step=step
            )

        steps = self.frames_for(MONTAGE_IN_S)
        if self._replayed(scene):
            # Every start's recorded global placement, replayed in its tile, then its
            # legalized placement (one step): the state the tiles then hold.
            replay = self._replay(tiles)
            text = "global placement of %d starts, replayed" % len(tiles)
            first, step = replay(0.0)
            for k in range(steps):
                self.emit(frame(ease((k + 1) / steps), 0.0, first, step, text))
            steps = self.frames_for(REPLAY_S)
            for k in range(steps):
                shown, step = replay(ease((k + 1) / steps))
                self.emit(frame(1.0, 0.0, shown, step, text))
            text = "the %d starts, legalized" % len(tiles)
            self.hold(REPLAY_LEGAL_S, frame(1.0, 0.0, text=text))
        else:
            for k in range(steps):
                self.emit(frame(ease((k + 1) / steps), 0.0))
        self.hold(MONTAGE_HOLD_S, frame(1.0, 0.0))
        steps = self.frames_for(MONTAGE_ZOOM_S)
        if grouped:
            for k in range(steps):
                self.emit(frame(1.0 - ease((k + 1) / steps), 0.0))
            return
        for k in range(steps):
            self.emit(frame(1.0, ease((k + 1) / steps)))
        # From the winner's end state back to the start of its replay.
        target = self.view.copy(caption="", phase=self.view.phase)
        for k in range(2):
            self.emit(target.copy(blend=(chosen["view"], 1.0 - (k + 1) / 3.0)))

    def _replay(self, tiles):
        """A function of ``u`` in [0, 1]: ``(tiles, step)`` with every start tile at that point
        of its recorded global placement (all tiles on one clock, each over its own snapshots;
        a tile whose poses leave the board gets a camera over them)."""
        runs = []
        for tile in tiles:
            snaps = self._snapshots(tile["node"])
            poses = [event_poses(self.trace, e) for e in snaps]
            camera = outline_camera(self.header)
            if self.follow_offboard:
                camera = span_camera(self.header, poses)
            bodies = [event_bodies(e) for e in snaps]
            runs.append((tile, snaps, poses, bodies, rigid_offsets(self.trace, snaps), camera))
        clock = next((r for r in runs if r[0]["chosen"]), runs[0])[1]

        def at(u):
            shown = []
            for tile, snaps, poses, bodies, offsets, camera in runs:
                current, now, _i = sample(poses, bodies, offsets, u * (len(poses) - 1), self.flip)
                view = View(camera=camera, phase="selection", poses=current, bodies=now or None)
                shown.append(dict(tile, view=view))
            event = clock[min(len(clock) - 1, int(round(u * (len(clock) - 1))))]
            return shown, (event.get("iter") or 0, event.get("iters") or 0, "iteration")

        return at

    def _attempts(self, scene, following):
        for sid in scene["scopes"]:
            snaps = self._snapshots(sid)
            poses = event_poses(self.trace, snaps[-1]) if snaps else self.view.poses
            self.view = self.view.copy(
                poses=poses,
                committed={},
                provisional={},
                flash={},
                ripped={},
                groups={},
                native=None,
                native_mix=0.0,
                open_pairs=None,
                findings=(),
                progress=(0, self.total, "none"),
                ghost=None,
                failed=True,
                phase="attempts",
                caption=sid.rsplit("/", 1)[-1],
                camera=outline_camera(self.header),
            )
            self.hold(ATTEMPT_S)
        self.view = self.view.copy(failed=False, caption="")

    def _congestion(self, scene, following):
        events = self.trace.kind(scene["scope"], "congestion")
        if not events:
            return
        event = events[-1]
        marked = tuple(sorted(event.get("inflation", {})))
        steps = self.frames_for(CONGESTION_S * 0.4)
        for k in range(steps):
            heat = dict(event, alpha=ease((k + 1) / steps))
            self.emit(self.view.copy(heat=heat, marked=marked, phase="congestion", step=None))
        heat = dict(event, alpha=1.0)
        self.hold(CONGESTION_S * 0.6, self.view.copy(heat=heat, marked=marked, phase="congestion"))
        self.view = self.view.copy(heat=None, marked=())

    def _snapshots(self, scope):
        snaps = [e for e in self.trace.kind(scope, "poses") if e.get("stage") == "global"]
        return sorted(snaps, key=lambda e: (e.get("iter") or 0, e["seq"]))

    def _legal_order(self, scope):
        legal = self.trace.kind(scope, "legal")
        if not legal:
            return [], {}
        rows = legal[-1].get("order") or self.trace.blob(legal[-1].get("order_blob")) or []
        return [r[0] for r in rows], {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    def _legal_units(self, scope):
        """The legalizer's steps: ``[(body or None, [refs])]``; a rigid body is one step."""
        order, placed = self._legal_order(scope)
        legal = self.trace.kind(scope, "legal")
        members = (legal[-1].get("group_members") or {}) if legal else {}
        owner = {ref: name for name, refs in members.items() for ref in refs}
        units, seen = [], set()
        for ref in order:
            name = owner.get(ref)
            if name is None:
                units.append((None, [ref]))
            elif name not in seen:
                seen.add(name)
                units.append((name, [r for r in members[name] if r in placed]))
        return units

    def _final_poses(self, scope):
        order, legal = self._legal_order(scope)
        if legal:
            return legal
        poses = self.trace.kind(scope, "poses")
        if poses:
            return event_poses(self.trace, poses[-1])
        return header_poses(self.header)

    def _placement(self, scene, following):
        scope = scene["scope"]
        snaps = self._snapshots(scope)
        caption = scene.get("label") or ""
        outline = outline_camera(self.header)
        camera = self._placement_camera(scope)
        self.view = self.view.copy(
            committed={},
            provisional={},
            flash={},
            ripped={},
            native=None,
            native_mix=0.0,
            open_pairs=None,
            findings=(),
            progress=(0, self.total, "none"),
            ghost=None,
            phase="global-placement",
            caption=caption,
            fixed=None,
            groups=dict(self.base_groups),
        )
        start_poses, start_camera = self.view.poses, self.view.camera
        if snaps:
            first = event_poses(self.trace, snaps[0])
            steps = self.frames_for(FLY_IN_S)
            for k in range(steps):
                t = ease((k + 1) / steps)
                self.view = self.view.copy(
                    poses=lerp_poses(start_poses, first, t, self.flip),
                    camera=lerp_rect(start_camera, camera, t),
                    step=None,
                )
                self.emit()
            poses = [event_poses(self.trace, e) for e in snaps]
            bodies = [event_bodies(e) for e in snaps]
            offsets = rigid_offsets(self.trace, snaps)
            steps = self.frames_for(self._global_seconds(scope))
            for k in range(steps):
                u = ease((k + 1) / steps) * (len(poses) - 1)
                current, now, index = sample(poses, bodies, offsets, u, self.flip)
                iteration = snaps[index]
                changes = {}
                if offsets:  # rigid bodies: members posed from the interpolated body pose
                    changes["bodies"] = now
                self.view = self.view.copy(
                    poses=current,
                    step=(iteration.get("iter") or 0, iteration.get("iters") or 0, "iteration"),
                    **changes,
                )
                self.emit()
        order, legal = self._legal_order(scope)
        if legal:
            # A widened camera stays until every part is legal (they come in from off the board).
            self.view = self.view.copy(phase="legalization", camera=camera)
            fixed = {c["ref"] for c in self.header["components"] if c["fixed"]}
            units = self._legal_units(scope)
            if any(name is not None for name, _refs in units):
                self._legal_bodies(scope, units, legal, fixed)
            else:
                moving = [r for r in order if r not in fixed]
                seconds = self._legal_seconds(len(order))
                per = max(
                    self.frame_ms, int(round(seconds * self.scale * 1000.0 / max(1, len(moving))))
                )
                poses = dict(self.view.poses)
                for ref in fixed:
                    if ref in legal:
                        poses[ref] = legal[ref]
                for index, ref in enumerate(moving):
                    poses[ref] = legal[ref]
                    self.view = self.view.copy(
                        poses=dict(poses), marked=(ref,), step=(index + 1, len(moving), "part")
                    )
                    self.emit(ms=per)
            self.view = self.view.copy(poses=dict(legal), marked=(), step=None, phase="placement")
        if self.view.camera != outline and camera != outline:
            steps = self.frames_for(ZOOM_S)
            start = self.view.camera
            for k in range(steps):
                self.emit(self.view.copy(camera=lerp_rect(start, outline, ease((k + 1) / steps))))
        self.view = self.view.copy(camera=outline)

    def _legal_bodies(self, scope, units, legal, fixed):
        """Legalization with rigid bodies: a body (all its members) is one step."""
        moving = [u for u in units if not all(r in fixed for r in u[1])]
        seconds = self._legal_seconds(len(units))
        per = max(self.frame_ms, int(round(seconds * self.scale * 1000.0 / max(1, len(moving)))))
        poses = dict(self.view.poses)
        for ref in fixed:
            if ref in legal:
                poses[ref] = legal[ref]
        final = event_bodies(self.trace.kind(scope, "legal")[-1])
        placed = dict(self.view.bodies or {})
        for index, (name, refs) in enumerate(moving):
            for ref in refs:
                poses[ref] = legal[ref]
            if name is not None and name in final:
                placed[name] = final[name]
            self.view = self.view.copy(
                poses=dict(poses),
                bodies=dict(placed),
                marked=tuple(refs),
                step=(index + 1, len(moving), "step"),
            )
            self.emit(ms=per)
        self.view = self.view.copy(bodies=final)

    def _move(self, scene, following):
        events = self.trace.kind(scene["scope"], "poses")
        if not events:
            return
        target = event_poses(self.trace, events[-1])
        start = self.view.poses
        outline = outline_camera(self.header)
        camera = self.view.camera
        self.view = self.view.copy(
            committed={},
            provisional={},
            flash={},
            ripped={},
            groups={},
            native=None,
            native_mix=0.0,
            open_pairs=None,
            findings=(),
            progress=(0, self.total, "none"),
            ghost=None,
            phase="placement",
            caption=scene.get("label") or "local move",
        )
        steps = self.frames_for(MOVE_S)
        for k in range(steps):
            t = ease((k + 1) / steps)
            self.view = self.view.copy(
                poses=lerp_poses(start, target, t, self.flip),
                camera=lerp_rect(camera, outline, t),
            )
            self.emit()

    def _route(self, scene, following):
        scope = scene["scope"]
        events = self._route_events(scope)
        negotiation = [e for e in events if e["kind"] == "net" and e.get("provisional")]
        rest = [e for e in events if not (e["kind"] == "net" and e.get("provisional"))]
        frames = self.frames_for(self._route_seconds(scope) - ROUTE_HOLD_S)
        share = min(ROUTE_NEGOTIATION_SHARE, len(negotiation) / float(max(1, len(events))))
        nego_frames = int(round(frames * share)) if negotiation else 0
        commit_frames = max(1, frames - nego_frames)
        self.view = self.view.copy(caption=scene.get("label") or "", marked=(), heat=None)
        fixed = self.trace.kind(scope, "fixed")
        if fixed:  # a knit: the block copper is final, the progress starts at its joins
            event = fixed[-1]
            self.view = self.view.copy(
                fixed=self.trace.blob(event["copper"]),
                groups={n: [list(g) for g in gs] for n, gs in (event.get("groups") or {}).items()},
                committed={},
                provisional={},
                progress=_progress(event, self.total),
            )
        counter = [0]

        def play(batch_events, count, phase):
            if not batch_events:
                return
            budget = count * self.frame_ms  # fewer events than frames: longer frames
            count = max(1, min(count, len(batch_events)))
            size = int(math.ceil(len(batch_events) / float(count)))
            batches = int(math.ceil(len(batch_events) / float(size)))
            ms = max(self.frame_ms, int(round(budget / float(batches) / 10.0)) * 10)
            for start in range(0, len(batch_events), size):
                self._decay()
                for event in batch_events[start : start + size]:
                    self._apply(event)
                    counter[0] += 1
                self.view = self.view.copy(
                    phase=phase if phase != "commit" else self._commit_phase(),
                    step=(counter[0], len(events), "event"),
                )
                self.emit(ms=ms)

        play(negotiation, nego_frames, "negotiation")
        play(rest, commit_frames, "commit")
        self._decay()
        self._decay()
        self.view = self.view.copy(phase="routed", flash={}, ripped={}, step=None)
        self.hold(ROUTE_HOLD_S)

    def _commit_phase(self):
        return "rip-up" if self.view.ripped else "commit"

    def _apply(self, event):
        v = self.view
        if event["kind"] == "route_end":
            committed = {n: self.trace.blob(d) for n, d in event.get("nets", {}).items()}
            groups = dict(event.get("groups") or {})
            self.view = v.copy(
                committed=committed,
                provisional={},
                groups=groups,
                progress=_progress(event, self.total),
                ghost=None,
            )
            return
        net, op = event["net"], event["op"]
        copper = self.trace.blob(event.get("copper")) if event.get("copper") else None
        groups = dict(v.groups)
        groups[net] = event.get("groups") or []
        committed, provisional = dict(v.committed), dict(v.provisional)
        flash, ripped = dict(v.flash), dict(v.ripped)
        if event.get("provisional"):
            if op == "add" and copper is not None:
                provisional[net] = copper
            else:
                provisional.pop(net, None)
        elif op == "commit" and copper is not None:
            committed[net] = copper
            provisional.pop(net, None)
            flash[net] = FLASH_FRAMES
            ripped.pop(net, None)
        else:
            old = committed.pop(net, None) or provisional.pop(net, None)
            if old is not None and event.get("op") in ("drop", "rip"):
                ripped[net] = (old, RIP_FRAMES)
        # The bar counts committed connections only: negotiation (overlaps allowed) fills a
        # lighter bar behind it, so the number never runs ahead of the copper.
        if event.get("provisional"):
            progress, ghost = v.progress, _progress(event, self.total)
        else:
            progress, ghost = _progress(event, self.total), None
        self.view = v.copy(
            committed=committed,
            provisional=provisional,
            flash=flash,
            ripped=ripped,
            groups=groups,
            progress=progress,
            ghost=ghost,
        )

    def _native_event(self, scene):
        for event in self.trace.events("native"):
            if event["kind"] == "board" and event["seq"] == scene.get("seq"):
                return event
        return None

    def _native(self, scene, following):
        event = self._native_event(scene)
        if event is None:
            return
        copper = self.trace.blob(event["copper"])
        poses = {r[0]: (r[1], r[2], r[3], r[4]) for r in event.get("poses", [])}
        drc = event.get("drc") or {}
        progress = _progress(event, self.total)
        stage = scene["stage"]
        base = self.view.copy(
            poses=poses or self.view.poses,
            open_pairs=drc.get("open_pairs", []) if drc else None,
            findings=tuple(tuple(f) for f in drc.get("findings") or []) if drc else (),
            progress=progress,
            ghost=None,
            phase=stage if stage in PHASE_TEXT else "native-phase",
            caption="" if stage in PHASE_TEXT else stage,
            step=None,
            flash={},
            ripped={},
            provisional={},
            camera=outline_camera(self.header),
        )
        seconds = self.duration(scene)
        steps = self.frames_for(seconds)
        if self.view.native is None:
            # Engine copper cross-fades to the saved board.
            for k in range(steps):
                self.emit(base.copy(native=copper, native_mix=ease((k + 1) / steps)))
        elif copper.get("zones") and stage == "planes":
            for k in range(steps):
                self.emit(
                    base.copy(native=copper, native_mix=1.0, zone_reveal=ease((k + 1) / steps))
                )
        else:
            self.hold(seconds, base.copy(native=copper, native_mix=1.0))
        self.view = base.copy(native=copper, native_mix=1.0, zone_reveal=1.0)

    def _end(self, scene, following):
        result = dict(scene.get("result") or {})
        # Room below the board for the verdict panel.
        x0, y0, x1, y1 = outline_camera(self.header)
        camera = (x0, y0 - 0.16 * (y1 - y0), x1, y1)
        card = dict(kind="end", result=result, rejected=scene.get("rejected", {}))
        steps = self.frames_for(0.3)
        start = self.view.camera
        for k in range(steps):
            t = ease((k + 1) / steps)
            self.emit(self.view.copy(camera=lerp_rect(start, camera, t), step=None, caption=""))
        self.view = self.view.copy(camera=camera)
        self.hold(END_S - 0.3, self.view.copy(card=card, phase="result", step=None, caption=""))


def _progress(event, total):
    progress = event.get("progress") or {}
    done = progress.get("done", 0)
    return (done, progress.get("total", total) or total, progress.get("source", "router"))


def montage_caption(criterion, among, selected):
    """The caption of a montage: the ladder's fixed texts, else a rung's promotion."""
    if criterion in MONTAGE_TEXT:
        return MONTAGE_TEXT[criterion].format(among=among, selected=selected)
    name = criterion_text(criterion)
    if selected > 1:
        return "%d candidates, %d promoted by %s" % (among, selected, name)
    return "%d candidates, the best by %s chosen" % (among, name)


def montage_score_text(criterion, score):
    """A short display of a candidate's score in its selection's criterion."""
    if score is None:
        return ""
    if criterion == "route-objective" and isinstance(score, list) and len(score) >= 4:
        missing, _nets, vias, length = score[:4]
        text = "%d vias, %.0f mm" % (int(vias), float(length))
        return ("%d open, " % int(missing) + text) if missing else text
    if criterion == "capacity-proxy" and isinstance(score, (int, float)):
        return "proxy %.3g" % score
    if criterion == "missing-connections" and isinstance(score, (int, float)):
        return "%d missing" % score
    if str(criterion).endswith("-objective") and isinstance(score, list) and len(score) >= 6:
        return "%d open" % int(score[-1])  # a halving rung's objective ends with native opens
    if isinstance(score, list) and len(score) == 1 and isinstance(score[0], (int, float)):
        score = score[0]
    if isinstance(score, (int, float)):
        return "%.3g" % score
    return ""


__all__ = ["Timeline", "View", "montage_score_text", "score_key"]
