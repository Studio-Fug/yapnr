"""Frame an auto-placed atopile board: an outline, a translation into view, a fitted sheet.

atopile places parts around the origin and draws no board outline, so exports of a code-only
board are a nearly blank A4 page. ``frame`` (``yapnr atopile build --outline-margin-mm M``):

1. takes the bounding box of every footprint placement;
2. moves the footprints (and any top-level tracks and vias) so the box sits ``margin`` inside the
   origin;
3. sets a custom sheet (``paper "User" W H``) of the box plus the margins;
4. draws an ``Edge.Cuts`` rectangle just inside the sheet.

It is idempotent: the outline it drew before is recognised by a fixed UUID prefix and replaced
(the imported script's pattern could not match its own lines, so re-runs stacked outlines).
For a real board, draw the outline in the layout and build with ``--frozen``.

Imported from rules_atopile's ``tools/board_outline.py`` (the ``outline_margin_mm`` of its
``atopile_project`` rule) and rewritten as functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

# Fixed UUID prefix of the outline lines, so a re-run replaces them instead of stacking.
SENTINEL = "a70117e0-0000-4000-8000"
# One of the outline's gr_line blocks (parentheses nest two levels deep inside it).
_OWN_LINE = re.compile(
    r"\n[ \t]*\(gr_line\b(?:[^()]|\((?:[^()]|\([^()]*\))*\))*?\(uuid\s+\""
    + re.escape(SENTINEL)
    + r"[^\"]*\"\)\s*\)"
)


def _placements(text: str) -> List[Tuple[int, int, float, float, str]]:
    """(start, end, x, y, rest) of each footprint's own ``(at x y [rot])``."""
    spans = []
    for match in re.finditer(r"\(footprint\b", text):
        at = re.search(
            r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)([^\)]*)\)", text[match.end() : match.end() + 4000]
        )
        if at:
            start, end = match.end() + at.start(), match.end() + at.end()
            spans.append((start, end, float(at.group(1)), float(at.group(2)), at.group(3)))
    return spans


def _outline(x0: float, y0: float, x1: float, y1: float) -> str:
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    lines = []
    for index, ((sx, sy), (ex, ey)) in enumerate(zip(corners, corners[1:])):
        lines.append(
            f"  (gr_line (start {sx:.3f} {sy:.3f}) (end {ex:.3f} {ey:.3f})\n"
            f'    (stroke (width 0.15) (type solid)) (layer "Edge.Cuts")\n'
            f'    (uuid "{SENTINEL}-00000000000{index}")\n'
            f"  )"
        )
    return "\n".join(lines)


def frame(text: str, margin: float) -> str:
    """The board text with an outline ``margin`` mm around its parts (unchanged if none)."""
    text = _OWN_LINE.sub("", text)
    spans = _placements(text)
    if not spans:
        return text
    xs = [s[2] for s in spans]
    ys = [s[3] for s in spans]
    dx, dy = margin - min(xs), margin - min(ys)
    width = max(xs) - min(xs) + 2 * margin
    height = max(ys) - min(ys) + 2 * margin
    for start, end, x, y, rest in sorted(spans, key=lambda s: s[0], reverse=True):
        text = text[:start] + f"(at {x + dx:.4f} {y + dy:.4f}{rest})" + text[end:]

    def shift(match: "re.Match[str]") -> str:
        x, y = float(match.group(2)) + dx, float(match.group(3)) + dy
        return f"{match.group(1)}{x:.4f} {y:.4f}{match.group(4)}"

    # Routed geometry at the top level moves with the parts; footprint-local geometry does not.
    text = re.sub(r"(\(segment\s+\(start\s+)(-?[\d.]+)\s+(-?[\d.]+)(\))", shift, text)
    text = re.sub(r"(\(end\s+)(-?[\d.]+)\s+(-?[\d.]+)(\)\s*\(width)", shift, text)
    text = re.sub(r"(\(via\s+\(at\s+)(-?[\d.]+)\s+(-?[\d.]+)(\))", shift, text)
    text = re.sub(
        r'\(paper\s+"[^"]*"(?:\s+[\d.]+\s+[\d.]+)?\)',
        f'(paper "User" {width:.3f} {height:.3f})',
        text,
        count=1,
    )
    inset = min(margin / 2, 0.5)
    block = _outline(inset, inset, width - inset, height - inset)
    last = text.rstrip().rfind(")")
    return text[:last] + block + "\n" + text[last:]


def frame_file(path: Path, margin: float) -> None:
    path.write_text(frame(path.read_text(encoding="utf-8"), margin), encoding="utf-8")
