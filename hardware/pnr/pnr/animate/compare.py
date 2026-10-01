"""Side-by-side comparisons of two traced runs: ``python -m pnr.animate --compare LEFT RIGHT``
(docs/design/constraint-and-hier-animations.md, section 6.3).

Each half is its own run's critical path (:mod:`.storyboard`, :mod:`.timeline`), drawn at half
the width by a :class:`PanelRenderer`: a caption strip (label, constraint legend, a live metric
computed from the recorded poses), the board and the run's own footer. One title strip spans
both.

**Synchronization.** Each timeline records where its scenes begin (``Timeline.marks``). When
both runs have the same sequence of scenes (the usual case: the same pool, the same stages),
each scene lasts as long as the longer run's; otherwise scenes map to five phases (intro: title
and source; placement: everything before the first route; routing: from the first route to the
first native stage; native; end) and each phase lasts as long as the longer run's. Either way
the shorter run holds its last frame of that segment. Both runs are then resampled onto one
clock (the frame interval) and runs of identical pairs are merged. Nothing is invented: every
panel frame is a frame of its own run's timeline.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw

from . import encode, highlight, storyboard, theme
from .render import Renderer, _fit, _wrap, font, mix, rgb, safe_text
from .timeline import Timeline

PHASES = ("intro", "placement", "routing", "native", "end")
CAPTION_PX = 46
DEFAULT_LABELS = ("Unconstrained", "Constrained")


# --- synchronization ---------------------------------------------------------------------
def scene_phases(marks):
    """``[(phase, first frame)]`` of a timeline's scene marks (consecutive scenes of one phase
    merged)."""
    out = []
    routed = False
    for start, kind in marks:
        if kind in ("title", "source"):
            phase = "intro"
        elif kind == "route":
            routed, phase = True, "routing"
        elif kind == "native":
            phase = "native"
        elif kind == "end":
            phase = "end"
        elif out and out[-1][0] in ("native", "end"):
            phase = out[-1][0]
        else:
            phase = "routing" if routed else "placement"
        if out and out[-1][0] == phase:
            continue
        out.append((phase, start))
    return out


def split(frames, marks, by_scene=False):
    """``{key: [(view, ms)]}`` of a timeline's frames: by phase, or by scene index."""
    if by_scene:
        bounds = [(index, start) for index, (start, _kind) in enumerate(marks)]
    else:
        bounds = scene_phases(marks)
    out = {}
    for index, (key, start) in enumerate(bounds):
        end = bounds[index + 1][1] if index + 1 < len(bounds) else len(frames)
        out.setdefault(key, []).extend(frames[start:end])
    return out


def _hold(frames, total, last):
    """``frames`` stretched to ``total`` ms by holding its last frame (or ``last``)."""
    have = sum(ms for _v, ms in frames)
    if have >= total:
        return list(frames)
    tail = frames[-1][0] if frames else last
    return list(frames) + [(tail, total - have)]


def _at(frames, t):
    """The view on screen at ``t`` ms (the frame that began at or before it)."""
    elapsed = 0
    for view, ms in frames:
        if t < elapsed + ms:
            return view
        elapsed += ms
    return frames[-1][0]


