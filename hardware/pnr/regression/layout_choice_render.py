"""Render actual synthetic study states (requires Pillow; no interpolated frames)."""

import argparse
import hashlib
import json
import math
from pathlib import Path


def main():
    from PIL import Image, ImageDraw, ImageFont

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("study", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--font", required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(a.font, 16)
    small = ImageFont.truetype(a.font, 12)
    cases = ["bank_shapes", "crowded_library"]
    frames = []

    def load(case, method, file):
        return json.loads((a.study / f"{case}-{method}-s0" / file).read_text())

    def render(case, graph, label):
        im = Image.new("RGB", (1100, 650), "#101712")
        d = ImageDraw.Draw(im)
        d.text(
            (20, 15),
            "Synthetic parent placement: " + case.replace("_", " "),
            font=font,
            fill="#e2eade",
        )
        d.text((20, 45), "A: shared-template control", font=font, fill="#b1eb73")
        d.text((570, 45), "B: contextual choice + diversity", font=font, fill="#b1eb73")
        for offset, g in [(0, load(case, "control", "placed.json")), (550, graph)]:
            width, height = g["outline"]["width"], g["outline"]["height"]
            scale = min(470 / width, 470 / height)

            def pt(p):
                return offset + 40 + p[0] * scale, 560 - p[1] * scale

            d.rectangle([pt((0, height)), pt((width, 0))], outline="#607669", width=2)
            pins = {}
            for c in g["components"]:
                th = math.radians(c["rot"])
                co, si = math.cos(th), math.sin(th)
                for p in c["pads"]:
                    x, y = p["offset"]
                    pins[(c["ref"], p["name"])] = (
                        c["pos"][0] + x * co - y * si,
                        c["pos"][1] + x * si + y * co,
                    )
            for net in g["nets"]:
                points = [pins[tuple(p)] for p in net["pins"] if tuple(p) in pins]
                if not points:
                    continue
                tree = [points.pop(0)]
                while points:
                    _, i, j = min(
                        (math.dist(p, q), i, j)
                        for i, p in enumerate(tree)
                        for j, q in enumerate(points)
                    )
                    d.line([pt(tree[i]), pt(points[j])], fill="#4b7160", width=1)
                    tree.append(points.pop(j))
            for c in g["components"]:
                th = math.radians(c["rot"])
                co, si = math.cos(th), math.sin(th)
                x, y = c["pos"]
                w, h = c["courtyard"]

                def pose(dx, dy):
                    return pt((x + dx * co - dy * si, y + dx * si + dy * co))

                d.polygon(
                    [
                        pose(-w / 2, -h / 2),
                        pose(w / 2, -h / 2),
                        pose(w / 2, h / 2),
                        pose(-w / 2, h / 2),
                    ],
                    fill="#24352a",
                    outline="#b1eb73",
                )
                for p in c["pads"]:
                    px, py = pt(pins[(c["ref"], p["name"])])
                    d.rectangle((px - 2, py - 2, px + 2, py + 2), fill="#f1ba63")
                px, py = pt(c["pos"])
                d.text((px - 12, py - 18), c["ref"], font=small, fill="#f4f7ef")
        d.text((20, 595), label, font=font, fill="#e2eade")
        d.text(
            (20, 622),
            "Seed 0 · actual accepted states · spans are airwires · placement-only evidence",
            font=small,
            fill="#f1ba63",
        )
        return im

    for case in cases:
        events = load(case, "diverse", "search.json")["layout_search"]["accepted"]
        for i, event in enumerate(events):
            frames.append(
                render(
                    case,
                    event["graph"],
                    f'{i+1}/{len(events)}: {event["stage"]} · cost {event["cost"]:.2f}',
                )
            )
    frames[0].save(
        a.out / "layout-choice.gif",
        save_all=True,
        append_images=frames[1:],
        duration=[1600] * len(frames),
        loop=0,
        optimize=True,
    )
    frames[-1].save(a.out / "layout-choice.png")
    manifest = dict(
        scope="Actual synthetic placements; no interpolation or electrical qualification",
        seed=0,
        cases=cases,
        frames=len(frames),
        font=Path(a.font).name,
        inputs={
            f"{c}/{m}": hashlib.sha256(
                (a.study / f"{c}-{m}-s0/search.json").read_bytes()
            ).hexdigest()
            for c in cases
            for m in ["control", "diverse"]
        },
        images={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in a.out.glob("layout-choice.*")
            if p.suffix in (".gif", ".png")
        },
    )
    (a.out / "layout-choice-animation.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
