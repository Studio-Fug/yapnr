#!/usr/bin/env python3
# flake8: noqa  (verbatim copy of a working-notes script)
"""Derived numbers for radar60/research-fab.md (first-order, closed-form; NOT full-wave).

Microstrip: Hammerstad-Jensen static model + thickness correction, Kirschning-Jansen dispersion.
Loss: dielectric (exact for quasi-TEM) + conductor Rs/(Z0 w) x fringing x roughness (rough, +/-30%).
Patch: transmission-line model (Balanis) + Jackson-Alexopoulos VSWR<2 bandwidth.
BGA: IWR6843 ABL0161, 0.65 mm pitch, 0.32 mm NSMD land (SWRS219F p.80).
"""
import math

C0 = 299792458.0
ETA0 = 376.730313668
F = 62e9  # band centre used throughout (60.3-63.8 GHz band, RAD/ANT-02)
LAM0 = C0 / F * 1e3  # mm


def z01(u):
    fu = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(fu / u + math.sqrt(1 + 4 / u**2))


def eeff0(u, er):
    a = (
        1
        + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49
        + math.log(1 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def static(w, h, t, er):
    u = w / h
    tn = t / h
    if tn > 0:
        du1 = tn / math.pi * math.log(1 + 4 * math.e / (tn / math.tanh(math.sqrt(6.517 * u)) ** 2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * du1
    else:
        du1 = dur = 0
    u1, ur = u + du1, u + dur
    ee = eeff0(ur, er) * (z01(u1) / z01(ur)) ** 2
    z = z01(ur) / math.sqrt(eeff0(ur, er))
    return z, ee


def eeff_f(w, h, t, er, f):
    _, e0 = static(w, h, t, er)
    u = w / h
    fn = f / 1e9 * h  # GHz*mm
    p1 = 0.27488 + (0.6315 + 0.525 / (1 + 0.0157 * fn) ** 20) * u - 0.065683 * math.exp(-8.7513 * u)
    p2 = 0.33622 * (1 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1 - math.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1 + 2.751 * (1 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return er - (er - e0) / (1 + p)


def width_for(z0, h, t, er):
    lo, hi = 0.01 * h, 20 * h
    for _ in range(100):
        m = 0.5 * (lo + hi)
        if static(m, h, t, er)[0] > z0:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def rs(f, sigma):
    return math.sqrt(math.pi * f * 4e-7 * math.pi / sigma)


SIG_CU, SIG_NI = 5.8e7, 1.43e7
FRINGE = 1.3  # assumption: edge current crowding at w/h~2 (+/-30% on conductor loss)
ROUGH = 1.9  # assumption: Hammerstad roughness factor ~saturated at 62 GHz (skin depth 0.27 um)
ENIG_P = 0.3  # assumption: fraction of conductor loss on finished (top+edge) surfaces


def line(name, h, er, tand, t=0.035, finish="Ag"):
    w = width_for(50.0, h, t, er)
    ee = eeff_f(w, h, t, er, F)
    lam_g = LAM0 / math.sqrt(ee)
    ad = 27.287 * er * (ee - 1) * tand / (math.sqrt(ee) * (er - 1) * LAM0)  # dB/mm
    ac = 8.686 * rs(F, SIG_CU) / (50.0 * w * 1e-3) * FRINGE * ROUGH / 1e3  # dB/mm
    if finish == "ENIG":
        ac *= (1 - ENIG_P) + ENIG_P * rs(F, SIG_NI) / rs(F, SIG_CU)
    z_m = static(w - 0.025, h, t, er)[0]
    z_p = static(w + 0.025, h, t, er)[0]
    z_h = static(w, h * 1.175, t, er)[0]
    return dict(
        name=name,
        h=h,
        er=er,
        tand=tand,
        w=w,
        ee=ee,
        lam_g=lam_g,
        ad=ad,
        ac=ac,
        tot=ad + ac,
        feed6=6 * (ad + ac),
        dz_w=(z_m - z_p) / 2,
        dz_h=z_h - 50,
        dphi_40um=360 * 0.040 / lam_g,
    )


def patch(h, er):
    W = C0 / (2 * F) * math.sqrt(2 / (er + 1)) * 1e3
    ereff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 * h / W)
    dL = 0.412 * h * (ereff + 0.3) * (W / h + 0.264) / ((ereff - 0.258) * (W / h + 0.8))
    L = C0 / (2 * F * math.sqrt(ereff)) * 1e3 - 2 * dL
    bw = 3.77 * (er - 1) / er**2 * (W / L) * (h / LAM0)
    return W, L, bw


if __name__ == "__main__":
    print(
        f"f = {F/1e9:.0f} GHz, lambda0 = {LAM0:.3f} mm; Cu skin depth = "
        f"{1/math.sqrt(math.pi*F*4e-7*math.pi*SIG_CU)*1e6:.3f} um; Rs Cu = {rs(F,SIG_CU)*1e3:.1f} mOhm, Ni = {rs(F,SIG_NI)*1e3:.1f} mOhm"
    )
    rows = [
        line("RO4835 LoPro 4 mil (Dk 3.56*, Df .0037) ImAg", 0.1016, 3.56, 0.0037),
        line("RO4835 LoPro 4 mil, ENIG", 0.1016, 3.56, 0.0037, finish="ENIG"),
        line("RO3003 5 mil (Dk 3.00, Df .0010) ImAg", 0.127, 3.00, 0.0010),
        line("RO4835 6.6 mil (Dk 3.66, Df .0037) ImAg", 0.168, 3.66, 0.0037),
        line(
            "FR408HR 2x1067 5 mil (TI LEVM: Dk 3.33, Df .0095) ImAg", 0.127, 3.33, 0.0095, t=0.040
        ),
        line(
            "OSH Park 6L FR408HR 106 4.36 mil (Dk 3.23, Df .011) ENIG",
            0.1107,
            3.23,
            0.011,
            t=0.040,
            finish="ENIG",
        ),
        line(
            "JLC 6L 3313 prepreg 0.0994 mm (Dk 4.1, Df ~.02) ENIG",
            0.0994,
            4.1,
            0.02,
            t=0.035,
            finish="ENIG",
        ),
    ]
    print("\n50-ohm microstrip at 62 GHz (mask-free; t = finished Cu):")
    print(
        f"{'stackup':58s} {'w50 mm':>7s} {'eeff':>5s} {'lg mm':>6s} {'a_d':>6s} {'a_c':>6s} {'dB/mm':>6s} {'6mm dB':>6s} {'dZ +-25um w':>11s} {'dZ +17.5% h':>11s}"
    )
    for r in rows:
        print(
            f"{r['name']:58s} {r['w']:7.3f} {r['ee']:5.2f} {r['lam_g']:6.3f} {r['ad']:6.3f} {r['ac']:6.3f} {r['tot']:6.3f} {r['feed6']:6.2f} {r['dz_w']:+11.1f} {r['dz_h']:+11.1f}"
        )
    print(f"(RF-04 check: 40 um path error on RO4835 4 mil = {rows[0]['dphi_40um']:.1f} deg)")
    print("\nRectangular patch at 62 GHz (TL model; Jackson-Alexopoulos VSWR<2 BW):")
    for nm, h, er in [
        ("RO4835 4 mil", 0.1016, 3.56),
        ("RO3003 5 mil", 0.127, 3.0),
        ("FR408HR 5 mil", 0.127, 3.33),
        ("RO4835 6.6 mil", 0.168, 3.66),
        ("RO4835 4 mil + RO4450F 4 mil (L2 cleared)", 0.2032, 3.55),
        ("RO4835 10 mil", 0.254, 3.66),
    ]:
        W, L, bw = patch(h, er)
        print(
            f"  {nm:44s} W={W:.3f} L={L:.3f} mm  BW~{bw*100:.1f}%  (+/-13 um L -> +/-{0.013/L*100:.1f}% f0 = {0.013/L*62:.2f} GHz;"
            f" +/-25 um -> +/-{0.025/L*62:.2f} GHz)"
        )
    print("  ANT-02 band 60.3-63.8 GHz = %.1f%% fractional" % ((63.8 - 60.3) / 62.05 * 100))
    print(
        "\nPatch radiation efficiency, Q model: 1/Q = 1/Qrad + tand + ROUGH*delta/h, Qrad = 1/(sqrt2*BW):"
    )
    dl = 1 / math.sqrt(math.pi * F * 4e-7 * math.pi * SIG_CU) * 1e3
    for nm, h, er, td in [
        ("RO3003 5 mil", 0.127, 3.0, 0.0010),
        ("RO4835 4 mil", 0.1016, 3.56, 0.0037),
        ("RO4835 4 mil + RO4450F 4 mil", 0.2032, 3.55, 0.0038),
        ("FR408HR 5 mil", 0.127, 3.33, 0.0095),
        ("JLC FR-4 3313 0.0994 mm", 0.0994, 4.1, 0.02),
    ]:
        W, L, bw = patch(h, er)
        qr = 1 / (math.sqrt(2) * bw)
        eta = (1 / qr) / (1 / qr + td + ROUGH * dl / h)
        print(f"  {nm:30s} BW {bw*100:.1f}%  eta ~{eta*100:.0f}% ({10*math.log10(eta):+.1f} dB)")
    print("\nIWR6843 BGA escape (0.65 mm pitch, 0.32 mm land):")
    p, land = 0.65, 0.32
    for nm, via_pad, clr in [
        ("TI LEVM 0.20/0.457 pad", 0.457, 0.1016),
        ("JLC 0.15/0.25", 0.25, 0.09),
        ("JLC 0.20/0.40", 0.40, 0.09),
        ("PCBWay 0.20 + 4 mil ring = 0.40", 0.40, 0.10),
        ("PCBWay std 0.20 + 6 mil ring = 0.50", 0.50, 0.10),
        ("OSH Park 6L 0.203 + 4 mil = 0.406", 0.406, 0.127),
        ("Eurocircuits 0.10 FHS + 0.30 = 0.40", 0.40, 0.10),
    ]:
        g_int = p / math.sqrt(2) - land / 2 - via_pad / 2
        g_dep = p - land / 2 - via_pad / 2
        print(
            f"  {nm:38s} interstitial gap {g_int*1e3:5.0f} um ({'ok' if g_int >= clr else 'FAIL'} vs {clr*1e3:.0f} um rule);"
            f" depopulated-site gap {g_dep*1e3:4.0f} um"
        )
    for nm, tw, sp in [
        ("JLC 0.09/0.09", 0.09, 0.09),
        ("PCBWay/Eurocircuits 0.10/0.10", 0.10, 0.10),
        ("OSH Park 0.127/0.127", 0.127, 0.127),
    ]:
        need = tw + 2 * sp
        print(
            f"  one trace between balls ({nm}): needs {need:.3f} mm of {p-land:.3f} mm -> {'ok' if need <= p-land+1e-9 else 'FAIL'}"
        )
    print(
        "\nOSH Park 6L bare-board cost, 70 x 60 mm: %.2f sq in x $15 = $%.2f per 3 boards"
        % (70 * 60 / 645.16, 70 * 60 / 645.16 * 15)
    )
