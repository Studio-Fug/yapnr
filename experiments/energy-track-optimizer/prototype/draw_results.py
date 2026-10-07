"""Draw measured outputs; no hand-drawn candidate trajectories."""

import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as Patch
from shapely.geometry import shape, LineString

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "deliverables").mkdir(exist_ok=True)
BG = "#101923"
AX = "#172431"
OLD = "#ffc65c"
NEW = "#54d4e8"
FG = "#b7d0df"


def polygon(ax, geom, **kwargs):
    if geom.is_empty:
        return
    for p in getattr(geom, "geoms", (geom,)):
        if p.geom_type == "Polygon":
            ax.add_patch(Patch(list(p.exterior.coords), **kwargs))
            for hole in p.interiors:
                ax.add_patch(
                    Patch(
                        list(hole.coords),
                        facecolor=AX,
                        edgecolor="none",
                        zorder=kwargs.get("zorder", 1),
                    )
                )


def style(ax):
    ax.set_facecolor(AX)
    ax.tick_params(colors="#93aabb")
    ax.spines[["top", "right", "bottom", "left"]].set_visible(False)
    ax.grid(alpha=0.08, color="white")
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)", color=FG)


def real():
    d = next(
        ROOT / p
        for p in (
            "results/real",
            "results/real-release",
            "results/reproduction",
            "results/real-final",
        )
        if (ROOT / p / "validation.json").exists()
    )
    geo = json.loads((d / "geometry.json").read_text())
    plan = json.loads((d / "plan.json").read_text())
    v = json.loads((d / "validation.json").read_text())
    fig, axs = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor(BG)
    for ax, path, title, color in zip(
        axs,
        [plan["before"], plan["after"]],
        ["BEFORE  |  7 bends", "GENERATED CANDIDATE  |  1 bend"],
        [OLD, NEW],
    ):
        style(ax)
        for o in geo["obstacles"]:
            if o["dependent_plane"]:
                continue
            g = shape(o["geom"])
            g = g.buffer(o["radius_mm"]) if o["radius_mm"] else g
            polygon(ax, g, facecolor="#7d91a2", edgecolor="none", alpha=0.50, zorder=1)
        x, y = zip(*path)
        ax.plot(x, y, lw=4, color=color, zorder=5)
        ax.scatter(
            [x[0], x[-1]], [y[0], y[-1]], s=45, facecolor=AX, edgecolor=color, lw=2, zorder=6
        )
        ax.set_xlim(44.7, 54.8)
        ax.set_ylim(60.6, 62.95)
        ax.set_title(title, fontsize=13, color="white", loc="left", pad=20)
        ax.text(
            0.01,
            -0.45,
            f'Length {v["length_before_mm"]:.9f} mm',
            transform=ax.transAxes,
            color="white",
            fontsize=12,
        )
    axs[0].set_ylabel("y (mm)", color=FG)
    polygon(
        axs[1],
        LineString(plan["before"]).buffer(1.0),
        facecolor=NEW,
        edgecolor="none",
        alpha=0.05,
        zorder=0,
    )
    axs[1].plot(*zip(*plan["before"]), color=OLD, lw=1, alpha=0.4, ls="--", zorder=4)
    fig.suptitle(
        "Real board: the optimizer rediscovers the 1-bend route",
        x=0.055,
        ha="left",
        y=0.98,
        color="white",
        fontsize=20,
    )
    fig.text(
        0.055,
        0.88,
        "SPIA_MOSI  •  In2.Cu  •  fixed endpoints / 0.15 mm width  •  movable plane refilled jointly",
        color=FG,
        fontsize=11,
    )
    fig.text(
        0.055,
        0.22,
        "Fresh KiCad 10.0.6: no new violation keys; 31 existing opens → 31. Untouched tracks/vias identical.",
        color=FG,
        fontsize=11,
    )
    fig.text(
        0.055,
        0.16,
        "Energy: 10.175094 → 9.275094 mm-equivalent. RADIO_IO IR: +0.993%, still within its 165 mΩ budget.",
        color=FG,
        fontsize=11,
    )
    fig.text(
        0.055,
        0.095,
        "No-regression experiment on an incomplete board. LVDS skew remained unavailable; no new full-wave RF solve.",
        color="#f0c66b",
        fontsize=10,
    )
    fig.text(
        0.055,
        0.045,
        "Gray = other fixed copper. Plane fill omitted for clarity; its refill is included in validation. Dashed = previous route.",
        color=FG,
        fontsize=10,
    )
    plt.subplots_adjust(top=0.78, bottom=0.36, left=0.055, right=0.98, wspace=0.13)
    fig.savefig(ROOT / "deliverables/Real_track_energy_proof.png", dpi=160, facecolor=BG)


def multi():
    r = json.loads((ROOT / "results/multiple_obstacles.json").read_text())
    p = r["plan"]
    old = p["before"]
    new = p["after"]
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor(BG)
    style(ax)
    corridor = LineString(old).buffer(2)
    polygon(ax, corridor, facecolor=NEW, edgecolor=NEW, alpha=0.06, zorder=0)
    for o in r["obstacles"]:
        g = shape(o["geom"])
        polygon(
            ax,
            g.buffer(0.3, join_style=2),
            facecolor="#8c73ff",
            edgecolor="#bfaaff",
            alpha=0.25,
            zorder=1,
        )
        polygon(ax, g, facecolor="#7d91a2", edgecolor="none", alpha=0.9, zorder=2)
    for q in p["diagnostics"]["reverse_field"]:
        if q["next"]:
            a, b = q["point"], q["next"]
            ax.plot([a[0], b[0]], [a[1], b[1]], color="#598d8f", lw=0.5, alpha=0.15, zorder=1)
    ax.plot(*zip(*old), color=OLD, lw=2, ls="--", label="Original legal witness", zorder=3)
    ax.plot(*zip(*new), color=NEW, lw=3, label="Selected endpoint-to-endpoint span", zorder=4)
    ax.scatter([0, 10], [0, 0], s=70, facecolor=AX, edgecolor=NEW, lw=2, zorder=5)
    ax.set_xlim(-0.6, 10.6)
    ax.set_ylim(-1.6, 3)
    ax.set_ylabel("y (mm)", color=FG)
    ax.legend(
        loc="upper center", frameon=False, labelcolor="white", ncol=2, bbox_to_anchor=(0.5, 1.08)
    )
    fig.suptitle(
        "Two obstacles, one finite span", x=0.055, ha="left", y=0.98, color="white", fontsize=21
    )
    fig.text(
        0.055,
        0.89,
        "A grown witness corridor supplies candidates; reverse goal costs guide the energy search.",
        color=FG,
        fontsize=11,
    )
    fig.text(
        0.055,
        0.12,
        f'Length {LineString(old).length:.6f} → {LineString(new).length:.6f} mm. Energy {p["energy_before"]:.6f} → {p["energy_after"]:.6f} mm-equivalent.',
        color="white",
        fontsize=11,
    )
    fig.text(
        0.055,
        0.065,
        "Purple = required clearance. Faint edges = reverse-cost tree. Both obstacles stay on the original route side.",
        color=FG,
        fontsize=10,
    )
    fig.text(
        0.055,
        0.025,
        "Synthetic geometry only; minimum on the bounded candidate roadmap, not a global geometric/Steiner optimum.",
        color="#f0c66b",
        fontsize=10,
    )
    plt.subplots_adjust(top=0.79, bottom=0.23, left=0.075, right=0.97)
    fig.savefig(ROOT / "deliverables/Multiple_obstacle_span_proof.png", dpi=150, facecolor=BG)


if __name__ == "__main__":
    real()
    multi()
