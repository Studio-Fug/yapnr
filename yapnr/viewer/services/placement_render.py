"""Headless SVG of recorded placement geometry; no synthetic routing or solver frames."""

from html import escape


def render(graph, decision=None, alternative=None):
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
    for c in graph.get("components", []):
        if decision and c["ref"] == decision["ref"]:
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
        else:
            part(
                c,
                c["pos"],
                c.get("rot", 0),
                "#9baea0",
                1 if placed is None or c["ref"] in placed else 0.2,
            )
    svg.append(
        f'<text x="0" y="{margin}">Amber: original · Green: chosen · Purple: alternative</text></g></svg>'
    )
    return "\n".join(svg)
