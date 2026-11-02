"""Deterministic SVGs from published geometry and measured scalar history; no solve."""

import hashlib
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent


def canvas(title):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="760" viewBox="0 0 900 760"><rect width="900" height="760" fill="#f8fafc"/><g font-family="sans-serif" fill="#172033"><text x="40" y="42" font-size="25">{title}</text>'  # noqa: E501
    ]


def render():
    out = ROOT / "figures"
    out.mkdir(exist_ok=True)
    rho = np.load(ROOT / "model/inputs/rho.npz", allow_pickle=False)["rho"]
    spec = json.loads((ROOT / "model/inputs/spec.json").read_text())
    drawing = canvas("Original four-port antenna coupon — baseline")
    drawing.append(
        '<text x="40" y="72" font-size="17">Top copper and ground voids; antenna-region crop, coordinates in mm</text>'
    )
    scale = 18

    def x(v):
        return 450 + scale * v

    def y(v):
        return 650 - scale * v

    drawing.append(
        f'<rect x="{x(-20)}" y="{y(30)}" width="720" height="576" fill="#dce6ec" stroke="#64748b"/>'
    )
    for void in spec["board"]["ground"]["keepout"]:
        a, b = void["x_mm"]
        c, d = void["y_mm"]
        drawing.append(
            f'<rect x="{x(a)}" y="{y(d)}" width="{(b-a)*scale}" height="{(d-c)*scale}" fill="#fff" stroke="#94a3b8" stroke-dasharray="5 3"/>'  # noqa: E501
        )
    # Consecutive occupied cells form exact axis-aligned rectangles, no smoothing.
    for j in range(rho.shape[1]):
        edges = np.diff(np.r_[0, rho[:, j], 0])
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            drawing.append(
                f'<rect x="{x(-18.4+.1*a):.4f}" y="{y(-.8+.1*(j+1)):.4f}" width="{(b-a)*.1*scale:.4f}" height="1.8" fill="#b86b25"/>'  # noqa: E501
            )
    for port in spec["ports"]:
        a, b = port["x_mm"]
        c, d = port["y_mm"]
        drawing.append(
            f'<rect x="{x(a)}" y="{y(d)}" width="{(b-a)*scale}" height="{(d-c)*scale}" fill="#1454ce"/><text x="{x(b)+10}" y="{y((c+d)/2)+5}" font-size="18">P{port["n"]}</text>'  # noqa: E501
        )
    for t in (-20, -10, 0, 10, 20):
        drawing.append(f'<text x="{x(t)-12}" y="704" font-size="16">{t}</text>')
    for t in (0, 10, 20, 30):
        drawing.append(f'<text x="48" y="{y(t)+5}" font-size="16">{t}</text>')
    drawing.append(
        '<text x="40" y="738" font-size="16">Copper: brown · source apertures: blue · ground voids: dashed · no optimized design claim</text></g></svg>'  # noqa: E501
    )
    (out / "baseline.svg").write_text("\n".join(drawing) + "\n")
    data = json.loads((ROOT / "results/p3-convergence.json").read_text())
    drawing = canvas("Actual-array P3 adjoint convergence")
    drawing.append(
        '<text x="40" y="76" font-size="18">Measured successive DTFT changes; all declared probes, exact saved source</text>'  # noqa: E501
    )

    def cx(v):
        return 115 + (v - 30000) / 170000 * 720

    def cy(v):
        return 635 - (math.log10(v) + 11) / 8 * 490

    for exponent in range(-11, -2):
        yy = cy(10**exponent)
        drawing.append(
            f'<line x1="115" y1="{yy}" x2="835" y2="{yy}" stroke="#d5dce4"/><text x="40" y="{yy+5}" font-size="17">1e{exponent}</text>'  # noqa: E501
        )
    for step in (40000, 80000, 120000, 160000, 200000):
        drawing.append(f'<text x="{cx(step)-27}" y="669" font-size="17">{step//1000}k</text>')
    points = " ".join(f"{cx(n):.3f},{cy(v):.3f}" for n, v in data["history"])
    drawing.append(f'<polyline points="{points}" fill="none" stroke="#1d4ed8" stroke-width="3"/>')
    drawing.append(
        f'<line x1="115" y1="{cy(1e-10)}" x2="835" y2="{cy(1e-10)}" stroke="#c2410c" stroke-width="2" stroke-dasharray="7 4"/>'  # noqa: E501
    )
    drawing.append(
        '<text x="160" y="120" font-size="20">First passing pair: 181,150 and 182,334 steps</text>'
    )
    drawing.append(
        '<text x="40" y="708" font-size="18">Strict threshold 1e-10 twice · 128 checks · P4 not completed</text>'
    )
    drawing.append(
        '<text x="40" y="738" font-size="16">Convergence does not establish full-array gradient or physical RF qualification.</text></g></svg>'  # noqa: E501
    )
    (out / "p3-convergence.svg").write_text("\n".join(drawing) + "\n")
    for path in sorted(out.glob("*.svg")):
        print(path.name, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    render()
