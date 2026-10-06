"""Renders a :class:`pnr.plane_partition.PlaneTrace` to a WebP animation and PNG stills.

See ``docs/plane-partition.md``. This reuses :mod:`pnr.animate`'s style (``theme``,
the background/substrate/outline colours) and its size budgets (``encode``: the same WebP
step-down until a frame budget is met), but draws its own frames directly from the
partition's cell grid (:class:`pnr.plane_partition.PlaneFrame`), not through the general
board :class:`pnr.animate.render.Renderer`: that one colours copper by *layer* (every net on
``F.Cu`` the same orange), which is right for a single-net-per-track routed board but hides
the one thing this animation has to show -- several rails sharing one layer, each its own
colour.

``make_frames`` turns a trace into ``[(PlaneFrame, ms)]`` with a short hold on the first frame
of each stage and a longer one on the last frame overall (the same "hold the ending" shape as
:mod:`.timeline`); :func:`render_webp` and :func:`render_stills` are the two entry points the
docs example (``regression/animate_plane_partition.py``) and the tests call.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw

from . import encode, theme
from .render import Transform, font, mix, rgb, safe_text

# Rail colours, in net order (cycles past this many rails sharing one layer; the legend and
# caption still name the net, so a repeat is not ambiguous for the handful a layer actually
# carries). Chosen from the existing theme so a plane-partition frame reads as one family
# with every other yapnr animation.
RAIL_COLOURS = (
    theme.NEW,
    theme.PROVISIONAL,
    theme.CONSTRAINT,
    theme.LAYERS["In1.Cu"],
    theme.LAYERS["F.Cu"],
    theme.LAYERS["In2.Cu"],
    theme.LAYERS["B.Cu"],
    theme.FAIL,
)
FREE_COLOR = theme.SUBSTRATE
BLOCKED_RGB = mix(theme.SUBSTRATE, theme.MUTED, 0.55)  # an (r, g, b) tuple already
NECK_COLOR = theme.FAIL
LEGEND_PX = 26
# Canonical stage order (pnr.plane_partition.PlaneTrace); a still's file name sorts by this.
STAGE_ORDER = ("raster", "terminals", "trunk", "widen", "grow", "carve", "polygons", "necks")
HOLD_STAGE_MS = 650  # the first frame of a new stage holds a little longer
HOLD_END_MS = 1800  # the last frame of the whole animation


def rail_color(nets: List[str], index: int) -> str:
    return RAIL_COLOURS[index % len(RAIL_COLOURS)]


def make_frames(trace, frame_ms: int = 70) -> List[Tuple[object, int]]:
    """``[(PlaneFrame, ms)]`` from a :class:`pnr.plane_partition.PlaneTrace`: every frame at
    ``frame_ms``, the first of each stage held a little longer, the last one longest."""
    out = []
    prev_stage = None
    frames = trace.frames
    for i, f in enumerate(frames):
        ms = frame_ms
        if f.stage != prev_stage:
            ms += HOLD_STAGE_MS
        if i == len(frames) - 1:
            ms += HOLD_END_MS
        out.append((f, ms))
        prev_stage = f.stage
    return out


class Renderer:
    """Draws one :class:`pnr.plane_partition.PlaneFrame` of one trace at ``width`` pixels."""

    def __init__(self, trace, width: int = 800, title: str = ""):
        self.trace = trace
        self.width = int(width)
        self.ny, self.nx = trace.free.shape
        self.h_mm = float(trace.h_mm or 0.1)
        self.j0, self.j1, self.i0, self.i1 = self._crop_box()
        self.x0_mm, self.y0_mm = self.i0 * self.h_mm, self.j0 * self.h_mm
        self.w_mm = (self.i1 - self.i0) * self.h_mm
        self.board_h_mm = (self.j1 - self.j0) * self.h_mm
        board_px_h = int(round(self.width * self.board_h_mm / max(1e-6, self.w_mm)))
        board_px_h = max(int(0.3 * self.width), min(int(1.3 * self.width), board_px_h))
        self.board_box_h = board_px_h
        self.title = safe_text(title or "")
        self.height = theme.HEADER_PX + LEGEND_PX + board_px_h + theme.FOOTER_PX
        self.height += self.height % 2
        self.palette = {n: rail_color(trace.nets, i) for i, n in enumerate(trace.nets)}
        self._static = self._base_raster()
        self._last = None

    def _crop_box(self) -> Tuple[int, int, int, int]:
        """``(j0, j1, i0, i1)``: the free cells' bounding box plus a small margin, so an
        outer pour's small region (most of a board's own full-layer raster is outside it)
        fills the frame instead of sitting in a corner of the whole board. Blocked cells
        are not part of this box: a keepout can sit anywhere on the board (``_outer``
        blocks a foreign pad's land wherever it is, not only inside the region), so using
        them too could pull the crop box far past the region that is actually drawn."""
        jj, ii = np.nonzero(self.trace.free)
        if not len(jj):
            return 0, self.ny, 0, self.nx
        pad = max(4, int(round(0.3 / self.h_mm)))  # a bit over the margin plane_partition draws
        j0, j1 = max(0, int(jj.min()) - pad), min(self.ny, int(jj.max()) + pad + 1)
        i0, i1 = max(0, int(ii.min()) - pad), min(self.nx, int(ii.max()) + pad + 1)
        return j0, j1, i0, i1

    # --- the parts of the frame that never change -----------------------------------
    def _base_raster(self) -> np.ndarray:
        """``(h, w, 3)`` uint8 over the crop box, row 0 at the box's lowest y (flipped to
        screen order on use): background outside the outline, free copper, and foreign
        (blocked) copper."""
        j0, j1, i0, i1 = self.j0, self.j1, self.i0, self.i1
        free = self.trace.free[j0:j1, i0:i1]
        colors = np.empty((j1 - j0, i1 - i0, 3), dtype=np.uint8)
        colors[:] = rgb(theme.BACKGROUND)
        colors[free] = rgb(FREE_COLOR)
        if self.trace.blocked is not None:
            blocked = self.trace.blocked[j0:j1, i0:i1]
            colors[blocked & ~free] = BLOCKED_RGB
        return colors

    # --- frames -----------------------------------------------------------------------
    def frame(self, f) -> Image.Image:
        if self._last is not None and self._last[0] is f:
            return self._last[1]
        image = Image.new("RGB", (self.width, self.height), rgb(theme.BACKGROUND))
        draw = ImageDraw.Draw(image)
        self._header(draw, f)
        self._legend(draw)
        board_top = theme.HEADER_PX + LEGEND_PX
        self._board(image, f, board_top)
        self._caption(draw, f)
        self._last = (f, image)
        return image

    def _header(self, draw, f):
        draw.text((10, 7), self.title, fill=rgb(theme.TEXT), font=font(18))

    def _legend(self, draw):
        x = 10
        y = theme.HEADER_PX + 4
        r = 6
        for n in self.trace.nets:
            color = rgb(self.palette[n])
            draw.ellipse((x, y, x + 2 * r, y + 2 * r), fill=color)
            label = safe_text(n)
            draw.text((x + 2 * r + 6, y - 3), label, fill=rgb(theme.TEXT), font=font(14))
            x += 2 * r + 6 + 8 * (len(label) + 2)

    def _board(self, image, f, board_top):
        colors = self._static
        if f.label is not None:
            colors = colors.copy()
            label = f.label[self.j0 : self.j1, self.i0 : self.i1]
            for i, n in enumerate(self.trace.nets):
                colors[label == i] = rgb(self.palette[n])
        # Row 0 of the crop is its lowest y (the engine's y-up frame); screen rows go top
        # to bottom, so flip vertically.
        board_img = Image.fromarray(colors[::-1, :, :])
        m = theme.MARGIN * max(self.w_mm, self.board_h_mm)
        x0_mm, y0_mm = self.x0_mm, self.y0_mm
        tf = self.tf = Transform(
            (x0_mm - m, y0_mm - m, x0_mm + self.w_mm + m, y0_mm + self.board_h_mm + m),
            self.width,
            self.board_box_h,
        )
        x0, y0 = tf(x0_mm, y0_mm + self.board_h_mm)
        x1, y1 = tf(x0_mm + self.w_mm, y0_mm)
        px_w, px_h = max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))
        resized = board_img.resize((px_w, px_h), Image.NEAREST)
        image.paste(resized, (int(round(x0)), board_top + int(round(y0))))
        draw = ImageDraw.Draw(image)
        box = (x0, board_top + y0, x0 + px_w, board_top + y0 + px_h)
        draw.rectangle(box, outline=rgb(theme.OUTLINE), width=1)
        for row in f.extra.get("ways", []) or []:
            self._neck_marker(draw, tf, board_top, row)

    def _neck_marker(self, draw, tf, board_top, row):
        at = row.get("neck_at")
        if at is None:
            return
        x, y = tf(at[0], at[1])
        y += board_top
        r = 4
        draw.ellipse((x - r, y - r, x + r, y + r), outline=rgb(NECK_COLOR), width=2)

    def _caption(self, draw, f):
        y = self.height - theme.FOOTER_PX + 8
        draw.text((10, y), safe_text(f.caption), fill=rgb(theme.MUTED), font=font(14))


# --- entry points ---------------------------------------------------------------------


def render_webp(
    trace,
    path,
    *,
    title: str = "",
    budget_bytes: int = int(2.5 * 1024 * 1024),
    width: int = 800,
    frame_ms: int = 70,
) -> dict:
    """Write an animated WebP of ``trace`` to ``path`` (encoded with
    :func:`pnr.animate.encode.encode`, the docs' usual step-down); returns the settings used."""

    def make(w, ms):
        renderer = Renderer(trace, width=w, title=title)
        frames = make_frames(trace, frame_ms=ms)
        return renderer, frames

    steps = [dict(s, width=min(s["width"], width)) for s in encode.WEBP_STEPS]
    data, settings, _frames, _renderer = encode.encode("webp", make, budget_bytes, steps)
    with open(path, "wb") as out:
        out.write(data)
    return settings


def render_stills(
    trace, out_dir, *, prefix: str = "plane-partition", title: str = "", width: int = 900
) -> List[str]:
    """One PNG per stage of ``trace`` (its last frame): ``[prefix]-[stage].png`` under
    ``out_dir``. Returns the written file names (not full paths)."""
    import os

    os.makedirs(out_dir, exist_ok=True)
    renderer = Renderer(trace, width=width, title=title)
    last_of_stage: Dict[str, object] = {}
    order: List[str] = []
    for f in trace.frames:
        if f.stage not in last_of_stage:
            order.append(f.stage)
        last_of_stage[f.stage] = f
    names = []
    for stage in order:
        image = renderer.frame(last_of_stage[stage])
        name = "%s-%s.png" % (prefix, stage)
        image.save(os.path.join(out_dir, name), format="PNG")
        names.append(name)
    return names
