#!/usr/bin/env python3
# flake8: noqa  (verbatim copy of a working-notes script)
"""Adversarial-review arithmetic for radar60/board-design.md (2026-10-03). Closed-form, first order; NOT full-wave.

Every output is a [D] number for the 'Review' section of board-design.md. Reuses ../fab-calc/fab60_calc.py.
Inputs that come from documents are tagged in the comments with their source.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fab-calc"))
import fab60_calc as fc  # noqa: E402

C0 = 299792458.0
F0 = 62.05e9
LAM0 = C0 / F0 * 1e3
D = C0 / (2 * 64e9) * 1e3


def hdr(t):
    print("\n## " + t)


def patch_f0(W, L, h, er):
    """Resonant frequency (GHz) of a fixed-copper patch (TL model, Hammerstad fringing)."""
    ereff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 * h / W)
    dL = 0.412 * h * (ereff + 0.3) * (W / h + 0.264) / ((ereff - 0.258) * (W / h + 0.8))
    return C0 / (2 * (L + 2 * dL) * 1e-3 * math.sqrt(ereff)) / 1e9


hdr("1. Dielectric under an L2 window (RO4835 core + filled L2 copper + pressed RO4450F)")
h_core, t_l2 = 0.1016, 0.0175  # RO4835 4 mil core; 0.5 oz L2 copper removed in the window
for h_bond in (
    0.090,
    0.1016,
):  # pressed bondply thickness: nominal 4 mil, or ~10 % flow loss (assumption)
    h = h_core + t_l2 + h_bond
    print(
        f"  bondply {h_bond*1e3:.0f} um -> window dielectric {h*1e3:.0f} um (board_calc used 203 um)"
    )

hdr(
    "2. Patch centre-frequency uncertainty on the 8 mil window (fixed copper, nominal W 1.605 / L 1.192 mm)"
)
W, L, h, er = 1.605, 1.192, 0.212, 3.54
f_nom = patch_f0(W, L, h, er)
terms = {
    "Dk +-0.10 (process 3.33-3.48 vs design 3.66; 60 GHz unknown)": abs(
        patch_f0(W, L, h, er + 0.10) - f_nom
    ),
    "L etch +-13 um (LDI, good lot)": abs(patch_f0(W, L - 0.013, h, er) - f_nom),
    "L etch +-25 um (+-1 mil class)": abs(patch_f0(W, L - 0.025, h, er) - f_nom),
    "thickness +-10 %": abs(patch_f0(W, L, h * 0.9, er) - f_nom),
}
for k, v in terms.items():
    print(f"  {k:62s} +-{v:.2f} GHz")
rss13 = math.sqrt(
    terms["Dk +-0.10 (process 3.33-3.48 vs design 3.66; 60 GHz unknown)"] ** 2
    + terms["L etch +-13 um (LDI, good lot)"] ** 2
    + terms["thickness +-10 %"] ** 2
)
rss25 = math.sqrt(
    terms["Dk +-0.10 (process 3.33-3.48 vs design 3.66; 60 GHz unknown)"] ** 2
    + terms["L etch +-25 um (+-1 mil class)"] ** 2
    + terms["thickness +-10 %"] ** 2
)
print(
    f"  f0 nominal {f_nom:.2f} GHz (model); RSS +-{rss13:.2f} GHz (13 um etch) / +-{rss25:.2f} GHz (25 um etch)"
    f" = +-{rss13/62.05*100:.1f} / +-{rss25/62.05*100:.1f} %; patch BW 4.3 % = +-{0.0215*62.05:.2f} GHz"
)
print(
    f"  bracketing step that covers +-RSS(13 um) with 3 variants: L scale +-{rss13/62.05*100:.1f} %"
)

hdr("3. Corporate 2-patch column inside one column pitch (d = 2.342 mm)")
l4 = fc.line("4 mil", 0.1016, 3.56, 0.0037)
l8 = fc.line("8 mil", 0.212, 3.54, 0.0038)
for Wp in (1.25, 1.40, 1.605):
    gap = D - Wp
    for nm, r in (("4 mil", l4), ("8 mil", l8)):
        clr = (gap - r["w"]) / 2
        print(
            f"  W {Wp:.3f}: gap {gap:.3f} mm; 50-ohm {nm} line {r['w']:.3f} mm centred -> {clr*1e3:4.0f} um to each patch edge"
            f" ({clr/0.212:.2f} h_window)"
        )
print(
    f"  W/L for W 1.25 / L 1.19 = {1.25/1.19:.2f}: TM10 within ~{(1.25/1.19-1)*100:.0f} % of TM01 -> cross-pol risk; keep W/L >= 1.2"
)

hdr(
    "4. Feed-loss bands carrying the model's own +-30 % conductor-loss error and a through-via launch"
)
line_rx = 2.7 * l4["tot"] / 1.0
line_tx_b = 1.5 * l4["tot"] + 9.5 * l8["tot"]
line_tx_4 = 11.0 * l4["tot"]
for nm, ln, extra in (
    ("RX 2.7 mm 4 mil", line_rx, 0.0),
    ("TX option B (1.5 mm 4 mil + 9.5 mm 8 mil + step)", line_tx_b, 0.1),
    ("TX all 4 mil GCPW (+10..20 % coplanar)", line_tx_4 * 1.15, 0.0),
):
    lo = ln * 0.7 + 0.3 + extra
    hi = ln * 1.3 + 0.8 + extra
    print(f"  {nm:52s} line {ln:.2f} dB -> {lo:.1f}-{hi:.1f} dB (launch 0.3-0.8 dB)")

hdr("5. 1.0 V RF/PA rail worst case at 2.5 A peak (window 0.95-1.05 V, SWRS219F 7.4)")
buck_dc = 0.020  # LP87524 VOUT >= 1 V PWM +-2 % (SNVSAW2B 6.5)
buck_tr = 0.030  # load step 0->2 A, 10 us, +-3 % (SNVSAW2B 6.5)
fb_dcr = 0.020  # MPZ2012S101AT000 DCR 20 mOhm (LCSC C15957 page)
for nm, i_fb, r_sh in (
    ("one ferrite carries 2.5 A, 5 mOhm shunt (plan)", 2.5, 0.005),
    ("PA branch ferrite 2.0 A, 2 mOhm shunt (ISK Rev D style)", 2.0, 0.002),
    ("two ferrites in parallel on the PA branch, 2 mOhm", 1.0, 0.002),
):
    drop = i_fb * fb_dcr + 2.5 * r_sh + 0.003
    print(
        f"  {nm:55s} IR {drop*1e3:4.1f} mV; worst-case min {1.0-buck_dc-buck_tr-drop:.3f} V;"
        f" nominal setpoint, half the step spec {1.0-0.015-drop:.3f} V"
    )

hdr(
    "6. REG-01 off-time: where can a calibration/monitor emission of c ms go? (4.80 ms burst / 50 ms frame)"
)
gap = 50 - 4.8
print(
    f"  inter-burst gap {gap:.1f} ms; isolating an emission from both bursts needs >= 2 x 33 ms = 66 ms of gap -> impossible"
)
print(
    f"  any emission shares a 33 ms window with a burst: burst + emission + sub-2 ms fragments <= {33-25.5-2:.1f} ms -> extra <= {33-25.5-2-4.8:.2f} ms"
)
print(
    "  alternatives: append <= 0.70 ms contiguous to the burst, or run cal/monitors in a frame that replaces one burst (<= 5.5 ms)"
)

hdr("7. EIRP bound for the 2-patch column (directivity, not a measurement)")
for d_l in (0.55, 0.60):
    kd = 2 * math.pi * d_l
    gain_af = 10 * math.log10(2 / (1 + math.sin(kd) / kd))
    print(
        f"  2 isotropic elements at {d_l:.2f} lambda0: +{gain_af:.1f} dB; with a 6.5-7.0 dBi patch -> column {6.5+gain_af-0.5:.1f}-{7.0+gain_af-0.5:.1f} dBi"
        " (0.5 dB allowance for element-pattern overlap)"
    )
print(
    "  -> use 10 dBi as the pre-measurement bound: TDM <= +2 dBm per port, 3-TX coherent <= -7.5 dBm per port (EIRP 12 dBm)"
)

hdr("8. Via fence pitch and GSG coupon pitch (PCBWay 0.10 mm min space, 0.15 mm min drill)")
for drill, pad in ((0.20, 0.40), (0.15, 0.35), (0.15, 0.30)):
    print(
        f"  via {drill:.2f}/{pad:.2f}: at 0.36 mm pitch pad gap {(0.36-pad)*1e3:+.0f} um; min pitch for 0.10 mm pad gap {pad+0.10:.2f} mm"
    )
print(
    f"  lambda_g/8 = {l4['lam_g']/8:.2f} mm (4 mil); a 0.45 mm pitch leaves 0.30 mm openings = lambda_g/{l4['lam_g']/0.30:.0f}"
)
for pitch in (0.150, 0.200, 0.250):
    s_w = 0.10
    g = pitch - s_w / 2 - 0.10 / 2 - s_w / 2  # G pad 0.10 mm wide centred at pitch
    print(
        f"  GSG {pitch*1e3:.0f} um with 0.10 mm pads: S-G copper gap {(pitch - s_w)*1e3:.0f} um "
        f"({'ok' if pitch - s_w >= 0.10 - 1e-9 else 'below 0.10 mm rule'})"
    )

hdr("9. Thermal at 50 C ambient (RthJA 22.3 C/W is JEDEC 2S2P 76x114 mm, free air)")
for p in (1.0, 1.3, 1.5):
    print(
        f"  {p:.1f} W: JEDEC {50+22.3*p:.0f} C; small board in enclosure 30-40 C/W [E] -> {50+30*p:.0f}-{50+40*p:.0f} C (TJ max 105 C)"
    )

hdr("10. Corner-reflector far field (2 D^2 / lambda, D = aperture ~ a*sqrt(2))")
for a in (48.6, 27.3):
    Dm = a * math.sqrt(2) * 1e-3
    print(f"  edge {a} mm: far field >= {2*Dm**2/(LAM0*1e-3):.1f} m")

hdr("11. TX length equalization geometry (option B arcs R >= 1.5 mm on 8 mil)")
man = [4.0, 7.64, 11.28]
for k, m in enumerate(man):
    print(f"  TX{k+1}: Manhattan {m:.2f} mm -> extra {10.9-m:+.1f} mm to reach 10.9 mm")
for nm, R, w_line, pitch_extra in (
    ("8 mil microstrip, R 1.5 mm", 1.5, 0.443, 0.0),
    ("4 mil GCPW, R 0.6 mm", 0.6, 0.200, 0.6),
):
    per = (2 * math.pi - 4) * R  # extra length of one meander period without legs, advance 4R
    leg = max(0.0, (6.9 - per) / 2)  # legs needed for TX1's +6.9 mm in one period
    print(
        f"  TX1 meander, {nm}: one period adds {per:.2f} mm + 2 x leg over {4*R:.1f} mm advance; +6.9 mm needs leg"
        f" {leg:.2f} mm -> about {4*R:.1f} x {2*R+leg+w_line+pitch_extra:.1f} mm of area"
    )
print(
    f"  delay-match limit 7.9 ps = {7.9e-12*C0/math.sqrt(l8['ee'])*1e3:.2f} mm (8 mil) / {7.9e-12*C0/math.sqrt(l4['ee'])*1e3:.2f} mm (4 mil)"
)
