"""Hierarchical place and route in three chapters (docs/design/constraint-and-hier-animations.md,
section 6.4): a trace with a ``blocks`` event (``regression/hier_case.py``) and its per-template
block traces (``blocks/<template>/``).

Scenes, after the title card and the unplaced board:

``chapter``        1 · Blocks
``block-grid``     one tile per template replaying its chosen trial (global placement,
                   legalization, route) from its block trace, the tiles in step by normalized
                   progress, all at one scale
``block-montage``  the twin template's trials with their rank keys, the chosen one framed
``reuse``          every block instance with its layout (twins share one), without the board
``chapter``        2 · Top level
``lift``           the blocks move onto the board to their first recorded macro poses (a
                   labelled transition: "blocks become macros")
``placement``      the winning seed's macro placement: rigid bodies with their outline and
                   copper, legalization one macro per step
``chapter``        3 · Knitting
``route``          the block copper kept as it is (dimmed); the nets between blocks commit
                   and flash; the progress starts at the connections the blocks already make
``montage``        the top-level seeds with their knit's route score (after the winner's own
                   knit, as the ladder's finalists follow the winner's route)
``native``, ``end``  writeback, planes, refill, KiCad's verdict

Every pose, copper track and score comes from the trace; the lift and the grid layout of the
``reuse`` scene are the only presentation layouts, and they are captioned as such.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from pnr.provenance import critical_path, from_hier, hier_blocks, hier_traces

from . import theme
from .render import Renderer, _fit, font, mix, rgb, safe_text
from .storyboard import SCHEMA, _montage, set_aside, subject
from .timeline import (
    MONTAGE_HOLD_S,
    MONTAGE_IN_S,
    Timeline,
    View,
    ease,
    event_bodies,
    event_poses,
    header_poses,
    lerp_body,
    lerp_poses,
    lerp_rect,
    outline_camera,
    pose_members,
)

CHAPTERS = {
    1: ("1 · Blocks", "each block template is placed and routed on its own board"),
    2: ("2 · Top level", "the blocks become rigid macros and are placed on the board"),
    3: ("3 · Knitting", "the nets between the blocks are routed"),
    4: (
        "4 · Compaction",
        "the gutters close to the copper they hold, then the nets between the blocks route again",
    ),
}
CHAPTER_S = 0.9
GRID_S = 6.0
MONTAGE_TAIL_S = 0.4  # the block montage holds this much longer (then the reuse scene cuts in)
REUSE_S = 1.2
LIFT_S = 0.8
GAP_UM = 2500  # between blocks in the reuse layout


# --- storyboard --------------------------------------------------------------------------
def templates_of(instances):
    """``[(template, [instances])]``, templates in order of their first block's name."""
    groups = {}
    for inst in sorted(instances, key=lambda i: i["block"]):
        groups.setdefault(inst["template"], []).append(inst)
    return sorted(groups.items(), key=lambda kv: kv[1][0]["block"])


def short(block):
    """A block's display name: the last part of its address (``top.bank_a`` -> ``bank_a``)."""
    return str(block).rsplit(".", 1)[-1]


