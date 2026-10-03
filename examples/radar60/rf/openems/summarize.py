"""Compare openEMS column/patch results with the closed-form predictions of `rfmacro.dims`.

  python3 openems/summarize.py results/openems/*/result.json

Prints one Markdown table row per run: resonance (best |S11|), RL-10 dB band, |S11| at the band
edges and centre, broadside directivity, radiation efficiency (P_rad / P_accepted), realized
broadside gain at the port, E/H-plane beamwidths, and the closed-form numbers for the same
parameters. Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rfmacro import dims as dims_mod  # noqa: E402
from rfmacro.params import resolve  # noqa: E402


def closed_form(params: dict, single: bool) -> dict:
    p = resolve({k: v for k, v in params.items() if k in resolve()})
    d = dims_mod.compute(p)
    eta = d["patch"]["efficiency"]
    if single:
        return dict(f0=d["patch"]["f0_ghz"], d_dbi=d["patch"]["directivity_dbi"], eta=eta)
    feed = (d["lines"]["alpha50_db_mm"] or 0.0) * 5.37  # P1 -> T 3.71 mm + mean arm 1.66 mm
    g = d["column"]["directivity_dbi"] + 10 * math.log10(eta) - feed
    return dict(
        f0=d["patch"]["f0_ghz"],
        d_dbi=d["column"]["directivity_dbi"],
        eta=eta * 10 ** (-feed / 10),
        g=g,
    )


def row(fn: str) -> str:
    with open(fn) as fh:
        r = json.load(fh)
    name = os.path.basename(os.path.dirname(fn))
    single = name.startswith("patch")
    cf = closed_form(r["meta"]["params"], single)
    band = ", ".join(f"{a:.2f}-{b:.2f}" for a, b in r["rl10_bands_ghz"]) or "none"
    s = r["s11_db_at"]
    ff = r["far_field"]
    pick = "62.05"
    fx = ff[pick]
    best = max(ff.values(), key=lambda v: v["realized_gain_broadside_dbi"])
    return (
        f"| {name} | {r['f_s11_min_ghz']:.2f} ({cf['f0']:.2f}) | {r['s11_min_db']:.1f} | {band} |"
        f" {s['60.30']:.1f} / {s['62.05']:.1f} / {s['63.80']:.1f} |"
        f" {fx['d_broadside_dbi']:.1f} ({cf['d_dbi']:.1f}) |"
        f" {100 * fx['rad_efficiency']:.0f} % ({100 * cf['eta']:.0f} %) |"
        f" {fx['realized_gain_broadside_dbi']:.1f} / best {best['realized_gain_broadside_dbi']:.1f}"
        f" ({cf.get('g', float('nan')):.1f}) | {fx['e_plane_hpbw_deg']:.0f} / {fx['h_plane_hpbw_deg']:.0f} |"
        f" {r['wall_s'] / 60:.0f} min |"
    )


def main(argv) -> int:
    print(
        "| run | f(min S11) GHz (closed form f0) | min S11 dB | RL10 band GHz | S11 60.3/62.05/63.8 dB |"
        " D broadside 62.05 dBi (cf) | rad. eff. 62.05 (cf) | realized gain 62.05 / best dBi (cf) |"
        " HPBW E/H deg | wall |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for fn in argv:
        print(row(fn))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
