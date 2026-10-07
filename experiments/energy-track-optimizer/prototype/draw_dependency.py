import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from prototype.energy_track import Config, Controller, Track, length

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "deliverables").mkdir(exist_ok=True)
a = Track("b_blocker", "A", ((3, -2), (3, 1), (7, 1), (7, -2)))
b = Track("a_waiting", "B", ((0, 0), (1.4, 1.4), (8.6, 1.4), (10, 0)))
r = Controller([a, b], config=Config(growth_mm=3.2, seconds=30, max_nodes=140)).run()
(ROOT / "results/dependency.json").write_text(json.dumps(r, indent=2) + "\n")
stages = [
    (a.points, b.points),
    (r["tracks"]["b_blocker"], b.points),
    (r["tracks"]["b_blocker"], r["tracks"]["a_waiting"]),
]
fig, axs = plt.subplots(1, 3, figsize=(15, 5.6), sharex=True, sharey=True)
fig.patch.set_facecolor("#101923")
colors = {"A": "#ffc65c", "B": "#54d4e8"}
for k, (ax, (pa, pb)) in enumerate(zip(axs, stages)):
    ax.set_facecolor("#172431")
    for name, path in [("A", pa), ("B", pb)]:
        x, y = zip(*path)
        ax.plot(x, y, color=colors[name], lw=7, alpha=0.12, solid_capstyle="round")
        ax.plot(x, y, color=colors[name], lw=2.8, solid_capstyle="round")
        ax.scatter(
            [x[0], x[-1]],
            [y[0], y[-1]],
            facecolor="#172431",
            edgecolor=colors[name],
            linewidth=2,
            s=48,
            zorder=5,
        )
    ax.set_title(
        ["1  B cannot improve yet", "2  A moves; B is requeued", "3  B now becomes straight"][k],
        fontsize=12,
        color="white",
        pad=18,
        loc="left",
    )
    ax.set_xlim(-0.6, 10.6)
    ax.set_ylim(-3.1, 2.3)
    ax.set_aspect("equal")
    ax.grid(alpha=0.10, color="white")
    ax.tick_params(colors="#93aabb")
    ax.spines[["top", "right", "bottom", "left"]].set_visible(False)
    ax.set_xlabel("x (mm)", color="#93aabb")
    ax.text(
        0.02,
        -0.34,
        f"A: {length(pa):.3f} mm    B: {length(pb):.3f} mm",
        transform=ax.transAxes,
        color="white",
        fontsize=11,
    )
axs[0].set_ylabel("y (mm)", color="#93aabb")
axs[0].annotate(
    "0.20 mm copper gap",
    xy=(5, 1.2),
    xytext=(4.6, 0.1),
    fontsize=9,
    color="white",
    arrowprops={"arrowstyle": "->", "color": "white"},
)
axs[1].plot(*zip(*a.points), color=colors["A"], ls="--", lw=1, alpha=0.35)
axs[2].plot(*zip(*b.points), color=colors["B"], ls="--", lw=1, alpha=0.35)
fig.suptitle(
    "A local improvement unlocks the next track",
    color="white",
    fontsize=21,
    x=0.055,
    ha="left",
    y=0.98,
)
fig.text(
    0.055,
    0.89,
    "Synthetic exact-distance geometry test  •  2 accepted moves  •  4 planning attempts  •  converged",
    color="#b7d0df",
    fontsize=11,
)
fig.text(
    0.055,
    0.07,
    "Fixed endpoints, net, layer and 0.20 mm width. Hard 0.20 mm edge-to-edge clearance. No length matching or pair routing in this case.",
    color="#b7d0df",
    fontsize=10,
)
fig.text(
    0.055,
    0.025,
    "Geometry-only proof: no KiCad/native-DRC or electrical claim. Dashed traces show the previous path.",
    color="#f0c66b",
    fontsize=10,
)
plt.subplots_adjust(top=0.80, bottom=0.22, wspace=0.12, left=0.055, right=0.98)
fig.savefig(
    ROOT / "deliverables/Track_neighbor_requeue_proof.png", dpi=150, facecolor=fig.get_facecolor()
)