def build(trace, title=None, subtitle=None):
    """The hierarchical storyboard (``kind: hier``) of a loaded trace."""
    dag = from_hier(trace)
    order, competitors, _entry = critical_path(dag, "final")
    index = {n.id: i for i, n in enumerate(order)}
    instances = hier_blocks(trace)
    templates = templates_of(instances)
    info = subject(trace, title, subtitle)
    info.update(blocks=len(instances), templates=len(templates))
    scenes = [dict(type="title"), dict(type="source"), _chapter(1)]
    tiles = []
    for template, insts in templates:
        rep = insts[0]
        tiles.append(
            dict(
                template=template,
                trial=rep["trial"],
                route="%s-%s" % (rep["trial"], rep["block"]),
                blocks=[i["block"] for i in insts],
            )
        )
    scenes.append(dict(type="block-grid", tiles=tiles))
    twins = max(templates, key=lambda kv: (len(kv[1]), len(kv[1][0]["members"])), default=None)
    if twins is not None:
        template, insts = twins
        sid = "block:%s/block-rank" % template
        node = dag.nodes.get(sid)
        if node is not None and node.candidates:
            cands = sorted(node.candidates, key=lambda c: (dag.nodes[c].order, c))
            scenes.append(
                dict(
                    type="block-montage",
                    template=template,
                    block=insts[0]["block"],
                    blocks=[i["block"] for i in insts],
                    criterion="block-rank",
                    among=len(cands),
                    tiles=[
                        dict(
                            trial=dag.nodes[c].scope,
                            score=node.scores.get(c),
                            chosen=c == node.chosen,
                        )
                        for c in cands
                    ],
                )
            )
    scenes.append(dict(type="reuse"))
    scenes.append(_chapter(2))
    top = next((n for n in order if n.stage == "place" and n.scope), None)
    route = next((n for n in order if n.stage == "route" and n.scope), None)
    if top is not None:
        scenes.append(dict(type="lift", scope=top.scope))
        scenes.append(dict(type="placement", scope=top.scope, label=top.scope))
        if any(e.get("stage") == "hull-nest" for e in trace.kind(top.scope, "poses")):
            # PNR_HULL_NEST: the legalized blocks slide into each other's notches.
            scenes.append(
                dict(type="move", scope=top.scope, label="blocks nest into each other's notches")
            )
    scenes.append(_chapter(3))
    if route is not None:
        scenes.append(dict(type="route", scope=route.scope, label=route.scope))
    seed = dag.nodes.get("top-seed")
    if seed is not None and competitors.get("top-seed"):
        montage = _montage(dag, seed, competitors["top-seed"], trace)
        montage["caption"] = "%d top-level placements knitted, the best route chosen" % len(
            seed.candidates
        )
        scenes.append(montage)
    steps = compaction_steps(trace)
    if steps:
        scenes.append(_chapter(4))
        for stage, route_scope, caption in steps:
            scenes.append(dict(type="move", scope=stage, label=caption))
            scenes.append(dict(type="route", scope=route_scope, label=route_scope))
    for node in order:
        if node.id.startswith("native:"):
            scenes.append(dict(type="native", stage=node.label, seq=node.meta.get("event")))
    result = trace.results[-1] if trace.results else {}
    scenes.append(
        dict(
            type="end",
            rejected=set_aside(order, index),
            result={
                k: result.get(k)
                for k in ("passed", "opens", "violations", "rules", "vias", "copper_length_mm")
            },
        )
    )
    return dict(
        schema=SCHEMA,
        kind="hier",
        subject=info,
        coarse=False,
        path=[n.id for n in order],
        scenes=scenes,
    )


def compaction_steps(trace):
    """``[(stage scope, route scope, caption)]``: the route-then-compact steps the driver kept
    (``PNR_ROUTE_COMPACT`` TOP, the ``route-compact`` selection), in order; [] without."""
    out = []
    for event in trace.selects:
        if event["id"] != "route-compact":
            continue
        chosen = event.get("chosen")
        for route in [chosen] if isinstance(chosen, str) else list(chosen or []):
            scope = trace.scopes.get(route)
            stage = scope and trace.scopes.get(scope.meta.get("start"))
            if stage is None or not trace.kind(stage.id, "poses"):
                continue
            m = stage.meta
            caption = "%s: %.1f -> %.1f mm %s, each gutter sized by its copper" % (
                m.get("axis", "?"),
                m.get("span_before_mm") or 0.0,
                m.get("span_after_mm") or 0.0,
                "wide" if m.get("axis") == "x" else "tall",
            )
            out.append((stage.id, route, caption))
    return out


def _chapter(number):
    heading, text = CHAPTERS[number]
    return dict(type="chapter", number=number, heading=heading, text=text)


def block_rank_text(score):
    """A trial's rank key (:func:`pnr.hier.synth.rank_key`: missing, unmatched lengths, port
    debt, area, vias, copper; a trace from before the unmatched-lengths term has five) as a
    short text."""
    if not isinstance(score, list) or len(score) < 5:
        return ""
    if len(score) >= 6:
        missing, unmatched, debt, area, vias = score[:5]
    else:
        (missing, debt, area, vias), unmatched = score[:4], 0
    text = "debt %.0f · %.0f sq mm · %d vias" % (float(debt), float(area), int(vias))
    if unmatched:
        text = "%d unmatched · " % int(unmatched) + text
    return ("%d open · " % int(missing) + text) if missing else text


