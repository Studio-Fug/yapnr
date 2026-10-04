"""Animation of an optimization run: the density evolving beside its S-parameters (§10.5).

Reads a run directory (`spec.json`, `history.json`, `frames.npz`, optionally `result.json`)
and renders one frame per iteration (evenly subsampled to at most `max_frames`, the last one
held): on the left the projected density ρ̄ of the design window, from the board's substrate
colour to copper, with the feeds; on the right |S_ij| in dB at the objective frequencies with
the requirement limits, and below it the epigraph value t over the iterations so far.

Conventions of `pnr.animate`: Pillow only (imported lazily; `//yapnr/rf:animate`), the
viewer's palette, animated WebP at 800 px within 2.5 MB and GIF at 640 px within 5 MB,
the final frame held, frames rendered lazily, no metadata, deterministic for the same run and Pillow version.

`figure` draws a still of a validated run (`validation.json`, `yapnr.rf.validate`): the
exported footprint's copper on the left and, on the right, |S_ij| of excitation 1 over the
re-validation sweeps on the optimization grid (thin) and the finer grids (thicker) with the fine
criteria, and the radiated fraction when the case has one.

    python -m yapnr.rf.animate RUN_DIR --out run.webp
    python -m yapnr.rf.animate RUN_DIR --figure run.png
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os

import numpy as np

BACKGROUND = "#10191e"
SUBSTRATE = "#15232a"
COPPER = "#e89a73"
OUTLINE = "#acc7ce"
TEXT = "#dce5e9"
MUTED = "#8a9ea7"
ACCENT = "#9ee6d1"
FAIL = "#ff566b"
OK = "#62ffad"
SERIES = ("#e89a73", "#6eb6e5", "#dfbd62", "#aa9de9", "#9ee6d1", "#f28cc0", "#62ffad")
HEADER_PX = 34
FOOTER_PX = 34
WEBP_STEPS = ((80, 800), (70, 800), (60, 800), (60, 720), (60, 640))
GIF_STEPS = ((128, 640), (96, 640), (64, 640), (64, 560))
HOLD_MS = 1500
WEBP_BUDGET = int(2.5 * 1024 * 1024)
GIF_BUDGET = 5 * 1024 * 1024


def _rgb(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))


def _font(size: int):
    from PIL import ImageFont

    return ImageFont.load_default(size)


class Run:
    """The parts of a run directory the animation needs."""

    def __init__(self, run_dir: str):
        with open(os.path.join(run_dir, "spec.json"), encoding="utf-8") as fh:
            self.spec = json.load(fh)
        with open(os.path.join(run_dir, "history.json"), encoding="utf-8") as fh:
            self.history = json.load(fh)["iterations"]
        with np.load(os.path.join(run_dir, "frames.npz"), allow_pickle=False) as z:
            self.frames = np.array(z["rho_bar"])
        self.result = None
        path = os.path.join(run_dir, "result.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.result = json.load(fh)
        n = min(len(self.history), len(self.frames))
        self.history = self.history[:n]
        self.frames = self.frames[:n]
        if n == 0:
            raise ValueError("the run has no iterations yet")
        self.freqs = self._freqs()

    def _freqs(self) -> list[float]:
        if self.result:
            return list(self.result["solver"]["objective_ghz"])
        keys = sorted({k[1] for k in self.history[0]["keys"]})
        return keys


class Renderer:
    """Draws frame `i` (an index into the run's iterations) at a given width."""

    def __init__(self, run: Run, width: int = 800):
        self.run = run
        self.width = int(width)
        self.height = int(round(self.width * 9 / 16))
        s_all = [v for h in run.history for vals in h["s_db"].values() for v in vals]
        s_all = [v for v in s_all if v is not None and math.isfinite(v)]
        self.db_lo = max(-40.0, math.floor(min(s_all + [-20.0]) / 5.0) * 5.0)
        self.db_hi = 0.0
        ts = [h["t"] for h in run.history]
        self.t_lo, self.t_hi = min(ts), max(ts)
        if self.t_hi - self.t_lo < 1e-6:
            self.t_hi = self.t_lo + 1.0

    def frame(self, i: int):
        from PIL import Image, ImageDraw

        w, h = self.width, self.height
        k = self.width / 800.0
        img = Image.new("RGB", (w, h), _rgb(BACKGROUND))
        draw = ImageDraw.Draw(img)
        rec = self.run.history[i]
        spec = self.run.spec
        draw.text(
            (12 * k, HEADER_PX * k / 2),
            f"RF inverse design: {spec['name']}",
            fill=_rgb(TEXT),
            font=_font(int(16 * k)),
            anchor="lm",
        )
        draw.text(
            (w - 12 * k, HEADER_PX * k / 2),
            f"iteration {rec['iteration']}   beta {rec['beta']:g}",
            fill=_rgb(MUTED),
            font=_font(int(13 * k)),
            anchor="rm",
        )
        top, bottom = HEADER_PX * k, h - FOOTER_PX * k
        self._design(img, self.run.frames[i], (8 * k, top, 0.5 * w - 8 * k, bottom))
        mid = top + 0.62 * (bottom - top)
        self._sparams(draw, rec, (0.5 * w + 30 * k, top + 6 * k, w - 14 * k, mid - 16 * k), k)
        self._trace(draw, i, (0.5 * w + 30 * k, mid + 4 * k, w - 14 * k, bottom - 16 * k), k)
        met = [p for p in rec["phi"].values() if p is not None]
        worst = [max(v for v in p if v is not None) for p in met if any(v is not None for v in p)]
        ok = sum(1 for v in worst if v <= 0)
        text = (
            f"t = {rec['t']:+.3f}    gray {rec['gray']:.2f}    {ok}/{len(worst)} requirements met"
        )
        draw.text(
            (12 * k, h - FOOTER_PX * k / 2),
            text,
            fill=_rgb(TEXT),
            font=_font(int(13 * k)),
            anchor="lm",
        )
        return img

    def _design(self, img, frame: np.ndarray, box) -> None:
        from PIL import Image

        x0, y0, x1, y1 = box
        ni, nj = frame.shape
        margin = 3
        scale = max(1, int(min((x1 - x0) / (ni + 2 * margin), (y1 - y0) / (nj + 2 * margin))))
        sub = np.array(_rgb(SUBSTRATE), dtype=np.float64)
        cu = np.array(_rgb(COPPER), dtype=np.float64)
        rho = frame.astype(np.float64) / 255.0
        ext = np.zeros((ni + 2 * margin, nj + 2 * margin))
        ext[margin : margin + ni, margin : margin + nj] = rho
        for p in self.run.spec["ports"]:
            self._feed(ext, p, margin, ni, nj)
        rgb = sub[None, None, :] + ext[..., None] * (cu - sub)[None, None, :]
        rgb = np.round(rgb).astype(np.uint8)
        # (i, j) with j up → image rows from the top.
        arr = np.transpose(rgb, (1, 0, 2))[::-1]
        tile = Image.fromarray(arr, "RGB").resize(
            (arr.shape[1] * scale, arr.shape[0] * scale), Image.NEAREST
        )
        ox = int(x0 + 0.5 * (x1 - x0 - tile.width))
        oy = int(y0 + 0.5 * (y1 - y0 - tile.height))
        img.paste(tile, (ox, oy))
        from PIL import ImageDraw

        d = ImageDraw.Draw(img)
        d.rectangle(
            (
                ox + margin * scale,
                oy + margin * scale,
                ox + (margin + ni) * scale - 1,
                oy + (margin + nj) * scale - 1,
            ),
            outline=_rgb(OUTLINE),
        )

    def _feed(self, ext, port: dict, margin: int, ni: int, nj: int) -> None:
        spec = self.run.spec
        pitch = spec["grid"]["pitch_mm"]
        x0 = spec["design_region"]["x_mm"][0]
        y0 = spec["design_region"]["y_mm"][0]
        width = port.get("width_cells")
        if not width and self.run.result:
            width = self.run.result["solver"]["port_width_cells"][str(port["n"])]
        width = int(width or 2)
        side, at = port["side"], port["at_mm"]
        if side in ("W", "E"):
            c = int(round((at - y0) / pitch - width / 2)) + margin
            rows = slice(0, margin) if side == "W" else slice(margin + ni, None)
            ext[rows, c : c + width] = 1.0
        else:
            c = int(round((at - x0) / pitch - width / 2)) + margin
            cols = slice(0, margin) if side == "S" else slice(margin + nj, None)
            ext[c : c + width, cols] = 1.0

    def _axes(self, draw, box, k, ylabel: str, ylo: float, yhi: float):
        x0, y0, x1, y1 = box
        draw.rectangle((x0, y0, x1, y1), outline=_rgb(MUTED))
        f = _font(int(10 * k))
        for v in (ylo, 0.5 * (ylo + yhi), yhi):
            y = y1 - (v - ylo) / (yhi - ylo) * (y1 - y0)
            draw.text((x0 - 4 * k, y), f"{v:g}", fill=_rgb(MUTED), font=f, anchor="rm")
        draw.text((x0 + 4 * k, y0 + 4 * k), ylabel, fill=_rgb(MUTED), font=f, anchor="la")

    def _sparams(self, draw, rec, box, k) -> None:
        x0, y0, x1, y1 = box
        freqs = self.run.freqs
        flo, fhi = min(freqs), max(freqs)
        if fhi - flo < 1e-9:
            flo, fhi = flo - 0.5, fhi + 0.5
        pad = 0.05 * (fhi - flo)
        flo, fhi = flo - pad, fhi + pad
        self._axes(draw, box, k, "|S| (dB)", self.db_lo, self.db_hi)

        def px(f, v):
            v = min(max(v, self.db_lo), self.db_hi)
            return (
                x0 + (f - flo) / (fhi - flo) * (x1 - x0),
                y1 - (v - self.db_lo) / (self.db_hi - self.db_lo) * (y1 - y0),
            )

        for req in self.run.spec["requirements"]:
            if "s" not in req:
                continue
            band = self.run.spec["bands"][req["band"]]
            lo, hi = band["lo_ghz"], band["hi_ghz"]
            for key, colour in (("max_db", FAIL), ("min_db", OK)):
                if key in req:
                    draw.line(
                        [px(lo, req[key]), px(hi, req[key])],
                        fill=_rgb(colour),
                        width=max(1, int(k)),
                    )
        f = _font(int(10 * k))
        for n, (name, vals) in enumerate(sorted(rec["s_db"].items())):
            colour = _rgb(SERIES[n % len(SERIES)])
            pts = [px(fr, v) for fr, v in zip(freqs, vals) if v is not None]
            if len(pts) > 1:
                draw.line(pts, fill=colour, width=max(1, int(2 * k)))
            for x, y in pts:
                draw.ellipse((x - 2 * k, y - 2 * k, x + 2 * k, y + 2 * k), fill=colour)
            draw.text((x1 - 4 * k, y0 + (6 + 12 * n) * k), name, fill=colour, font=f, anchor="ra")
        draw.text((x0, y1 + 3 * k), f"{flo + pad:g} GHz", fill=_rgb(MUTED), font=f, anchor="la")
        draw.text((x1, y1 + 3 * k), f"{fhi - pad:g} GHz", fill=_rgb(MUTED), font=f, anchor="ra")

    def _trace(self, draw, i, box, k) -> None:
        x0, y0, x1, y1 = box
        lo, hi = self.t_lo, self.t_hi
        self._axes(draw, box, k, "t", round(lo, 2), round(hi, 2))
        n = len(self.run.history)
        ts = [h["t"] for h in self.run.history[: i + 1]]
        pts = [
            (x0 + j / max(1, n - 1) * (x1 - x0), y1 - (t - lo) / (hi - lo) * (y1 - y0))
            for j, t in enumerate(ts)
        ]
        if lo < 0 < hi:
            yz = y1 - (0 - lo) / (hi - lo) * (y1 - y0)
            draw.line([(x0, yz), (x1, yz)], fill=_rgb(MUTED), width=1)
        if len(pts) > 1:
            draw.line(pts, fill=_rgb(ACCENT), width=max(1, int(2 * k)))
        x, y = pts[-1]
        draw.ellipse((x - 3 * k, y - 3 * k, x + 3 * k, y + 3 * k), fill=_rgb(ACCENT))


def frame_indices(n: int, max_frames: int = 120) -> list[int]:
    """Evenly subsampled iterations, the first and the last included."""
    if n <= max_frames:
        return list(range(n))
    return sorted({int(round(j * (n - 1) / (max_frames - 1))) for j in range(max_frames)})


def durations(count: int, frame_ms: int = 80, hold_ms: int = HOLD_MS) -> list[int]:
    """Frame durations: the last frame (the final design) is held."""
    return [frame_ms] * (count - 1) + [hold_ms]


def _sequence(renderer: Renderer, picks: list[int], convert=None):
    from PIL import Image

    class _Frames(Image.Image):
        def __init__(self):
            super().__init__()
            self._index = -1
            self.seek(0)

        @property
        def n_frames(self):
            return len(picks)

        @property
        def is_animated(self):
            return len(picks) > 1

        def seek(self, frame):
            if frame == self._index:
                return
            if not 0 <= frame < len(picks):
                raise EOFError("no such frame")
            im = renderer.frame(picks[frame])
            if convert is not None:
                im = convert(im)
            self.im = im.im
            self._mode = im.mode
            self._size = im.size
            self.palette = im.palette.copy() if im.palette is not None else None
            self.info = {}
            self._index = frame

        def tell(self):
            return self._index

    return _Frames()


def webp_bytes(run: Run, picks, quality: int, width: int, frame_ms: int = 80) -> bytes:
    seq = _sequence(Renderer(run, width), picks)
    out = io.BytesIO()
    seq.save(
        out,
        format="WEBP",
        save_all=True,
        duration=durations(len(picks), frame_ms),
        loop=0,
        quality=quality,
        method=4,
        minimize_size=True,
        lossless=False,
    )
    return out.getvalue()


def gif_bytes(run: Run, picks, colors: int, width: int, frame_ms: int = 80) -> bytes:
    from PIL import Image

    renderer = Renderer(run, width)
    sample = (
        [picks[int(round(j * (len(picks) - 1) / 7))] for j in range(8)] if len(picks) > 8 else picks
    )
    sheet = Image.new("RGB", (renderer.width, renderer.height * len(sample)))
    for n, idx in enumerate(sample):
        sheet.paste(renderer.frame(idx), (0, renderer.height * n))
    palette = sheet.quantize(
        colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    seq = _sequence(
        renderer, picks, lambda im: im.quantize(palette=palette, dither=Image.Dither.NONE)
    )
    out = io.BytesIO()
    seq.save(
        out,
        format="GIF",
        save_all=True,
        duration=durations(len(picks), frame_ms),
        loop=0,
        optimize=False,
        disposal=1,
    )
    return out.getvalue()


def animate(run_dir: str, out: str, *, max_frames: int = 120) -> dict:
    """Render `run_dir` to `out` (.webp or .gif) within the size budget; returns the settings."""
    run = Run(run_dir)
    picks = frame_indices(len(run.history), max_frames)
    kind = os.path.splitext(out)[1].lower()
    if kind == ".webp":
        steps, budget, make = WEBP_STEPS, WEBP_BUDGET, webp_bytes
    elif kind == ".gif":
        steps, budget, make = GIF_STEPS, GIF_BUDGET, gif_bytes
    else:
        raise ValueError("the output must be .webp or .gif")
    data = b""
    for q, width in steps:
        data = make(run, picks, q, width)
        if len(data) <= budget:
            with open(out, "wb") as fh:
                fh.write(data)
            return {"frames": len(picks), "bytes": len(data), "setting": [q, width]}
    raise ValueError(f"animation over its size budget: {len(data)} > {budget} bytes")


# -- the still figure of a validated run ------------------------------------------------------


def _copper_raster(run_dir: str, spec_d: dict, sub: int, margin: int) -> np.ndarray:
    """The footprint's copper sampled `sub` times per pixel, with `margin` pixels of feed
    around the region (feeds drawn as straight strips)."""
    from yapnr.rf.export.kicad import read_footprint
    from yapnr.rf.export.raster import rasterize
    from yapnr.rf.spec import Spec
    from yapnr.rf.validate import board_polygons

    spec = Spec.from_dict(spec_d)
    fp = read_footprint(os.path.join(run_dir, "footprint.kicad_mod"))
    polys = board_polygons(fp, spec)
    pitch = spec.grid.pitch_mm
    x0, x1, y0, y1 = spec.design_region
    step = pitch / sub
    xs = np.arange(x0 - margin * pitch + 0.5 * step, x1 + margin * pitch, step)
    ys = np.arange(y0 - margin * pitch + 0.5 * step, y1 + margin * pitch, step)
    cu = rasterize(polys, xs, ys)
    for pad, port in zip(sorted(fp.pads, key=lambda q: int(q["number"])), spec.ports):
        sx, sy = pad["size"]
        w = sy if port.side in ("W", "E") else sx
        c = port.at_mm
        if port.side == "W":
            cu[np.ix_(xs < x0, np.abs(ys - c) < w / 2)] = True
        elif port.side == "E":
            cu[np.ix_(xs > x1, np.abs(ys - c) < w / 2)] = True
        elif port.side == "S":
            cu[np.ix_(np.abs(xs - c) < w / 2, ys < y0)] = True
        else:
            cu[np.ix_(np.abs(xs - c) < w / 2, ys > y1)] = True
    return cu


def figure(run_dir: str, out: str, *, width: int = 1000) -> dict:
    """Write the still figure of a validated run (PNG); returns its size in bytes."""
    from PIL import Image, ImageDraw

    from yapnr.rf import cases

    run_spec = os.path.join(run_dir, "spec.json")
    with open(run_spec, encoding="utf-8") as fh:
        spec_d = json.load(fh)
    with open(os.path.join(run_dir, "validation.json"), encoding="utf-8") as fh:
        val = json.load(fh)
    k = width / 1000.0
    w, h = width, int(round(540 * k))
    img = Image.new("RGB", (w, h), _rgb(BACKGROUND))
    draw = ImageDraw.Draw(img)
    verdict = []
    for level in ("coarse", "fine", "finer"):
        if level in val:
            verdict.append(f"{level} {'pass' if val[level]['ok'] else 'FAIL'}")
    draw.text(
        (12 * k, HEADER_PX * k / 2),
        f"RF inverse design: {spec_d['name']}",
        fill=_rgb(TEXT),
        font=_font(int(16 * k)),
        anchor="lm",
    )
    draw.text(
        (w - 12 * k, HEADER_PX * k / 2),
        "re-simulated from the footprint: " + ", ".join(verdict),
        fill=_rgb(OK if val.get("ok") else FAIL),
        font=_font(int(13 * k)),
        anchor="rm",
    )
    # Left: the copper.
    margin, sub = 4, 4
    cu = _copper_raster(run_dir, spec_d, sub, margin)
    box = (8 * k, HEADER_PX * k, 0.46 * w, h - 30 * k)
    scale = min((box[2] - box[0]) / cu.shape[0], (box[3] - box[1]) / cu.shape[1])
    sub_c = np.array(_rgb(SUBSTRATE), dtype=np.uint8)
    cu_c = np.array(_rgb(COPPER), dtype=np.uint8)
    rgb = np.where(cu[..., None], cu_c[None, None, :], sub_c[None, None, :])
    arr = np.ascontiguousarray(np.transpose(rgb, (1, 0, 2))[::-1])
    tile = Image.fromarray(arr, "RGB").resize(
        (max(1, int(arr.shape[1] * scale)), max(1, int(arr.shape[0] * scale))), Image.NEAREST
    )
    ox = int(box[0] + 0.5 * (box[2] - box[0] - tile.width))
    oy = int(box[1] + 0.5 * (box[3] - box[1] - tile.height))
    img.paste(tile, (ox, oy))
    px_mm = scale * sub / spec_d["grid"]["pitch_mm"]
    m = margin * sub * scale
    draw.rectangle(
        (ox + m, oy + m, ox + tile.width - m - 1, oy + tile.height - m - 1), outline=_rgb(OUTLINE)
    )
    # Lumped parts: the body outlined (it is void in the copper), labelled with its value.
    x0_mm = spec_d["design_region"]["x_mm"][0] - margin * spec_d["grid"]["pitch_mm"]
    y1_mm = spec_d["design_region"]["y_mm"][1] + margin * spec_d["grid"]["pitch_mm"]
    for el in spec_d.get("lumped", []):
        (xa, xb), (ya, yb) = sorted(el["x_mm"]), sorted(el["y_mm"])
        rect = (
            ox + (xa - x0_mm) * px_mm,
            oy + (y1_mm - yb) * px_mm,
            ox + (xb - x0_mm) * px_mm,
            oy + (y1_mm - ya) * px_mm,
        )
        draw.rectangle(rect, outline=_rgb(TEXT), width=max(1, int(2 * k)))
        draw.text(
            (rect[2] + 4 * k, 0.5 * (rect[1] + rect[3])),
            f"{el['ohms']:g} Ω",
            fill=_rgb(TEXT),
            font=_font(int(11 * k)),
            anchor="lm",
        )
    bar = 2.0 if px_mm * 5 > 0.4 * tile.width else 5.0
    yb = oy + tile.height + 10 * k
    draw.line([(ox, yb), (ox + bar * px_mm, yb)], fill=_rgb(TEXT), width=max(1, int(2 * k)))
    draw.text(
        (ox + bar * px_mm + 6 * k, yb),
        f"{bar:g} mm",
        fill=_rgb(MUTED),
        font=_font(int(11 * k)),
        anchor="lm",
    )
    # Right: |S_j1| and η.
    has_eta = any(key.startswith("eta") for key in val["fine"]["table"]) if "fine" in val else False
    right = (0.53 * w, HEADER_PX * k + 8 * k, w - 14 * k, h - 30 * k)
    if has_eta:
        split = right[1] + 0.62 * (right[3] - right[1])
        s_box = (right[0], right[1], right[2], split - 18 * k)
        e_box = (right[0], split + 6 * k, right[2], right[3])
    else:
        s_box, e_box = right, None
    crit = cases.CRITERIA.get(val["case"], {}).get("fine", [])
    self = _Plot(draw, k)
    tables = [
        (val[lv]["table"], width_)
        for lv, width_ in (("coarse", 1), ("fine", 2), ("finer", 3))
        if lv in val
    ]
    ghz = tables[-1][0]["ghz"]
    names = plotted_sparams(crit, tables[-1][0])
    lo = min(-40.0, math.floor(min(min(tables[-1][0][n]) for n in names) / 10.0) * 10.0)
    lo = max(lo, -60.0)
    self.frame(
        s_box,
        ghz[0],
        ghz[-1],
        lo,
        0.0,
        ("|S_i1|" if all(n.endswith("1") for n in names) else "|S_ij|")
        + " (dB)   thin to thick: optimization pitch, 1/2, 1/3",
    )
    for c in crit:
        if c.kind in ("s_max", "s_min") and c.ghz is not None:
            col = FAIL if c.kind == "s_max" else OK
            self.hline(c.ghz[0], c.ghz[1], c.limit, col)
    for n, name in enumerate(names):
        colour = SERIES[n % len(SERIES)]
        for table, lw in tables:
            self.curve(table["ghz"], table[name], colour, lw)
        self.label(n, name, colour, len(names))
    if e_box is not None:
        self.frame(e_box, ghz[0], ghz[-1], 0.0, 1.0, "radiated fraction")
        for c in crit:
            if c.kind == "eta_min":
                f0, f1 = c.ghz if c.at_ghz is None else (min(c.at_ghz), max(c.at_ghz))
                self.hline(f0, f1, c.limit, OK)
        for table, lw in tables:
            self.curve(table["ghz"], table["eta1"], ACCENT, lw)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    with open(out, "wb") as fh:
        fh.write(buf.getvalue())
    return {"bytes": len(buf.getvalue()), "size": [w, h]}


def plotted_sparams(checks, table: dict) -> list[str]:
    """The S-parameters the figure plots: |S_i1| of every port, plus the other entries the
    criteria judge (a combiner's output match and isolation), in the table's order."""
    names = [key for key in table if key.startswith("S") and key.endswith("1")]
    for c in checks:
        if c.kind in ("s_max", "s_min"):
            key = f"S{c.ports[0]}{c.ports[1]}"
            if key in table and key not in names:
                names.append(key)
    return sorted(names, key=lambda n: (n[2:], n[1:2]))


class _Plot:
    """A minimal line plot on a Pillow canvas."""

    def __init__(self, draw, k: float):
        self.draw, self.k = draw, k

    def frame(self, box, xlo, xhi, ylo, yhi, title):
        self.box, self.xlo, self.xhi, self.ylo, self.yhi = box, xlo, xhi, ylo, yhi
        x0, y0, x1, y1 = box
        d, k = self.draw, self.k
        d.rectangle(box, outline=_rgb(MUTED))
        f = _font(int(10 * k))
        span = yhi - ylo
        ticks = np.linspace(ylo, yhi, 5) if span <= 1.0 else np.arange(ylo, yhi + 1e-9, 10.0)
        for v in ticks:
            y = self.py(v)
            d.line([(x0, y), (x0 + 4 * k, y)], fill=_rgb(MUTED))
            d.text((x0 - 4 * k, y), f"{v:g}", fill=_rgb(MUTED), font=f, anchor="rm")
        d.text((x0 + 6 * k, y0 + 4 * k), title, fill=_rgb(MUTED), font=f, anchor="la")
        d.text((x0, y1 + 3 * k), f"{xlo:g} GHz", fill=_rgb(MUTED), font=f, anchor="la")
        d.text((x1, y1 + 3 * k), f"{xhi:g} GHz", fill=_rgb(MUTED), font=f, anchor="ra")

    def px(self, x):
        x0, _, x1, _ = self.box
        return x0 + (x - self.xlo) / (self.xhi - self.xlo) * (x1 - x0)

    def py(self, v):
        _, y0, _, y1 = self.box
        v = min(max(v, self.ylo), self.yhi)
        return y1 - (v - self.ylo) / (self.yhi - self.ylo) * (y1 - y0)

    def hline(self, xa, xb, v, colour):
        self.draw.line(
            [(self.px(xa), self.py(v)), (self.px(xb), self.py(v))],
            fill=_rgb(colour),
            width=max(1, int(self.k)),
        )

    def curve(self, xs, ys, colour, lw):
        # Break the line at gaps in frequency (the diplexer's two channels), against the 90th
        # percentile of the steps (the antenna's sweep mixes 0.04 and 0.01 GHz steps).
        xs = list(xs)
        step = float(np.percentile(np.diff(xs), 90)) if len(xs) > 1 else 1.0
        seg = []
        for i, (x, y) in enumerate(zip(xs, ys)):
            if seg and x - xs[i - 1] > 3.5 * step:
                self._poly(seg, colour, lw)
                seg = []
            seg.append((self.px(x), self.py(y)))
        self._poly(seg, colour, lw)

    def _poly(self, pts, colour, lw):
        if len(pts) > 1:
            self.draw.line(pts, fill=_rgb(colour), width=max(1, int(lw * self.k)))

    def label(self, n, name, colour, count=1):
        x0, y0, x1, y1 = self.box
        self.draw.text(
            (x1 - 6 * self.k, y1 - (8 + 13 * (count - n)) * self.k),
            name,
            fill=_rgb(colour),
            font=_font(int(11 * self.k)),
            anchor="ra",
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m yapnr.rf.animate", description=__doc__.split("\n")[0]
    )
    ap.add_argument("run_dir")
    ap.add_argument("--out", help="output .webp or .gif")
    ap.add_argument("--figure", help="output .png: the still figure of a validated run")
    ap.add_argument("--max-frames", type=int, default=120)
    args = ap.parse_args(argv)
    if not args.out and not args.figure:
        ap.error("give --out and/or --figure")
    if args.out:
        print(json.dumps(animate(args.run_dir, args.out, max_frames=args.max_frames)))
    if args.figure:
        print(json.dumps(figure(args.run_dir, args.figure)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
