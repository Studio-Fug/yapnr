#!/usr/bin/env python3
"""Compare two accepted native board snapshots without interpolating geometry.

Requires optional matplotlib and Pillow. Each input directory contains native
geometry.json (tracks, vias, pads) and the regression runner's result.json.
Keep the same viewport and layer panels across both frames. Geometry can be
exported from pcbnew; these are accepted endpoints, not a placement animation.
"""

import argparse
import io
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-dir", type=Path, required=True)
    parser.add_argument("--after-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.lines import Line2D
    from matplotlib.patches import Circle, Polygon
    from PIL import Image

    snapshots = []
    for folder in (args.before_dir, args.after_dir):
        result = json.loads((folder / "result.json").read_text())
        if not result["passed"] or result["opens"] or result["violations"]:
            raise ValueError("Only accepted native DRC-clean snapshots may be rendered")
        snapshots.append(json.loads((folder / "geometry.json").read_text()))
    colors = {"USB_DP": "#df3434", "USB_DN": "#1875d1", "D_P": "#a42cbe", "D_N": "#128461"}
    points = [
        q
        for data in snapshots
        for t in data["tracks"]
        if t["net"] in colors
        for q in (t["a"], t["z"])
    ]
    xmin, xmax = min(q[0] for q in points) - 3, max(q[0] for q in points) + 3
    ymin, ymax = min(q[1] for q in points) - 3, max(q[1] for q in points) + 3
    frames = []
    for data, title in zip(
        snapshots,
        ("Before · independent matched legs", "After · coupled paths and paired via access"),
    ):
        fig, axes = plt.subplots(1, 2, figsize=(12, 6))
        fig.subplots_adjust(left=0.06, right=0.98, bottom=0.15, top=0.80, wspace=0.12)
        for ax, layer in zip(axes, ("F.Cu", "B.Cu")):
            ax.set_aspect("equal")
            ax.set_xlim(xmin, xmax)
            ax.set_ylim(ymax, ymin)
            fig.canvas.draw()
            scale = (
                abs(ax.transData.transform((1, 0))[0] - ax.transData.transform((0, 0))[0])
                * 72
                / fig.dpi
            )
            for selected in (False, True):
                tracks = [
                    t
                    for t in data["tracks"]
                    if t["layer"] == layer and (t["net"] in colors) == selected
                ]
                ax.add_collection(
                    LineCollection(
                        [[t["a"], t["z"]] for t in tracks],
                        colors=[colors.get(t["net"], "#c1c9d0") for t in tracks],
                        linewidths=[t["width"] * scale for t in tracks],
                        zorder=5 if selected else 1,
                    )
                )
            for pad in data["pads"]:
                if layer in pad["layers"]:
                    ax.add_patch(
                        Polygon(
                            pad["outline"],
                            color=colors.get(pad["net"], "#d0d6dc"),
                            alpha=0.85,
                            zorder=3,
                        )
                    )
            for via in data["vias"]:
                ax.add_patch(
                    Circle(
                        via["pos"],
                        via["diameter"] / 2,
                        color=colors.get(via["net"], "#bac5ce"),
                        zorder=6,
                    )
                )
                ax.add_patch(Circle(via["pos"], via["drill"] / 2, color="white", zorder=7))
            ax.set_title(layer)
            ax.set_xlabel("mm")
            ax.set_ylabel("mm")
        fig.suptitle(title, fontsize=16)
        fig.text(
            0.5,
            0.87,
            "Native accepted copper · legacy MCU USB · seed 1 · DRC: 0 opens / 0 violations",
            ha="center",
        )
        fig.text(
            0.5,
            0.04,
            "Same viewport and rules; placement selected mechanically. Two final snapshots; no intermediate geometry.",
            ha="center",
            fontsize=9,
        )
        fig.legend(
            [Line2D([0], [0], color=color, lw=3) for color in colors.values()],
            list(colors),
            loc="lower center",
            bbox_to_anchor=(0.5, 0.075),
            ncol=4,
            frameon=False,
        )
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=100)
        plt.close(fig)
        buffer.seek(0)
        frames.append(Image.open(buffer).convert("RGB").quantize(colors=128))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.out,
        save_all=True,
        append_images=frames[1:],
        duration=2500,
        loop=0,
        optimize=False,
        disposal=2,
    )


if __name__ == "__main__":
    main()
