#!/usr/bin/env python3
"""Render accepted nest poses of the synthetic half-macro unit-test fixture.

Run with hardware/pnr and hardware/pnr/tests on PYTHONPATH. Requires Pillow
and the engine's Python dependencies. No routing or native DRC is performed.
"""

import argparse
import copy
import os
from unittest import mock

from PIL import Image, ImageDraw, ImageFont
from test_hull_nest import CL, HULL_ON, G, NestTest, constraints_for

from pnr.place import hull
from pnr.place.geometry import courtyard_rect
from pnr.place.metrics import hard_violations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    frames = []
    with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
        graph = NestTest().apart()
        poses = [("Before nesting", copy.deepcopy(graph))]

        def capture(current, step):
            poses.append(("Accepted nest move %d" % step, copy.deepcopy(current)))

        with mock.patch.object(hull, "trace_move", capture):
            report = hull.nest(graph, constraints_for(graph), 30, 20, clearance=CL, grid=G)
        if report.get("undone") or report.get("moved", 0) < 1:
            raise ValueError("Expected accepted fixture moves")
        font = ImageFont.load_default(size=16)
        small = ImageFont.load_default(size=13)

        def box(rect):
            return (
                40 + rect.left * 18,
                450 - rect.top * 18,
                40 + rect.right * 18,
                450 - rect.bottom * 18,
            )

        for label, state in poses:
            metrics = hull.component_nesting(state.components)
            if metrics["hull_collision_mm2"] or any(
                hard_violations(state, constraints_for(state)).values()
            ):
                raise ValueError("Refusing to render an illegal fixture pose")
            image = Image.new("RGB", (640, 540), "#101923")
            draw = ImageDraw.Draw(image)
            draw.text(
                (24, 18), "Hull nesting: synthetic placement fixture", fill="white", font=font
            )
            draw.text((24, 44), label, fill="#b5c9d7", font=font)
            draw.rectangle((40, 90, 580, 450), outline="#678291", width=2)
            for component, colour in zip(state.components, ("#56cee1", "#efba60")):
                draw.rectangle(box(courtyard_rect(component)), outline=colour, width=2)
                for _plane, rect in hull.hull_placement_rects(component):
                    draw.rectangle(box(rect), fill=colour)
                x = (rect.left + rect.right) / 2
                y = (rect.bottom + rect.top) / 2
                draw.text(
                    (40 + x * 18, 450 - y * 18),
                    component.ref,
                    fill="#101923",
                    font=font,
                    anchor="mm",
                )
            draw.text(
                (24, 465),
                "Macro rectangle overlap: %.2f mm2; hull collision: 0"
                % metrics["macro_overlap_mm2"],
                fill="white",
                font=small,
            )
            draw.text(
                (24, 489),
                "Outlines: macro boxes. Filled areas: placement hulls.",
                fill="#b5c9d7",
                font=small,
            )
            draw.text(
                (24, 513),
                "Recorded accepted poses only. No routing or native DRC.",
                fill="#b5c9d7",
                font=small,
            )
            frames.append(image)
    frames[0].save(
        args.out, save_all=True, append_images=frames[1:], duration=1300, loop=0, optimize=True
    )
    print("%d recorded fixture poses; %s" % (len(frames), report["nesting"]))


if __name__ == "__main__":
    main()
