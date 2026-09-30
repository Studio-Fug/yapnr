"""Encoders: animated WebP (docs), GIF (README), MP4 (optional, through ffmpeg).

Frames are rendered lazily, one at a time, through :class:`FrameSequence` (a multi-frame
Pillow image), so a long animation never holds all its RGB frames in memory. No metadata is
written (no EXIF, XMP or comment). The GIF uses one fixed palette for all frames (median cut
over evenly sampled frames, no dithering, plus the theme's signal colours, which a short scene
such as the verdict would otherwise lose to the copper colours), so its bytes are deterministic.

:func:`encode` steps down deterministically until an output meets its size budget: WebP
quality 80, 70, 60, then a longer frame interval, then a narrower width; GIF 128, 96, 64
colours, then 100 ms frames, then a narrower width. The chosen settings are returned; an
output still over budget is an error.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageColor

from . import theme

WEBP_STEPS = (
    dict(quality=80, frame_ms=60, width=800),
    dict(quality=70, frame_ms=60, width=800),
    dict(quality=60, frame_ms=60, width=800),
    dict(quality=60, frame_ms=80, width=800),
    dict(quality=60, frame_ms=80, width=720),
    dict(quality=60, frame_ms=80, width=640),
)
GIF_STEPS = (
    dict(colors=128, frame_ms=80, width=640),
    dict(colors=96, frame_ms=80, width=640),
    dict(colors=64, frame_ms=80, width=640),
    dict(colors=64, frame_ms=100, width=640),
    dict(colors=64, frame_ms=100, width=560),
)
PALETTE_SAMPLES = 12
# Kept exactly in every GIF palette: the colours that carry meaning in small areas.
SIGNAL_COLOURS = (theme.FAIL, theme.ACCENT, theme.NEW, theme.PROVISIONAL, theme.TEXT, theme.MUTED)


class FrameSequence(Image.Image):
    """A lazily rendered multi-frame image: frame ``i`` is ``render(i)``."""

    def __init__(self, count, render):
        super().__init__()
        self._count = int(count)
        self._render = render
        self._index = -1
        self.seek(0)

    @property
    def n_frames(self):
        return self._count

    @property
    def is_animated(self):
        return self._count > 1

    def seek(self, frame):
        if frame == self._index:
            return
        if not 0 <= frame < self._count:
            raise EOFError("no such frame")
        image = self._render(frame)
        self.im = image.im
        self._mode = image.mode
        self._size = image.size
        self.palette = image.palette.copy() if image.palette is not None else None
        self.info = {}
        self._index = frame

    def tell(self):
        return self._index


def _durations(frames):
    return [int(ms) for _view, ms in frames]


def webp_bytes(renderer, frames, quality=80, method=4):
    """Animated WebP of ``frames`` ([(view, ms)]) drawn by ``renderer``."""
    sequence = FrameSequence(len(frames), lambda i: renderer.frame(frames[i][0]))
    out = io.BytesIO()
    sequence.save(
        out,
        format="WEBP",
        save_all=True,
        duration=_durations(frames),
        loop=0,
        quality=int(quality),
        method=int(method),
        minimize_size=True,
        lossless=False,
    )
    return out.getvalue()


def gif_palette(renderer, frames, colors):
    """A palette image from evenly sampled frames (median cut, deterministic)."""
    count = min(PALETTE_SAMPLES, len(frames))
    picks = sorted({int(round(i * (len(frames) - 1) / max(1, count - 1))) for i in range(count)})
    first = renderer.frame(frames[picks[0]][0])
    sheet = Image.new("RGB", (first.width, first.height * len(picks)))
    for k, index in enumerate(picks):
        sheet.paste(renderer.frame(frames[index][0]), (0, first.height * k))
    small = sheet.resize((max(1, sheet.width // 2), max(1, sheet.height // 2)), Image.NEAREST)
    fixed = [ImageColor.getrgb(c) for c in SIGNAL_COLOURS]
    count = max(2, int(colors) - len(fixed))
    cut = small.quantize(colors=count, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    raw = cut.getpalette() or []
    entries = [tuple(raw[3 * i : 3 * i + 3]) for i in range(min(count, len(raw) // 3))]
    entries += [rgb for rgb in fixed if rgb not in entries]
    palette = Image.new("P", (1, 1))
    palette.putpalette([v for rgb in entries for v in rgb])
    return palette


def gif_bytes(renderer, frames, colors=128):
    """GIF of ``frames`` with one fixed palette and no dithering."""
    palette = gif_palette(renderer, frames, colors)

    def render(i):
        return renderer.frame(frames[i][0]).quantize(palette=palette, dither=Image.Dither.NONE)

    sequence = FrameSequence(len(frames), render)
    out = io.BytesIO()
    sequence.save(
        out,
        format="GIF",
        save_all=True,
        duration=[max(20, int(round(ms / 10.0)) * 10) for ms in _durations(frames)],
        loop=0,
        optimize=False,
        disposal=1,
    )
    return out.getvalue()


def mp4(renderer, frames, path, frame_ms=60, timeout=600):
    """H.264 MP4 through ffmpeg (``yuv420p``, CRF 23, ``+faststart``); False without ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    from pnr.proc import run_checked

    with tempfile.TemporaryDirectory(prefix="pnr-animate-") as tmp:
        tmp = Path(tmp)
        lines = []
        for i, (view, ms) in enumerate(frames):
            name = "f%05d.png" % i
            renderer.frame(view).save(tmp / name, format="PNG")
            lines += ["file '%s'" % name, "duration %.3f" % (ms / 1000.0)]
        lines.append("file 'f%05d.png'" % (len(frames) - 1))
        (tmp / "frames.txt").write_text("\n".join(lines) + "\n")
        fps = "%.3f" % (1000.0 / frame_ms)
        command = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0"]
        command += ["-i", str(tmp / "frames.txt"), "-vf", "fps=" + fps, "-c:v", "libx264"]
        command += ["-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart"]
        command += ["-map_metadata", "-1", str(tmp / "out.mp4")]
        run_checked(command, timeout=timeout)
        shutil.copyfile(tmp / "out.mp4", path)
    return True


def encode(kind, make, budget_bytes, steps=None):
    """Encode with the first setting of ``steps`` whose output fits ``budget_bytes``.

    ``make(width, frame_ms)`` returns ``(renderer, frames)``. Returns ``(bytes, settings,
    frames, renderer)``; raises ValueError if even the last setting is over budget."""
    steps = steps or (WEBP_STEPS if kind == "webp" else GIF_STEPS)
    last = None
    for setting in steps:
        renderer, frames = make(setting["width"], setting["frame_ms"])
        if kind == "webp":
            data = webp_bytes(renderer, frames, quality=setting["quality"])
        else:
            data = gif_bytes(renderer, frames, colors=setting["colors"])
        last = (data, dict(setting), frames, renderer)
        if len(data) <= budget_bytes:
            return last
    raise ValueError(
        "%s over its size budget even at the last setting: %d > %d bytes"
        % (kind, len(last[0]), budget_bytes)
    )
