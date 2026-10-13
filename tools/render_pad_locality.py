"""Reproducible placement evidence; dashed links are measurements, never copper."""

import argparse
import json
import math
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
from PIL import Image

matplotlib.use("Agg")

parser = argparse.ArgumentParser(description="Render the actual recorded pad-locality A/B layouts")
parser.add_argument("data", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
out = args.output
out.mkdir(parents=True, exist_ok=True)
rows = json.loads(args.data.read_text())
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "text.color": "#e1e8db",
        "axes.labelcolor": "#a4b6a3",
        "xtick.color": "#a4b6a3",
        "ytick.color": "#a4b6a3",
    }
)


def transform(c, p):
    t = math.radians(c["rot"])
    x, y = p
    return c["pos"][0] + x * math.cos(t) - y * math.sin(t), c["pos"][1] + x * math.sin(
        t
    ) + y * math.cos(t)


def view(ax, flag, seed):
    stats = next(
        r for r in rows if r["case"] == "converter" and r["flag"] == str(flag) and r["seed"] == seed
    )
    g = stats["graph"]
    ax.set(
        facecolor="#111a14", xlim=(0, 30), ylim=(0, 30), aspect="equal", xlabel="mm", ylabel="mm"
    )
    ax.grid(alpha=0.1)
    ax.tick_params(labelsize=8)
    for c in g["components"]:
        color = "#b1eb73" if c["ref"] == "C9" else "#e3b776" if c["ref"] == "U5" else "#758d77"
        w, h = c["courtyard"]
        ax.add_patch(
            Polygon(
                [
                    transform(c, p)
                    for p in [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
                ],
                facecolor=color + "16",
                edgecolor=color,
                linewidth=0.7,
            )
        )
        for p in c["pads"]:
            w, h = p["size"]
            x, y = p["offset"]
            ax.add_patch(
                Polygon(
                    [
                        transform(c, (x + dx, y + dy))
                        for dx, dy in [
                            (-w / 2, -h / 2),
                            (w / 2, -h / 2),
                            (w / 2, h / 2),
                            (-w / 2, h / 2),
                        ]
                    ],
                    color=color,
                    alpha=0.85,
                )
            )
        ax.text(
            *c["pos"],
            c["ref"],
            fontsize=7,
            ha="center",
            va="center",
            color="white" if c["ref"] in ("C9", "U5") else "#cad4c8",
            bbox=dict(facecolor="#101911", edgecolor="none", pad=0.5, alpha=0.7),
        )
    a = stats["audit"]["links"]
    for link in [link for link in a if link["member"] == "C9"]:
        x, y = link["a"]
        u, v = link["b"]
        ax.plot([x, u], [y, v], color="#e882c1", lw=2, linestyle="--")
        ax.annotate(
            f"{link['distance_mm']:.2f} mm",
            xy=((x + u) / 2, (y + v) / 2),
            xytext=(5, 10 if link["pad"] == "1" else -15),
            textcoords="offset points",
            fontsize=8,
            color="#f1b8db",
            bbox=dict(facecolor="#101911", edgecolor="none", pad=2),
        )
    title = ("Baseline" if flag == 0 else "Candidate · EXPERIMENTAL") + f" / seed {seed}"
    ax.set_title(title, fontsize=13, color="#e1e8db", pad=12)
    ax.text(
        0.02,
        -0.12,
        f"All-part translation: {stats['motion']['sum_mm']:.1f} mm   |   Power placement cost: {stats['q']:.1f}",
        transform=ax.transAxes,
        fontsize=9,
    )


fig, axes = plt.subplots(1, 2, figsize=(14, 7.5), facecolor="#162019")
view(axes[0], 0, 0)
view(axes[1], 1, 0)
fig.suptitle("C9 pad-locality A/B — actual engine-generated placement", fontsize=17, y=0.97)
fig.text(
    0.5,
    0.02,
    (
        "Same input, seed and viewport. Dashed lines are pad-distance measurements, not copper.\n"
        "Shorter BOOT2 connection; greater legalization displacement. Native routing remains unvalidated."
    ),
    ha="center",
    fontsize=10,
    color="#b9c9b5",
)
fig.subplots_adjust(bottom=0.22, top=0.88, wspace=0.18)
fig.savefig(out / "converter-placement-ab.png", dpi=145, facecolor=fig.get_facecolor())
plt.close(fig)
frames = []
for flag in (0, 1):
    fig, ax = plt.subplots(figsize=(7, 7.6), facecolor="#162019")
    view(ax, flag, 0)
    fig.text(0.5, 0.04, "Actual placements · geometry only · unqualified", ha="center", fontsize=10)
    fig.subplots_adjust(bottom=0.18, top=0.9)
    p = out / f"frame-{flag}.png"
    fig.savefig(p, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    frames.append(Image.open(p).convert("RGB"))
frames[0].save(
    out / "converter-placement-ab.gif",
    save_all=True,
    append_images=frames[1:],
    duration=1800,
    loop=0,
)
print(out)
