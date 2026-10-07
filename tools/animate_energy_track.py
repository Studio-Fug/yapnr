#!/usr/bin/env python3
"""Render only recorded accepted energy transactions; no interpolated geometry.

Requires the optional plotting tools matplotlib and Pillow. The run directory
is an actual pnr.gloss output containing result.json and work/trials/*/spec.json.
This view is explicitly the synthetic gloss_fixture's sig net on F.Cu.
"""
import argparse
import io
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads((args.run_dir / "result.json").read_text())
    if (
        result["objective_before"] != [0] * 6
        or result["objective_after"] != [0] * 6
        or not result["end_gate"]["passed"]
    ):
        raise ValueError("This animation requires a native-clean, phase-end-passed synthetic run")
    records = []
    for transaction in result["transactions"]:
        if not transaction["accepted"]:
            continue
        if transaction.get("objective") != [0] * 6 or transaction.get("reasons"):
            raise ValueError("Every shown transaction must have a verified clean native objective")
        number = transaction["transaction"]
        specs = json.loads(
            (args.run_dir / "work/trials" / f"{number:03d}" / "spec.json").read_text()
        )["specs"]
        for spec in specs:
            if spec["net"] == "sig" and spec["layer"] == "F.Cu" and spec.get("energy"):
                if records and records[-1]["points"] != spec["old_nm"]:
                    raise ValueError("Recorded transactions do not form a continuous path history")
                if not records:
                    records.append(
                        dict(
                            label="Original legal route",
                            points=spec["old_nm"],
                            length=spec["metrics"]["L"],
                            bends=spec["metrics"]["B"],
                        )
                    )
                records.append(
                    dict(
                        label=f"Accepted transaction {number}",
                        points=spec["new_nm"],
                        length=spec["metrics"]["L_new"],
                        bends=spec["metrics"]["B_new"],
                    )
                )
    if len(records) < 2:
        raise ValueError("No accepted energy transitions to show")
    import matplotlib
    from matplotlib import pyplot as plt
    from PIL import Image

    matplotlib.use("Agg")
    frames = []
    for index, row in enumerate(records):
        fig, ax = plt.subplots(figsize=(9, 4.8), dpi=100)
        fig.patch.set_facecolor("#101923")
        ax.set_facecolor("#172431")
        original = [(x / 1e6, y / 1e6) for x, y in records[0]["points"]]
        points = [(x / 1e6, y / 1e6) for x, y in row["points"]]
        ax.plot(*zip(*original), color="#ffc65c", lw=1.5, ls="--", alpha=0.3)
        ax.plot(
            *zip(*points), color="#54d4e8" if index else "#ffc65c", lw=4, solid_capstyle="round"
        )
        ax.scatter(
            [points[0][0], points[-1][0]],
            [points[0][1], points[-1][1]],
            s=60,
            facecolor="#172431",
            edgecolor="#54d4e8",
            lw=2,
            zorder=5,
        )
        # The foreign X1.1 rectangular pad from tests/gloss_fixture.py.
        ax.add_patch(plt.Rectangle((10.2, 9.7), 0.6, 0.6, facecolor="#b5bfce", edgecolor="none"))
        ax.annotate(
            "Fixed foreign pad",
            xy=(10.5, 10.3),
            xytext=(9, 12),
            color="#b7d0df",
            fontsize=10,
            arrowprops={"arrowstyle": "->", "color": "#b7d0df"},
        )
        ax.set_xlim(3, 27)
        ax.set_ylim(8.4, 12.7)
        ax.set_aspect("equal")
        ax.grid(color="white", alpha=0.08)
        ax.tick_params(colors="#a0b8c9")
        ax.spines[["top", "right", "bottom", "left"]].set_visible(False)
        ax.set_xlabel("x (mm)", color="#a0b8c9")
        ax.set_ylabel("y (mm)", color="#a0b8c9")
        fig.text(
            0.07, 0.91, "Energy optimization through the actual engine", color="white", fontsize=18
        )
        fig.text(
            0.07,
            0.84,
            "Synthetic native fixture · sig / F.Cu · fixed viewport and endpoints",
            color="#b7d0df",
            fontsize=11,
        )
        fig.text(
            0.07, 0.72, f'{index}/{len(records)-1}  {row["label"]}', color="#54d4e8", fontsize=13
        )
        fig.text(
            0.07,
            0.20,
            f'Length: {row["length"]:.6f} mm     Bends: {row["bends"]}',
            color="white",
            fontsize=12,
        )
        fig.text(
            0.07,
            0.12,
            "Cold KiCad DRC: clean before and after every shown accepted transaction.",
            color="#b7d0df",
            fontsize=10,
        )
        fig.text(
            0.07,
            0.065,
            "Recorded states only; no simulated motion. This is not radar/RF validation.",
            color="#f0c66b",
            fontsize=10,
        )
        fig.subplots_adjust(left=0.07, right=0.97, top=0.68, bottom=0.30)
        data = io.BytesIO()
        fig.savefig(data, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        data.seek(0)
        frames.append(Image.open(data).convert("RGB").quantize(colors=96))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.out,
        save_all=True,
        append_images=frames[1:],
        duration=[2200] + [1800] * (len(frames) - 2) + [3000],
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(
        json.dumps(
            dict(
                frames=len(frames),
                native_clean=True,
                source="synthetic gloss_fixture accepted transactions",
                length_before=records[0]["length"],
                length_after=records[-1]["length"],
                bends_before=records[0]["bends"],
                bends_after=records[-1]["bends"],
            )
        )
    )


if __name__ == "__main__":
    main()
