"""Frame an auto-placed atopile board: an outline, a translation into view, a fitted sheet.

atopile places parts around the origin and draws no board outline, so exports of a code-only
board are a nearly blank A4 page. ``frame`` (``yapnr atopile build --outline-margin-mm M``):

1. takes the bounding box of every footprint placement;
2. moves the footprints and the board's own tracks, arcs, vias and zones (every point of them)
   so the box sits ``margin`` inside the origin; footprint-local geometry and the board's
   graphics stay as they are;
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


_NUMBER = r"(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)"
_HEAD = re.compile(r"\(([A-Za-z_][A-Za-z0-9_]*)")
# What moves in each of the board's own items: every such point in the block, or the first.
_MOVES = {
    "footprint": (("at",), True),
    "segment": (("start", "end"), False),
    "arc": (("start", "mid", "end"), False),
    "via": (("at",), True),
    "zone": (("xy", "start", "mid", "end"), False),
}


def _items(text: str) -> List[Tuple[int, int, str]]:
    """(start, end, head) of the board's own items: the blocks directly inside ``kicad_pcb``."""
    items: List[Tuple[int, int, str]] = []
    depth, index, length, opened = 0, 0, len(text), None
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
            if depth == 2:
                match = _HEAD.match(text, index)
                opened = (index, match.group(1) if match else "")
        elif char == ")":
            if depth == 2 and opened is not None:
                items.append((opened[0], index + 1, opened[1]))
                opened = None
            depth -= 1
        index += 1
    return items


def _points(block: str, names: Tuple[str, ...]) -> "re.Pattern[str]":
    return re.compile(r"\((" + "|".join(names) + r")\s+" + _NUMBER + r"\s+" + _NUMBER)


def _placement(block: str) -> "Tuple[float, float] | None":
    """The footprint's own ``(at x y)``: the first one in its block."""
    match = _points(block, ("at",)).search(block)
    return (float(match.group(2)), float(match.group(3))) if match else None


def _move(block: str, head: str, dx: float, dy: float) -> str:
    names, first_only = _MOVES[head]

    def shift(match: "re.Match[str]") -> str:
        x, y = float(match.group(2)) + dx, float(match.group(3)) + dy
        return f"({match.group(1)} {x:.4f} {y:.4f}"

    return _points(block, names).sub(shift, block, count=1 if first_only else 0)


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
    items = [item for item in _items(text) if item[2] in _MOVES]
    placed = [
        at
        for start, end, head in items
        if head == "footprint"
        for at in [_placement(text[start:end])]
        if at
    ]
    if not placed:
        return text
    xs = [x for x, _y in placed]
    ys = [y for _x, y in placed]
    dx, dy = margin - min(xs), margin - min(ys)
    width = max(xs) - min(xs) + 2 * margin
    height = max(ys) - min(ys) + 2 * margin
    for start, end, head in reversed(items):
        text = text[:start] + _move(text[start:end], head, dx, dy) + text[end:]
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
