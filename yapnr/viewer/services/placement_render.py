"""Headless SVG of recorded placement geometry; no synthetic routing or solver frames."""

from html import escape

from .placement_airwires import spans


def render(graph, decision=None, alternative=None, airwires=True, surface=None, rotation=0):
    outline = graph.get("outline", {})
    width, height = float(outline.get("width", 30)), float(outline.get("height", 30))
    margin = max(width, height) * 0.07
    font = max(width, height) * 0.018
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="900" '
        f'viewBox="{-margin} {-height-margin*2} {width+margin*2} {height+margin*4}">',
        '<rect x="-100000" y="-100000" width="200000" height="200000" fill="#101712"/>',
        f'<g font-family="monospace" font-size="{font}" fill="#b8c7bd">',
        f'<text x="0" y="{-height-margin}">Recorded placement · copper unverified</text>',
    ]
    polygon = outline.get("polygon") or [[0, 0], [width, 0], [width, height], [0, height]]
    points = " ".join(f"{float(x)},{-float(y)}" for x, y in polygon)
    svg.append(
        f'<polygon points="{points}" fill="none" stroke="#49614f" stroke-width="{font*.15}"/>'
    )
    if surface:
        field = next(f for f in surface["fields"] if f["rotation"] == rotation)
        extent = surface["maximum"] - surface["minimum"]
        pitch = surface["pitch_mm"]
        for i, value in enumerate(field["values"]):
            t = (value - surface["minimum"]) / extent if extent else 0.5
            x = field["origin"][0] + i % field["nx"] * pitch - pitch / 2
            y = field["origin"][1] + i // field["nx"] * pitch + pitch / 2
            svg.append(
                f'<rect x="{x}" y="{-y}" width="{pitch}" height="{pitch}" '
                f'fill="hsl({210-170*t},75%,50%)" opacity=".48"/>'
            )
        caption = f'{surface["ref"]} · {rotation}° · {surface["expression"]} · iteration {surface["frame_step"]}'
        svg.append(f'<text x="0" y="{-height-font}">{escape(caption)}</text>')

    if airwires:
        for net in spans(graph, decision)["nets"]:
            for edge in net["edges"]:
                x, y = edge["start"]
                u, v = edge["end"]
                svg.append(
                    f'<path d="M{x},{-y} L{u},{-v}" stroke="#78b5c4" '
                    f'stroke-opacity=".5" stroke-width="{font*.10}">'
                    f'<title>{escape(net["name"])} · {edge["length_mm"]:.3f} mm</title></path>'
                )

    def part(c, pos, rot, color, opacity=1, dashed=False):
        x, y = map(float, pos)
        w, h = map(float, c.get("courtyard") or c.get("bbox") or [1, 1])
        dash = f' stroke-dasharray="{font*.5} {font*.3}"' if dashed else ""
        svg.append(
            f'<g opacity="{opacity}" transform="translate({x} {-y}) rotate({-float(rot)})" '
            f'stroke="{color}" stroke-width="{font*.12}">'
        )
        svg.append(f'<rect x="{-w/2}" y="{-h/2}" width="{w}" height="{h}" fill="none"{dash}/>')
        for p in c.get("pads", []):
            px, py = map(float, p["offset"])
            pw, ph = map(float, p.get("size") or [0.3, 0.3])
            svg.append(
                f'<rect x="{px-pw/2}" y="{-py-ph/2}" width="{pw}" height="{ph}" fill="{color}" stroke="none"/>'
            )
        svg.append("</g>")
        svg.append(
            f'<text x="{x}" y="{-y-h/2-font*.3}" fill="{color}" opacity="{opacity}" '
            f'text-anchor="middle">{escape(c["ref"])}</text>'
        )

    placed = set(decision.get("stationary_neighbors", [])) if decision else None
    moves = {m["ref"]: m for m in (decision or {}).get("moves", [])}
    for c in graph.get("components", []):
        if c["ref"] in moves:
            move = moves[c["ref"]]
            part(c, move["before"], move["before_rotation"], "#f1ba63", dashed=True)
            part(c, move["after"], move["rotation"], "#b1eb73")
            x, y = move["before"]
            u, v = move["after"]
            svg.append(
                f'<path d="M{x},{-y} L{u},{-v}" stroke="#f1ba63" stroke-width="{font*.12}"/>'
            )
        elif decision and c["ref"] == decision["ref"]:
            part(
                c,
                decision.get("original_position", decision["target"]),
                decision.get("original_rotation", 0),
                "#f1ba63",
                dashed=True,
            )
            part(c, decision["position"], decision["rotation"], "#b1eb73")
            if alternative:
                part(c, alternative["position"], alternative["rotation"], "#c49aec", dashed=True)
        elif surface and c["ref"] == surface["ref"]:
            part(c, c["pos"], c.get("rot", 0), "#f1ba63")
            part(c, c["pos"], rotation, "#c49aec", dashed=True)
        else:
            part(
                c,
                c["pos"],
                c.get("rot", 0),
                "#9baea0",
                1 if placed is None or c["ref"] in placed else 0.2,
            )
    legend = "Amber: original · Green: chosen · Purple: alternative"
    if surface:
        legend = f'Blue: lower · Amber: higher · shared scale {surface["minimum"]:.2f}–{surface["maximum"]:.2f}'
        x, y = field["best_position"]
        svg.append(
            f'<path d="M{x-font},{-y} H{x+font} M{x},{-y-font} V{-y+font}" '
            f'stroke="#b1eb73" stroke-width="{font*.2}"/>'
        )
        svg.append(
            f'<text x="0" y="{margin+font*1.5}">Rigid-pose slice · '
            "Green cross: sampled minimum · no legality claim</text>"
        )
    svg.append(f'<text x="0" y="{margin}">{legend}</text></g></svg>')
    return "\n".join(svg)
