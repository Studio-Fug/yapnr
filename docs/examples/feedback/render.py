"""Reproduce synthetic geometry snapshots; no native PCB/routing evidence."""

import json
from pathlib import Path

COLORS = {"R5": "#e8a366", "R6": "#7fcbb8", "Q1": "#909adb"}
SIZES = {"R5": (0.4, 0.4), "R6": (0.4, 0.4), "Q1": (2, 4)}


def render(points, title):
    shapes, labels = [], []
    for ref, (x, y) in points.items():
        w, h = SIZES[ref]
        shapes.append(
            f'<rect x="{140+(x-w/2)*65}" y="{260-(y+h/2)*45}" '
            f'width="{w*65}" height="{h*45}" rx="3" fill="{COLORS[ref]}"/>'
        )
        labels.append(
            f'<text x="{140+x*65}" y="{251-y*45}" text-anchor="middle" fill="white">{ref}</text>'
        )
    for y in (0, 2):
        shapes.append(
            f'<circle cx="140" cy="{260-y*45}" r="18" fill="none" stroke="#d9e995" stroke-dasharray="3 3"/>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="580" height="400" viewBox="0 0 580 400">'
        '<rect width="580" height="400" fill="#152820"/>'
        '<g font-family="sans-serif" font-size="17">'
        + f'<text x="24" y="32" fill="white">{title}</text>'
        '<text x="24" y="60" fill="#b3c5ba" font-size="13">'
        "Synthetic placement · not native PCB validation</text>"
        + "".join(shapes + labels)
        + '<text x="24" y="374" fill="#b3c5ba" font-size="13">'
        "Dashed circles: required R5 and R6 target positions</text></g></svg>\n"
    )


if __name__ == "__main__":
    root = Path(__file__).parent
    for name, title in (
        ("before", "Before · neither resistor reaches its target"),
        ("half", "Partial · R6 still fails"),
        ("after", "After · both resistors reach their targets"),
    ):
        (root / (name + ".svg")).write_text(
            render(json.loads((root / (name + ".json")).read_text()), title)
        )
