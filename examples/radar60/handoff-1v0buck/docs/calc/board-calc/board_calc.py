#!/usr/bin/env python3
# flake8: noqa  (verbatim copy of a working-notes script)
"""Derived numbers for radar60/board-design.md (closed-form, first order; NOT full-wave).

Reuses the microstrip/patch models of ../fab-calc/fab60_calc.py (Hammerstad-Jensen + Kirschning-Jansen,
conductor loss Rs/(Z0 w) x fringing x roughness, +/-30 %). Every output here is a [D] number.
Geometry: IWR6843 ABL0161 ball map (SWRS219F Table 6-1); U1 rotated so the RX edge faces north.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fab-calc"))
import fab60_calc as fc  # noqa: E402

C0 = 299792458.0
F0 = 62.05e9
LAM0 = C0 / F0 * 1e3
D = C0 / (2 * 64e9) * 1e3  # ANT-01 lattice


def hdr(t):
    print("\n## " + t)


hdr("Lines at 62 GHz (ImAg, mask-free)")
l4 = fc.line("RO4835 LoPro 4 mil over L2", 0.1016, 3.56, 0.0037)
l8 = fc.line("RO4835 4 mil + RO4450F 4 mil over L3 (L2 cleared)", 0.2032, 3.54, 0.0038)
l8fr = fc.line("RO4835 4 mil + FR-4 prepreg 4 mil over L3 (fallback)", 0.2032, 3.9, 0.012)
ljlc = fc.line("JLC 3313 0.0994 mm FR-4, ENIG (Rev A0)", 0.0994, 4.1, 0.02, finish="ENIG")
for r in (l4, l8, l8fr, ljlc):
    print(
        f"  {r['name']:52s} w50 {r['w']:.3f} mm  eeff {r['ee']:.2f}  lg {r['lam_g']:.3f} mm  {r['tot']:.3f} dB/mm"
    )
for z in (35.36, 70.7):
    for nm, h, er in (("4 mil", 0.1016, 3.56), ("8 mil", 0.2032, 3.54)):
        print(f"  {z:5.1f} ohm on {nm}: w = {fc.width_for(z, h, 0.035, er):.3f} mm")

hdr("Ball and feed geometry (U1-relative mm; U1 rotated -90 deg: RX edge north, TX edge east)")
p = 0.65
rows = "ABCDEFGHJKLMNPR"


def ball(name):
    r = rows.index(name[0]) + 1
    c = int(name[1:])
    x, y = (c - 8) * p, (8 - r) * p  # datasheet view: row A north, column 1 west
    return (y, -x)  # rotate -90 deg


for n, b in (
    ("RX1", "M2"),
    ("RX2", "K2"),
    ("RX3", "H2"),
    ("RX4", "F2"),
    ("TX1", "B4"),
    ("TX2", "B6"),
    ("TX3", "B8"),
    ("VOUT_PA", "A2"),
    ("VOUT_PA", "B2"),
    ("VIN_13RF2", "C2"),
    ("CLKP", "B15"),
    ("CLKM", "C15"),
):
    print(f"  {n:9s} {b:4s} -> ({ball(b)[0]:+.2f}, {ball(b)[1]:+.2f})")
rx_x = [ball(b)[0] for b in ("M2", "K2", "H2", "F2")]
xc = sum(rx_x) / 4
cols = [xc + (k - 1.5) * D for k in range(4)]
print(
    f"  d = {D:.3f} mm; RX columns x = {[round(c, 3) for c in cols]}; lateral shifts {[round(c - b, 3) for c, b in zip(cols, rx_x)]}"
)


def sbend(off, R):
    th = math.acos(1 - abs(off) / (2 * R))
    return 2 * R * math.sin(th), 2 * R * th


R = 1.0
spans = [sbend(c - b, R) for c, b in zip(cols, rx_x)]
span = max(s for s, _ in spans)
L_rx = max(L for _, L in spans)
print(
    f"  RX S-bends (R = {R} mm): vertical span needed {span:.2f} mm, longest path {L_rx:.2f} mm (others meandered to match)"
)

tx_y = [ball(b)[1] for b in ("B4", "B6", "B8")]
xT = [8.0 + k * D for k in range(3)]
yin = 3.8
plane = 5.2
man = [(xT[k] - plane) + abs(yin - tx_y[k]) for k in range(3)]
arc_saving = (2 - math.pi / 2) * 1.0
print(
    f"  TX columns x = {[round(v, 3) for v in xT]}, inputs at y = {yin}; Manhattan feed lengths {[round(v, 2) for v in man]} mm;"
    f" one 90-deg arc (R 1 mm) saves {arc_saving:.2f} mm -> equal-length TX feed ~{max(man) - arc_saving:.1f} mm"
)
rx_c = (cols[3], 9.5)
tx_c = (xT[0], yin + 2.3)
print(
    f"  nearest RX/TX column centres {rx_c} / {tx_c}: {math.dist(rx_c, tx_c):.2f} mm = {math.dist(rx_c, tx_c)/LAM0:.2f} lambda0"
)

hdr(
    "RF-03 path loss (launch 0.3-0.5 dB assumed, from SPRACG5-class transitions; lines from the table above)"
)
for nm, segs in (
    ("RX, 4 mil, 3.0 mm", [(3.0, l4)]),
    ("TX, 4 mil, 11.0 mm", [(11.0, l4)]),
    ("TX, 1.5 mm 4 mil + 9.5 mm 8 mil (L2 cleared, RO4450F)", [(1.5, l4), (9.5, l8)]),
    ("TX, 1.5 mm 4 mil + 9.5 mm 8 mil (FR-4 prepreg fallback)", [(1.5, l4), (9.5, l8fr)]),
    ("RX, JLC FR-4 (A0), 3.0 mm", [(3.0, ljlc)]),
    ("TX, JLC FR-4 (A0), 11.0 mm", [(11.0, ljlc)]),
):
    line_db = sum(L * r["tot"] for L, r in segs)
    print(
        f"  {nm:58s} line {line_db:.2f} dB -> total {line_db + 0.3:.2f}-{line_db + 0.5 + (0.1 if len(segs) > 1 else 0):.2f} dB"
    )

hdr("RF-04 / RF-03 phase and delay")
B = 3.5e9
tau = 5 / 360 / (B / 2)
for r in (l4, l8):
    vp = C0 / math.sqrt(r["ee"])
    print(
        f"  {r['name'][:30]:30s}: 5 deg = {5/360*r['lam_g']*1e3:.0f} um; residual <= 5 deg across 60.3-63.8 GHz after a"
        f" single-frequency cal needs delay match <= {tau*1e12:.1f} ps = {tau*vp*1e3:.2f} mm; design target 2 ps = {2e-12*vp*1e3:.2f} mm"
    )
print(f"  via fence pitch lambda_g/8: 4 mil {l4['lam_g']/8:.2f} mm, 8 mil {l8['lam_g']/8:.2f} mm")

hdr("Patches at 62 GHz (cavity/TL model)")
for nm, h, er in (
    ("RO4835 4 mil (L2 solid)", 0.1016, 3.56),
    ("RO4835+RO4450F 8 mil (L2 window)", 0.2032, 3.54),
    ("RO4835+FR-4 8 mil (fallback)", 0.2032, 3.9),
    ("JLC FR-4 3313 (A0)", 0.0994, 4.1),
):
    W, L, bw = fc.patch(h, er)
    print(
        f"  {nm:34s} W {W:.3f} L {L:.3f} mm BW(VSWR2) {bw*100:.1f} %  inter-column gap at d: {D - W:.3f} mm"
    )

hdr("Regulatory power settings (REG-01/02), TX conducted max 12 dBm (SWRS219F)")
eirp = 12.0
for g, tag in ((7.0, "nominal column 7 dBi"), (9.0, "upper bound 9 dBi (directivity, lossless)")):
    tdm = eirp - g
    coh = eirp - g - 20 * math.log10(3)
    print(
        f"  {tag:42s}: TDM per-port <= {tdm:+.1f} dBm (backoff {12 - tdm:.1f} dB); 3-TX coherent per-port <= {coh:+.1f} dBm"
        f" (backoff {12 - coh:.1f} dB)"
    )

hdr("REG-01 off-time (profile 4.1: 4.80 ms burst / 50 ms frame)")
burst, frame, win = 4.80, 50.0, 33.0
print(
    f"  worst 33 ms window: on <= {burst} ms -> off >= {win - burst:.2f} ms; minus a <2 ms fragment -> > {win - burst - 2:.2f} ms >= 25.5 ms"
    f" (margin {win - burst - 2 - 25.5:.2f} ms). Max burst for 25.5 ms off with 2 ms fragments excluded: {win - 25.5 - 2:.1f} ms"
)

hdr("IF and PMIC spur")
S = 50e12
for f in (3.89e6, 4.0e6, 8.0e6, 6.67e6):
    print(f"  IF {f/1e6:.2f} MHz -> range {C0*f/(2*S):.1f} m")
print(f"  usable complex IF at 8 Msps (0.9 Fs) = 7.2 MHz -> max range {C0*7.2e6/(2*S):.1f} m")

hdr("Power and thermal")
p_rails = 1.0 * 2.5 + 1.2 * 1.0 + 1.8 * 0.85 + 3.3 * 0.05
print(
    f"  datasheet-max rail power {p_rails:.2f} W -> {p_rails/0.85/5:.2f} A from 5 V at 85 % efficiency"
)
print(
    f"  1.0 V rail: 5 mOhm Kelvin shunt drop at 2.5 A = {2.5*0.005*1e3:.1f} mV (inside FB loop on dev builds)"
)
for pw in (1.0, 1.3):
    print(
        f"  TJ at 50 C ambient, RthJA 22.3 C/W (JEDEC 2S2P), {pw} W -> {50 + pw*22.3:.0f} C (limit 105 C)"
    )
print(
    f"  1.0 V buck ripple current: (5-1)*(1/5)/(0.47 uH * 4 MHz) = {(5-1)*(1/5)/(0.47e-6*4e6):.2f} A p-p"
)

hdr("LVDS raw capture (dev builds)")
bits = 512 * 4 * 2 * 16
print(
    f"  {bits} bits per 80 us chirp = {bits/80e-6/1e6:.0f} Mbps -> per lane (2 lanes) {bits/80e-6/2e6:.0f} Mbps;"
    f" UI at 900 Mbps {1/0.9:.2f} ns; 2 mm lane-to-clock skew = {2e-3/(C0/math.sqrt(3.3))*1e12:.0f} ps"
)

hdr("Radome (flat sheet, normal incidence)")
for nm, er in (
    ("polycarbonate (er ~2.9 assumed)", 2.9),
    ("ABS (er ~2.5 assumed)", 2.5),
    ("PP (er ~2.2 assumed)", 2.2),
):
    print(
        f"  {nm:32s} half-wave wall {LAM0/(2*math.sqrt(er)):.2f} mm; full-wave {LAM0/math.sqrt(er):.2f} mm"
    )
print(f"  air gap multiples of lambda0/2: {LAM0/2:.2f}, {LAM0:.2f}, {1.5*LAM0:.2f} mm")

hdr("Board")
w, h = 60.0, 46.0
print(
    f"  U1 centre (26.0, 28.0), rot 270; antenna copper box x {26-4.96:.1f}..{26+12.684+0.8:.1f}, y {28+3.8:.1f}..{28+11.57:.1f}"
)
print(f"  outline {w} x {h} mm = {w*h/100:.1f} cm2 = {w*h/645.16:.2f} sq in")
