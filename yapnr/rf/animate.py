"""Animation of an optimization run: the density evolving beside its S-parameters (§10.5).

Reads a run directory (`spec.json`, `history.json`, `frames.npz`, optionally `result.json`)
and renders one frame per iteration (evenly subsampled to at most `max_frames`, the last one
held): on the left the projected density ρ̄ of the design window, from the board's substrate
colour to copper, with the feeds; on the right |S_ij| in dB at the objective frequencies with
the requirement limits, and below it the epigraph value t over the iterations so far.

Conventions of `pnr.animate`: Pillow only (imported lazily; `//yapnr/rf:animate`), the
viewer's palette, animated WebP at 800 px within 2.5 MB and GIF at 640 px within 5 MB,
the final frame held, frames rendered lazily, no metadata, deterministic for the same run and Pillow version.

    python -m yapnr.rf.animate RUN_DIR --out run.webp
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
SERIES = ("#e89a73", "#6eb6e5", "#dfbd62", "#aa9de9", "#62ffad", "#ff566b", "#9ee6d1")
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m yapnr.rf.animate", description=__doc__.split("\n")[0]
    )
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="output .webp or .gif")
    ap.add_argument("--max-frames", type=int, default=120)
    args = ap.parse_args(argv)
    info = animate(args.run_dir, args.out, max_frames=args.max_frames)
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
