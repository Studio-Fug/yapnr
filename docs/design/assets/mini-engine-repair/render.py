"""Render frozen native copper comparisons: Python + matplotlib 3.10.1 + Pillow.

Run from any directory. No solver or selection is rerun. Inputs are exported native
geometry with exact original board hashes; views use identical extents per module.
"""

import io
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon
from PIL import Image

plt.switch_backend("Agg")

ROOT = Path(__file__).parent
COLORS = {"F.Cu": "#e77e68", "In1.Cu": "#e9bc59", "In2.Cu": "#78c6a8", "B.Cu": "#7faeea"}


def load(name):
    return json.loads((ROOT / (name + ".json")).read_text())


def render(doc, title, bounds, wires=None):
    fig, ax = plt.subplots(figsize=(7, 7), facecolor="#101820")
    ax.set_facecolor("#16232b")
    ax.set_xlim(bounds[:2])
    ax.set_ylim(bounds[2:])
    ax.set_aspect("equal")
    ax.tick_params(colors="#afbac4")
    for t in doc["tracks"]:
        ax.plot(*zip(t[2], t[3]), color=COLORS[t[1]], lw=max(0.6, t[4] * 12))
    for part in doc["parts"]:
        for pad in part["pads"]:
            for poly in pad.get("polys", []):
                ax.add_patch(Polygon(poly, facecolor="#ced3bf", edgecolor="#16232b", lw=0.3))
        ax.text(*part["xy"], part["ref"], color="white", fontsize=8, ha="center")
    for via in doc["vias"]:
        ax.add_patch(Circle(via["xy"], via["diameter"] / 2, color="#a1b8c9"))
    if wires is None:
        wires = [(o["points"][0], o["points"][1]) for o in doc["opens"]]
    for a, b in wires:
        ax.plot(*zip(a, b), color="#ffed55", ls="--", lw=0.8)
    ax.set_title(title, color="white", fontsize=13)
    fig.text(
        0.5,
        0.025,
        "Saved copper · top projection · planes hidden for trace visibility\n"
        "Yellow = missing links (unless labeled legacy overlay) · electrically unqualified",
        color="#d7e0e7",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.055, 1, 1))
    fig.canvas.draw()
    scale = ax.get_window_extent().width / (bounds[1] - bounds[0]) * 72 / fig.dpi
    for line, track in zip(ax.lines, doc["tracks"]):
        line.set_linewidth(track[4] * scale)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def extent(docs):
    points = [
        p
        for d in docs
        for f in d["parts"]
        for pad in f["pads"]
        for poly in pad.get("polys", [])
        for p in poly
    ]
    return [
        min(p[0] for p in points) - 2,
        max(p[0] for p in points) + 2,
        min(p[1] for p in points) - 2,
        max(p[1] for p in points) + 2,
    ]


def animate(name, frames):
    frames[0].save(
        ROOT / (name + ".gif"),
        save_all=True,
        append_images=frames[1:],
        duration=2200,
        loop=0,
        optimize=True,
    )


def legacy_wires(doc):
    nets = {}
    for part in doc["parts"]:
        for pad in part["pads"]:
            if pad["net"]:
                nets.setdefault(pad["net"], []).append(pad["xy"])
    wires = []
    for points in nets.values():
        tree, rest = points[:1], points[1:]
        while rest:
            _, i, a = min(
                ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2, i, q)
                for i, p in enumerate(rest)
                for q in tree
            )
            b = rest.pop(i)
            tree.append(b)
            wires.append((a, b))
    return wires


if __name__ == "__main__":
    for module, arm in [("converter", "branches"), ("pd", "strict channels")]:
        before, after = load(module + "-control"), load(module)
        bounds = extent([before, after])
        frames = [
            render(
                d,
                f"{module.upper()} · {label} · seed {d['seed']}\n"
                f"{len(d['opens'])} native opens",
                bounds,
            )
            for d, label in [(before, "legacy"), (after, arm)]
        ]
        animate(module + "-comparison", frames)
    doc = load("converter")
    animate(
        "airwires-comparison",
        [
            render(
                doc,
                "BEFORE · legacy overlay reconnects every pad",
                extent([doc]),
                legacy_wires(doc),
            ),
            render(
                doc,
                "AFTER · 9 disconnected copper-group links",
                extent([doc]),
                [(w["a"], w["b"]) for w in doc["airwires"]],
            ),
        ],
    )