# --- timeline ----------------------------------------------------------------------------
class _Scoped:
    """A trace with another header (a block trial's outline)."""

    def __init__(self, trace, header):
        self._trace = trace
        self.header = header

    def __getattr__(self, name):
        return getattr(self._trace, name)


def trial_outline(sub, trial):
    outline = sub.scopes[trial].meta.get("outline") if trial in sub.scopes else None
    if not outline:
        outline = [sub.header["outline"]["w"], sub.header["outline"]["h"]]
    return int(outline[0]), int(outline[1])


def trial_header(sub, trial):
    w, h = trial_outline(sub, trial)
    return dict(sub.header, outline=dict(w=w, h=h, polygon=None))


def _at_fraction(frames, fraction):
    total = sum(ms for _v, ms in frames)
    t = fraction * total
    elapsed = 0
    for view, ms in frames:
        if t < elapsed + ms:
            return view
        elapsed += ms
    return frames[-1][0]


class HierTimeline(Timeline):
    """The timeline of a hierarchical storyboard (the base scenes plus the chapters)."""

    def __init__(self, trace, storyboard, frame_ms=60, max_seconds=34.0, pacing=None):
        self.instances = hier_blocks(trace)
        self.subs = hier_traces(trace)
        self.by_macro = {i["macro"]: i for i in self.instances}
        self._joined = {}
        for scene in storyboard["scenes"]:
            if scene["type"] == "route":
                fixed = trace.kind(scene["scope"], "fixed")
                if fixed:
                    groups = fixed[-1].get("groups") or {}
                    self._joined = {n: [list(g) for g in gs] for n, gs in groups.items()}
        super().__init__(trace, storyboard, frame_ms, max_seconds, pacing)

    def duration(self, scene):
        kind = scene["type"]
        if kind == "chapter":
            return CHAPTER_S
        if kind == "block-grid":
            return GRID_S
        if kind == "block-montage":
            return MONTAGE_IN_S + MONTAGE_HOLD_S + MONTAGE_TAIL_S
        if kind == "reuse":
            return REUSE_S
        if kind == "lift":
            return LIFT_S
        return super().duration(scene)

    # The blocks' own offsets: member pose in the block frame about the block's centre.
    def offsets(self, inst):
        w, h = inst["size_um"]
        return [
            (ref, x - w / 2.0, y - h / 2.0, rot)
            for ref, (x, y, rot, _side) in sorted(inst["members"].items())
        ]

    def _chapter(self, scene, following):
        keep = self.view.card if (self.view.card or {}).get("kind") == "reuse" else None
        card = dict(
            kind="chapter",
            heading=scene["heading"],
            text=scene["text"],
            backdrop=self.view.copy(card=keep, montage=None),
        )
        self.hold(CHAPTER_S, self.view.copy(card=card, phase="chapter", caption="", step=None))

    def _block_grid(self, scene, following):
        runs = []
        for spec in scene["tiles"]:
            sub = self.subs[spec["template"]]
            proxy = _Scoped(sub, trial_header(sub, spec["trial"]))
            board = dict(
                scenes=[
                    dict(type="placement", scope=spec["trial"], label=""),
                    dict(type="route", scope=spec["route"], label=""),
                ]
            )
            # One common scale across the grid: the tiles keep their outline cameras.
            timeline = Timeline(
                proxy,
                board,
                frame_ms=self.frame_ms,
                max_seconds=1e9,
                pacing=self.pacing,
                follow_offboard=False,
            )
            runs.append((spec, timeline.frames))
        steps = self.frames_for(GRID_S)
        caption = "%d templates, each on its own board" % len(runs)
        for k in range(steps):
            fraction = (k + 1) / float(steps)
            tiles = []
            for spec, frames in runs:
                view = _at_fraction(frames, fraction)
                names = ", ".join(short(b) for b in spec["blocks"])
                tiles.append(
                    dict(
                        template=spec["template"],
                        outline=trial_outline(self.subs[spec["template"]], spec["trial"]),
                        view=view,
                        label="%s · trial %s" % (names, spec["trial"]),
                        sub=_tile_status(view),
                        lit=True,
                        chosen=False,
                    )
                )
            card = dict(kind="grid", tiles=tiles, alpha=1.0)
            self.emit(self.view.copy(card=card, phase="blocks", caption=caption, step=None))

    def _trial_view(self, sub, trial, block):
        order = sub.kind(trial, "legal")
        poses = {}
        if order:
            rows = order[-1].get("order") or sub.blob(order[-1].get("order_blob")) or []
            poses = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
        elif sub.kind(trial, "poses"):
            poses = event_poses(sub, sub.kind(trial, "poses")[-1])
        route = "%s-%s" % (trial, block)
        committed, groups = {}, {}
        ends = sub.kind(route, "route_end")
        if ends:
            committed = {n: sub.blob(d) for n, d in ends[-1].get("nets", {}).items()}
            groups = dict(ends[-1].get("groups") or {})
        header = trial_header(sub, trial)
        return View(poses=poses, committed=committed, groups=groups, camera=outline_camera(header))

    def _block_montage(self, scene, following):
        sub = self.subs[scene["template"]]
        names = ", ".join(short(b) for b in scene["blocks"])
        tiles = []
        for tile in scene["tiles"]:
            tiles.append(
                dict(
                    template=scene["template"],
                    outline=trial_outline(sub, tile["trial"]),
                    view=self._trial_view(sub, tile["trial"], scene["block"]),
                    label=("chosen · " if tile["chosen"] else "") + tile["trial"],
                    sub=block_rank_text(tile["score"]),
                    lit=tile["chosen"],
                    chosen=tile["chosen"],
                )
            )
        caption = "%s: %d trial layouts, the best by rank chosen" % (names, scene["among"])

        def frame(alpha):
            card = dict(kind="grid", tiles=tiles, alpha=alpha, legend=RANK_LEGEND)
            return self.view.copy(card=card, phase="selection", caption=caption, step=None)

        steps = self.frames_for(MONTAGE_IN_S)
        for k in range(steps):
            self.emit(frame(ease((k + 1) / steps)))
        self.hold(MONTAGE_HOLD_S + MONTAGE_TAIL_S, frame(1.0))

    def reuse_layout(self):
        """``({macro: pose}, camera)``: the block instances side by side, twins first, rows
        filled up to the board's width (a presentation layout, not a placement)."""
        width = self.header["outline"]["w"]
        rows, row, used = [], [], 0
        for _template, insts in templates_of(self.instances):
            for inst in insts:
                w = inst["size_um"][0]
                if row and used + GAP_UM + w > width:
                    rows.append(row)
                    row, used = [], 0
                used += (GAP_UM if row else 0) + w
                row.append(inst)
        if row:
            rows.append(row)
        heights = [max(i["size_um"][1] for i in r) for r in rows]
        total_h = sum(heights) + GAP_UM * (len(rows) - 1)
        bodies = {}
        y = self.header["outline"]["h"] / 2.0 + total_h / 2.0
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for r, height in zip(rows, heights):
            row_w = sum(i["size_um"][0] for i in r) + GAP_UM * (len(r) - 1)
            x = width / 2.0 - row_w / 2.0
            for inst in r:
                w, h = inst["size_um"]
                bodies[inst["macro"]] = (x + w / 2.0, y - height / 2.0, 0.0, "top")
                x0, x1 = min(x0, x), max(x1, x + w)
                y0, y1 = min(y0, y - height / 2.0 - h / 2.0), max(y1, y - height / 2.0 + h / 2.0)
                x += w + GAP_UM
            y -= height + GAP_UM
        m = 0.12 * max(x1 - x0, y1 - y0)
        return bodies, (x0 - m, y0 - m, x1 + m, y1 + m)

    def _posed(self, bodies):
        poses = {}
        for macro, pose in bodies.items():
            inst = self.by_macro.get(macro)
            if inst is not None:
                poses.update(pose_members(pose, self.offsets(inst)))
        return poses

    def _reuse(self, scene, following):
        # From here on the ratsnest counts the joins the block copper makes (the fixed event
        # of the winner's knit: the same block copper for every seed).
        self.base_groups = self._joined
        bodies, camera = self.reuse_layout()
        twins = [insts for _t, insts in templates_of(self.instances) if len(insts) > 1]
        if twins:
            names = [short(i["block"]) for i in twins[0]]
            caption = "%s: one layout, %d instances" % (" and ".join(names), len(names))
        else:
            caption = "every block with its own layout"
        self.view = self.view.copy(
            poses=self._posed(bodies),
            bodies=bodies,
            camera=camera,
            card=dict(kind="reuse"),
            committed={},
            provisional={},
            groups=dict(self.base_groups),
            fixed=None,
            phase="reuse",
            caption=caption,
            step=None,
            progress=(0, self.total, "none"),
        )
        self.hold(REUSE_S)

    def _lift(self, scene, following):
        snaps = self._snapshots(scene["scope"])
        if not snaps:
            return
        first = snaps[0]
        target_bodies = event_bodies(first)
        target = event_poses(self.trace, first)
        start_bodies = dict(self.view.bodies or {})
        source = header_poses(self.header)
        start_camera = self.view.camera
        outline = outline_camera(self.header)
        steps = self.frames_for(LIFT_S)
        loose = [r for r in target if not any(r in i["members"] for i in self.instances)]
        for k in range(steps):
            t = ease((k + 1) / steps)
            bodies = {
                m: lerp_body(start_bodies.get(m, target_bodies[m]), target_bodies[m], t)
                for m in sorted(target_bodies)
            }
            poses = self._posed(bodies)
            poses.update(
                lerp_poses(
                    {r: source[r] for r in loose}, {r: target[r] for r in loose}, t, self.flip
                )
            )
            self.view = self.view.copy(
                poses=poses,
                bodies=bodies,
                camera=lerp_rect(start_camera, outline, t),
                card=None,
                phase="lift",
                caption="",
            )
            self.emit()
        self.view = self.view.copy(poses=target, bodies=target_bodies)

    def _tile_view(self, tile):
        node = tile["node"]
        scope = self.trace.scopes.get(node)
        if scope is None or scope.type != "route" or "/" in node:
            return super()._tile_view(tile)
        start = scope.meta.get("start")
        poses = self._final_poses(start)
        legal = self.trace.kind(start, "legal")
        bodies = event_bodies(legal[-1]) if legal else {}
        ends = [e for e in scope.events if e["kind"] == "route_end"]
        committed, groups = {}, {}
        if ends:
            committed = {n: self.trace.blob(d) for n, d in ends[-1]["nets"].items()}
            groups = ends[-1].get("groups", {})
        fixed = self.trace.kind(node, "fixed")
        return View(
            camera=outline_camera(self.header),
            phase="selection",
            poses=poses,
            committed=committed,
            groups=groups,
            bodies=bodies,
            fixed=self.trace.blob(fixed[-1]["copper"]) if fixed else None,
        )