def synchronize(left, left_marks, right, right_marks, clock_ms=60):
    """Pairs ``[((left view, right view), ms)]``: both runs scene by scene (same scene
    sequence) or phase by phase, on one clock."""
    by_scene = [k for _s, k in left_marks] == [k for _s, k in right_marks]
    a = split(left, left_marks, by_scene)
    b = split(right, right_marks, by_scene)
    keys = range(len(left_marks)) if by_scene else PHASES
    last_a = left[0][0] if left else None
    last_b = right[0][0] if right else None
    pairs = []
    for key in keys:
        fa, fb = a.get(key, []), b.get(key, [])
        if not fa and not fb:
            continue
        total = max(sum(ms for _v, ms in fa), sum(ms for _v, ms in fb))
        fa, fb = _hold(fa, total, last_a), _hold(fb, total, last_b)
        last_a, last_b = fa[-1][0], fb[-1][0]
        slots = int(total // clock_ms)
        for k in range(slots):
            pairs.append(((_at(fa, k * clock_ms), _at(fb, k * clock_ms)), clock_ms))
        rest = total - slots * clock_ms
        if rest >= 10:
            t = slots * clock_ms
            pairs.append(((_at(fa, t), _at(fb, t)), int(rest)))
    merged = []
    for pair, ms in pairs:
        if merged and merged[-1][0][0] is pair[0] and merged[-1][0][1] is pair[1]:
            merged[-1] = (merged[-1][0], merged[-1][1] + ms)
        else:
            merged.append((pair, ms))
    return merged


# --- drawing -----------------------------------------------------------------------------
class PanelRenderer(Renderer):
    """One half of a comparison: compact montage captions, a short title card, an end card
    with the half's own metrics."""

    def __init__(self, header, subject, width, reference=None):
        super().__init__(header, subject, width=width)
        self.compact = True
        self.reference = list(reference or [])

    def metric(self, view):
        """The live metric line (from the frame's poses; empty on the title card)."""
        if view.card is not None and view.card.get("kind") == "title":
            return ""
        constraints = self.constraints or self.reference
        return highlight.metrics(
            self.header,
            self.components,
            self.pins,
            self.pin_xy(view.poses),
            view.poses,
            constraints,
            reference=not self.constraints,
        )

    def _footer(self, draw, view, top):
        """The footer of a half: phase and experiment, "% routed" bar, and the step counter
        only while there is one (so a montage's caption has the room)."""
        footer = theme.FOOTER_PX
        draw.rectangle((0, top, self.width, top + footer), fill=rgb(theme.BACKGROUND))
        phase = theme.PHASE_TEXT.get(view.phase, "")
        if view.caption:
            phase = (phase + " · " if phase else "") + safe_text(view.caption)
        done, total, source = view.progress
        fraction = 0.0 if not total else max(0.0, min(1.0, done / float(total)))
        step = ""
        if view.step is not None:
            i, n, unit = view.step
            step = "%s %d/%d" % (unit, i, n) if n else ""
        bar_w, bar_h = 64, 8
        reserve = draw.textlength(step, font=font(10)) + 10 if step else 0
        bx = self.width - 12 - reserve - bar_w
        label = "%d%%" % int(math.floor(100 * fraction + 1e-9))
        room = bx - 8 - draw.textlength(label, font=font(12)) - 12 - 12
        size, text = _fit(draw, phase, room, (12, 11, 10))
        self.strings.add(text)
        draw.text((12, top + footer / 2), text, fill=rgb(theme.TEXT), font=font(size), anchor="lm")
        by = top + (footer - bar_h) // 2
        draw.rounded_rectangle(
            (bx, by, bx + bar_w, by + bar_h), radius=4, fill=mix(theme.BACKGROUND, theme.MUTED, 0.3)
        )
        color = theme.PROVISIONAL if source == "router-provisional" else theme.ACCENT
        if view.phase == "result" and done < total:
            color = theme.FAIL
        if view.ghost is not None:
            g_done, g_total, _source = view.ghost
            ghost = 0.0 if not g_total else max(0.0, min(1.0, g_done / float(g_total)))
            if ghost > fraction:
                draw.rounded_rectangle(
                    (bx, by, bx + max(bar_h, bar_w * ghost), by + bar_h),
                    radius=4,
                    fill=mix(theme.BACKGROUND, theme.PROVISIONAL, 0.45),
                )
        if fraction > 0:
            draw.rounded_rectangle(
                (bx, by, bx + max(bar_h, bar_w * fraction), by + bar_h), radius=4, fill=rgb(color)
            )
        draw.text(
            (bx - 8, top + footer / 2), label, fill=rgb(theme.TEXT), font=font(12), anchor="rm"
        )
        if step:
            draw.text(
                (self.width - 12, top + footer / 2),
                step,
                fill=rgb(theme.MUTED),
                font=font(10),
                anchor="rm",
            )

    def _title_card(self, image):
        draw = ImageDraw.Draw(image)
        x, y, w, h = self.board_box
        cx, cy = self.width / 2.0, y + h / 2.0
        size, title = _fit(draw, self.title, w - 24, (24, 20, 18, 16))
        draw.text((cx, cy - 34), title, fill=rgb(theme.TEXT), font=font(size), anchor="mm")
        lines = _wrap(self.description, max(30, int(w / 8.2)))
        for i, line in enumerate(lines[:3]):
            draw.text(
                (cx, cy + 2 + 18 * i), line, fill=rgb(theme.MUTED), font=font(13), anchor="mm"
            )
        draw.text(
            (cx, cy + 14 + 18 * min(3, len(lines))),
            self.stats,
            fill=rgb(theme.ACCENT),
            font=font(13),
            anchor="mm",
        )

    def _end_card(self, image, card):
        result = card.get("result") or {}
        x, y, w, h = self.board_box
        panel_h = 60
        top = y + h - panel_h
        overlay = Image.new("RGBA", (w, panel_h), rgb(theme.BACKGROUND) + (215,))
        region = image.crop((x, top, x + w, top + panel_h)).convert("RGBA")
        region.alpha_composite(overlay)
        image.paste(region.convert("RGB"), (x, top))
        draw = ImageDraw.Draw(image)
        opens, violations = result.get("opens"), result.get("violations")
        if isinstance(violations, dict):
            violations = sum(violations.values())
        if opens is None and violations is None:
            verdict, color = "Engine result (no KiCad stage)", theme.MUTED
        else:
            verdict = "KiCad DRC: %s unconnected · %s violations" % (opens, violations)
            color = theme.ACCENT if result.get("passed") else theme.FAIL
        metrics = []
        if result.get("vias") is not None:
            metrics.append("%d vias" % result["vias"])
        if result.get("copper_length_mm") is not None:
            metrics.append("%.1f mm copper" % result["copper_length_mm"])
        poses = card.get("poses")
        if poses:
            wirelength = highlight.hpwl(self.pins, self.pin_xy(poses))
            metrics.append("HPWL %.0f mm" % (wirelength / 1000.0))
        size, verdict = _fit(draw, safe_text(verdict), w - 24, (15, 14, 13, 12))
        small, line = _fit(draw, safe_text(" · ".join(metrics)), w - 24, (12, 11, 10))
        self.strings.update((verdict, line))
        draw.text((x + 12, top + 19), verdict, fill=rgb(color), font=font(size), anchor="lm")
        draw.text((x + 12, top + 42), line, fill=rgb(theme.MUTED), font=font(small), anchor="lm")


class PairRenderer:
    """Composes two :class:`PanelRenderer` frames under one title strip; duck-types
    ``Renderer.frame(view)`` for the encoder, with a view being a pair of views."""

    def __init__(self, left, right, title, stats, labels):
        self.left, self.right = left, right
        self.panel_w = left.width
        self.width = left.width + right.width
        self.title = safe_text(title or "")
        self.stats = safe_text(stats or "")
        self.labels = [safe_text(t) for t in labels]
        self.legends = [
            highlight.legend(left.constraints or left.reference, reference=not left.constraints),
            highlight.legend(right.constraints or right.reference, reference=not right.constraints),
        ]
        # A panel is its renderer's frame below the renderer's own header.
        body = max(left.height, right.height) - theme.HEADER_PX
        height = theme.HEADER_PX + CAPTION_PX + body
        self.height = height + (height % 2)
        self.strings = {self.title, self.stats} | set(self.labels)
        for legend in self.legends:
            self.strings.update(legend)
        self._cache = [None, None]
        self._last = None

    def frame(self, pair):
        if self._last is not None and self._last[0] is pair:
            return self._last[1]
        image = Image.new("RGB", (self.width, self.height), rgb(theme.BACKGROUND))
        draw = ImageDraw.Draw(image)
        head = theme.HEADER_PX
        size, title = _fit(draw, self.title, self.width - 24 - 330, (16, 15, 14))
        draw.text((12, head / 2), title, fill=rgb(theme.TEXT), font=font(size), anchor="lm")
        draw.text(
            (self.width - 12, head / 2),
            self.stats,
            fill=rgb(theme.MUTED),
            font=font(13),
            anchor="rm",
        )
        for side, (renderer, view) in enumerate(((self.left, pair[0]), (self.right, pair[1]))):
            panel = self._panel(side, renderer, view)
            image.paste(panel, (side * self.panel_w, head))
        x = self.panel_w
        draw.line((x, head + 4, x, self.height - 4), fill=mix(theme.BACKGROUND, theme.MUTED, 0.35))
        self._last = (pair, image)
        return image

    def _panel(self, side, renderer, view):
        cached = self._cache[side]
        if cached is not None and cached[0] is view:
            return cached[1]
        frame = renderer.frame(view)
        panel = Image.new(
            "RGB", (renderer.width, self.height - theme.HEADER_PX), rgb(theme.BACKGROUND)
        )
        panel.paste(frame.crop((0, theme.HEADER_PX, frame.width, frame.height)), (0, CAPTION_PX))
        draw = ImageDraw.Draw(panel)
        label = self.labels[side]
        draw.text((12, 14), label, fill=rgb(theme.TEXT), font=font(15), anchor="lm")
        chips = self.legends[side]
        own = bool(renderer.constraints)
        x = renderer.width - 12
        left_edge = 12 + draw.textlength(label, font=font(15)) + 12
        for text in reversed(chips):
            size, text = _fit(draw, text, max(40, x - left_edge - 12), (11, 10))
            width = draw.textlength(text, font=font(size)) + 12
            box = (x - width, 5, x, 23)
            tone = theme.CONSTRAINT if own else theme.REFERENCE
            draw.rounded_rectangle(box, radius=5, outline=rgb(tone), width=1)
            draw.text(
                ((box[0] + box[2]) / 2.0, 14),
                text,
                fill=rgb(tone if own else theme.MUTED),
                font=font(size),
                anchor="mm",
            )
            self.strings.add(text)
            x = box[0] - 6
        metric = renderer.metric(view)
        if metric:
            size, metric = _fit(draw, safe_text(metric), renderer.width - 24, (12, 11, 10))
            draw.text((12, 35), metric, fill=rgb(theme.MUTED), font=font(size), anchor="lm")
        self._cache[side] = (view, panel)
        return panel


def _end_poses(frames):
    for view, _ms in reversed(frames):
        if view.poses:
            return view.poses
    return {}


def build(
    left,
    right,
    width=960,
    frame_ms=60,
    max_seconds=30.0,
    pacing="showcase",
    labels=None,
    title=None,
    replay_pool=False,
):
    """``(renderer, frames)`` of a comparison of two loaded traces (``replay_pool``: each
    half's pool shortlist replays every start's global placement, :class:`Timeline`)."""
    panel = int(width) // 2
    halves = []
    names = list(labels or ())
    defaults = [default_label(left), default_label(right)]
    names = [names[i] if i < len(names) and names[i] else defaults[i] for i in range(2)]
    for side, (trace, other) in enumerate(((left, right), (right, left))):
        board = storyboard.build(trace, title=names[side])
        timeline = Timeline(
            trace,
            board,
            frame_ms=frame_ms,
            max_seconds=max_seconds,
            pacing=pacing,
            replay_pool=replay_pool,
        )
        reference = []
        if not highlight.constraints_of(trace.header):
            reference = highlight.constraints_of(other.header)
        renderer = PanelRenderer(trace.header, board["subject"], panel, reference=reference)
        frames = timeline.frames
        end = frames[-1][0]
        if end.card is not None and end.card.get("kind") == "end":
            card = dict(end.card, poses=_end_poses(frames))
            frames = frames[:-1] + [(end.copy(card=card), frames[-1][1])]
        halves.append((renderer, frames, timeline.marks, board["subject"]))
    (lr, lf, lm, ls), (rr, rf, rm, rs) = halves
    stats = "%d parts · %d nets · %d layers" % (rs["parts"], rs["nets"], rs["layers"])
    if rs.get("seed") is not None:
        stats += " · seed %s" % rs["seed"]
    default_title = "%s vs. %s" % (ls.get("case") or "left", rs.get("case") or "right")
    pair = PairRenderer(lr, rr, title or default_title, stats, names)
    return pair, synchronize(lf, lm, rf, rm, clock_ms=frame_ms)


def default_label(trace):
    constraints = highlight.constraints_of(trace.header)
    if not constraints:
        return DEFAULT_LABELS[0]
    return highlight.legend(constraints)[0]


def render_compare(
    left,
    right,
    fmt,
    path,
    width=960,
    max_seconds=30.0,
    budget_mb=None,
    title=None,
    labels=None,
    pacing="showcase",
    allow_failed=False,
    replay_pool=False,
):
    """Render one comparison file; returns its manifest entry (without file-system paths)."""
    from .cli import encoded_frames, trace_digest

    results = []
    for trace in (left, right):
        result = (trace.results[-1] if trace.results else {}) or {}
        if result and not result.get("passed", False) and not allow_failed:
            raise SystemExit("a traced run failed its gate; pass --allow-failed to animate it")
        results.append(result)
    if fmt == "webp":
        steps, budget = encode.COMPARE_WEBP_STEPS, 2.5 if budget_mb is None else budget_mb
    elif fmt == "gif":
        steps, budget = encode.COMPARE_GIF_STEPS, 5.0 if budget_mb is None else budget_mb
    else:
        raise SystemExit("a comparison renders to .webp or .gif")
    steps = [dict(s, width=min(s["width"], width)) for s in steps]

    def make(w, frame_ms):
        return build(
            left,
            right,
            width=w,
            frame_ms=frame_ms,
            max_seconds=max_seconds,
            pacing=pacing,
            labels=labels,
            title=title,
            replay_pool=replay_pool,
        )

    data, settings, _frames, renderer = encode.encode(
        fmt, make, int(budget * 1024 * 1024), steps, extra_colours=encode.CONSTRAINT_COLOURS
    )
    Path(path).write_bytes(data)
    count, seconds = encoded_frames(data)
    import PIL

    cases = [str(t.run.get("subject", {}).get("case") or "") for t in (left, right)]
    return dict(
        kind="compare",
        cases=cases,
        labels=list(renderer.labels),
        title=renderer.title,
        file=Path(path).name,
        format=fmt,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=renderer.width,
        height=renderer.height,
        frames=count,
        seconds=seconds,
        trace_sha256={c: trace_digest(t) for c, t in zip(cases, (left, right))},
        results={c: _result_fields(r) for c, r in zip(cases, results)},
        settings=dict(settings, pacing=pacing, max_seconds=max_seconds, replay_pool=replay_pool),
        pillow=PIL.__version__,
        captions=sorted(
            s for s in renderer.strings | renderer.left.strings | renderer.right.strings if s
        ),
    )


def _result_fields(result):
    return {
        k: result.get(k)
        for k in ("passed", "opens", "violations", "rules", "vias", "copper_length_mm")
    }


__all__ = [
    "PairRenderer",
    "PanelRenderer",
    "build",
    "render_compare",
    "scene_phases",
    "synchronize",
]