RANK_LEGEND = "rank: opens, port debt, area, vias, copper"


def _tile_status(view):
    phase = theme.PHASE_TEXT.get(view.phase, "")
    done, total, _source = view.progress
    if view.committed or view.phase in ("negotiation", "commit", "rip-up", "routed"):
        return "%s · %d%% routed" % (phase, int(100 * done / float(total)) if total else 0)
    return phase


# --- renderer ----------------------------------------------------------------------------
class HierRenderer(Renderer):
    """The board renderer of a hierarchical case: block macros with their outline and copper,
    chapter cards and grids of block tiles."""

    def __init__(self, header, subject_, width, instances, subs, copper):
        super().__init__(header, subject_, width=width)
        self.instances = {i["macro"]: i for i in instances}
        self.subs = subs
        self.block_copper = copper
        templates = len({i["template"] for i in instances})
        self.stats = safe_text(
            self.stats + " · %d blocks, %d templates" % (len(instances), templates)
        )
        self.strings.add(self.stats)
        self._tiles = {}
        self._moved = {}
        self._bare = False

    # Frames.
    def _compose(self, view):
        card = view.card or {}
        kind = card.get("kind")
        if kind not in ("chapter", "grid"):
            return super()._compose(view)
        image = Image.new("RGB", (self.width, self.height), rgb(theme.BACKGROUND))
        x, y, w, h = self.board_box
        if kind == "chapter":
            backdrop = card.get("backdrop")
            if backdrop is not None:
                board = self.board(backdrop, w, h, labels=False)
                dim = Image.new("RGB", board.size, rgb(theme.BACKGROUND))
                image.paste(Image.blend(dim, board, 0.14), (x, y))
            draw = ImageDraw.Draw(image)
            cx, cy = self.width / 2.0, y + h / 2.0
            heading, text = safe_text(card["heading"]), safe_text(card["text"])
            self.strings.update((heading, text))
            draw.text((cx, cy - 16), heading, fill=rgb(theme.TEXT), font=font(30), anchor="mm")
            draw.text((cx, cy + 22), text, fill=rgb(theme.MUTED), font=font(15), anchor="mm")
            return image
        image.paste(self._grid(card, w, h), (x, y))
        return image

    def _grid(self, card, width, height):
        """Tiles in rows (at most four per row), each as wide as its board at one common scale,
        so blocks keep their relative sizes; a label and a status line under each."""
        tiles = card["tiles"]
        alpha = max(0.0, min(1.0, card.get("alpha", 1.0)))
        cols = min(4, len(tiles))
        rows = [tiles[i : i + cols] for i in range(0, len(tiles), cols)]
        gap, label = 16, 36
        legend = card.get("legend")
        top_room = 22 if legend else 0
        pad = 1.08  # the tile camera's margin around the outline
        scale = min(
            (width - gap * (len(row) + 1)) / sum(t["outline"][0] * pad for t in row) for row in rows
        )
        heights = [max(t["outline"][1] * pad for t in row) for row in rows]
        room = height - top_room - gap * (len(rows) + 1) - label * len(rows)
        scale = min(scale, room / sum(heights))
        used = sum(h * scale for h in heights) + label * len(rows) + gap * (len(rows) - 1)
        y = top_room + (height - top_room - used) / 2.0
        image = Image.new("RGB", (width, height), rgb(theme.BACKGROUND))
        draw = ImageDraw.Draw(image)
        if legend:
            draw.text(
                (width / 2.0, 12),
                legend,
                fill=mix(theme.BACKGROUND, theme.MUTED, alpha),
                font=font(12),
                anchor="mm",
            )
            self.strings.add(legend)
        for row, row_h in zip(rows, heights):
            sizes = [
                (int(t["outline"][0] * pad * scale), int(t["outline"][1] * pad * scale))
                for t in row
            ]
            row_w = sum(w for w, _h in sizes) + gap * (len(row) - 1)
            x = (width - row_w) / 2.0
            for tile, (bw, bh) in zip(row, sizes):
                bx, by = int(x), int(y + (row_h * scale - bh) / 2.0)
                self._tile(draw, image, tile, bx, by, max(8, bw), max(8, bh), alpha, gap)
                x += bw + gap
            y += row_h * scale + label + gap
        return image

    def _tile(self, draw, image, tile, bx, by, bw, bh, alpha, gap):
        renderer = self._tile_renderer(tile["template"], tile["outline"])
        picture = renderer.board(tile["view"], bw, bh, labels=bw >= 260)
        fade = alpha * (1.0 if tile.get("lit", True) else 0.45)
        if fade < 1.0:
            back = Image.new("RGB", picture.size, rgb(theme.BACKGROUND))
            picture = Image.blend(back, picture, fade)
        image.paste(picture, (bx, by))
        tone = theme.ACCENT if tile.get("chosen") else theme.TEXT
        if tile.get("chosen"):
            draw.rectangle(
                (bx - 2, by - 2, bx + bw + 1, by + bh + 1),
                outline=mix(theme.BACKGROUND, theme.ACCENT, alpha),
                width=2,
            )
        cx, room = bx + bw / 2.0, bw + gap - 4
        size, text = _fit(draw, safe_text(tile["label"]), room, (13, 12, 11, 10))
        shade = alpha if tile.get("chosen") else fade
        draw.text(
            (cx, by + bh + 6),
            text,
            fill=mix(theme.BACKGROUND, tone, shade),
            font=font(size),
            anchor="ma",
        )
        self.strings.add(text)
        if tile.get("sub"):
            size, text = _fit(draw, safe_text(tile["sub"]), room, (11, 10, 9))
            draw.text(
                (cx, by + bh + 21),
                text,
                fill=mix(theme.BACKGROUND, theme.MUTED, alpha),
                font=font(size),
                anchor="ma",
            )
            self.strings.add(text)

    def _tile_renderer(self, template, outline):
        key = (template, tuple(outline))
        if key not in self._tiles:
            sub = self.subs[template]
            header = dict(sub.header, outline=dict(w=outline[0], h=outline[1], polygon=None))
            parts = len(header["components"])
            nets = len([n for n in header["nets"] if len(n["pins"]) >= 2])
            info = dict(
                title="",
                description="",
                parts=parts,
                nets=nets,
                layers=len(header["copper_layers"]),
            )
            self._tiles[key] = Renderer(header, info, width=max(64, int(outline[0] / 50)))
        return self._tiles[key]

    # Boards.
    def board(self, view, width, height, ss=theme.SUPERSAMPLE, labels=True):
        bare = view.card is not None and view.card.get("kind") == "reuse"
        previous, self._bare = self._bare, bare
        try:
            return super().board(view, width, height, ss, labels)
        finally:
            self._bare = previous

    def _substrate(self, draw, tf):
        if not self._bare:
            super()._substrate(draw, tf)

    def _outline(self, draw, tf, ss):
        if not self._bare:
            super()._outline(draw, tf, ss)

    def _ratsnest(self, draw, tf, view, ss):
        if not self._bare:  # the reuse layout is no placement: no connections to show
            super()._ratsnest(draw, tf, view, ss)

    def _engine_copper(self, view):
        layers = Renderer._engine_copper(view)
        if view.bodies and view.fixed is None and not view.committed and not view.provisional:
            nets = {}
            for macro in sorted(view.bodies):
                if macro in self.block_copper:
                    nets[macro] = self.moved_copper(macro, view.bodies[macro])
            if nets:
                style = "solid" if self._bare else "fixed"
                layers = [(nets, {m: style for m in nets})] + layers
        return layers

    def moved_copper(self, macro, pose):
        """A block's copper (block frame) under a macro pose (board frame)."""
        key = (macro, tuple(round(v, 1) if isinstance(v, float) else v for v in pose))
        if key in self._moved:
            return self._moved[key]
        inst = self.instances[macro]
        w, h = inst["size_um"]
        x, y, rot, _side = pose
        a = math.radians(rot)
        c, s = math.cos(a), math.sin(a)

        def move(px, py):
            dx, dy = px - w / 2.0, py - h / 2.0
            return x + dx * c - dy * s, y + dx * s + dy * c

        blob = self.block_copper[macro]
        tracks = []
        for layer, x0, y0, x1, y1, width in blob.get("tracks", []):
            a0, a1 = move(x0, y0), move(x1, y1)
            tracks.append([layer, a0[0], a0[1], a1[0], a1[1], width])
        vias = []
        for vx, vy, diameter, drill in blob.get("vias", []):
            p = move(vx, vy)
            vias.append([p[0], p[1], diameter, drill])
        if len(self._moved) > 256:
            self._moved.clear()
        self._moved[key] = dict(tracks=tracks, vias=vias, zones=[])
        return self._moved[key]

    def _decorate(self, image, draw, tf, view, ss):
        """Dashed block outlines (and names, where legible) posed with their macros."""
        if not view.bodies:
            return
        color = rgb(theme.BLOCK_OUTLINE)
        size = min(14 * ss, tf.length(1100))
        for macro in sorted(view.bodies):
            inst = self.instances.get(macro)
            if inst is None:
                continue
            x, y, rot, _side = view.bodies[macro]
            w, h = inst["size_um"]
            a = math.radians(rot)
            c, s = math.cos(a), math.sin(a)
            corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
            points = [tf(x + u * c - v * s, y + u * s + v * c) for u, v in corners]
            for p0, p1 in zip(points, points[1:] + points[:1]):
                self._dashed(draw, p0, p1, max(1, int(1.5 * ss)), color, tf, ss)
            if size >= 8 * ss:
                name = safe_text(short(inst["block"]))
                self.strings.add(name)
                top = min(points, key=lambda p: (p[1], p[0]))
                left = min(p[0] for p in points)
                draw.text(
                    (left + 3 * ss, top[1] - 2 * ss),
                    name,
                    fill=color,
                    font=font(size),
                    anchor="lb",
                )


def make(trace, board, width, frame_ms, max_seconds, pacing):
    """``(renderer, frames)`` of a hierarchical storyboard, for :func:`pnr.animate.encode.encode`."""
    instances = hier_blocks(trace)
    copper = {i["macro"]: trace.blob(i["copper"]) for i in instances}
    renderer = HierRenderer(
        trace.header, board["subject"], width, instances, hier_traces(trace), copper
    )
    frames = HierTimeline(
        trace, board, frame_ms=frame_ms, max_seconds=max_seconds, pacing=pacing
    ).frames
    return renderer, frames
