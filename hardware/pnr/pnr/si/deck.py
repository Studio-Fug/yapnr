"""ngspice deck for one requirement x driver corner x cable length (pure Python).

Topology, driver pad to the first pixel's DIN::

    KIBIS driver (RECTDRIVER subckt, package R/L/C + C_comp inside)
      -> board leg 0: lumped pi sections per merged segment (L = L' len, C = C' len,
         split so no section exceeds ``max_section_ps``), vias as L + C/2 | C/2, stubs /
         extra vias / other pads as shunt C where they attach, SMD land C at pad nodes
      -> series part: R (+ ESL)
      -> board leg 1 ... -> connector pad ``conn``
      -> connector: C/2 - L - C/2 (per-pin model)
      -> cable: ``T`` lines (Z0 = the corner's cable impedance, TD = len / (nvp c)); with a
         ``loss`` model, one lossless ``T`` per ``segment_m`` (default 1 m) each behind the
         segment's lumped series loss: the skin-effect R-L ladder of
         :func:`pnr.si.physics.skin_ladder` (``loss.model`` ``skin_ladder``, default: R_dc
         at DC rising as sqrt(f), 6.35 ohm/m at 300 MHz) or one frequency-flat R at
         ``f_ref`` (``flat``); 0 m -> the pixel sits at the mating side
      -> receiver ``rx``: C_in, clamp diodes to its rail and GND (rail = driver corner rail)

Only the cable is a transmission line (board copper is lumped: a critique run showed
short board ``T`` elements returning flat garbage). The segmented lossy cable was
chosen over ngspice ``LTRA`` (a flat R per segment reproduced LTRA's R-L-C line within
1.5 % on p027 led1 at 3 m, 20x faster: LTRA's convolution took 23-56 s per deck; LTRA
has no frequency-dependent R either, hence the skin-effect ladder). One analysis covers one rising
and one falling edge window: ``.tran tstep tstop 0 max_step``. The driver include is
written as ``{DRIVER_LIB}`` in the template; :func:`deck_hash` hashes the template
plus the driver model's sha256, so the hash does not depend on cache paths.
"""

from __future__ import annotations

import hashlib
import math

from pnr.si import physics

PLACEHOLDER = "{DRIVER_LIB}"
DECK_VERSION = "pnr-si-deck-v2"


def nominal_z0(cable):
    return float(cable["per_metre"]["z0_ohm"])


def window_ns(profile, cable, cable_m):
    """Edge window (ns): base + k x cable delay, rounded up."""
    w = profile["stimulus"]["window_ns"]
    td = (
        cable_m
        * physics.cable_lc(cable["per_metre"]["z0_ohm"], cable["per_metre"]["nvp"])["td_ns_per_m"]
    )
    raw = w["base"] + w["per_cable_td"] * td
    step = w.get("round_to", 10.0)
    return math.ceil(raw / step - 1e-9) * step


def _fmt(x):
    return "%.6g" % x


class _Net:
    def __init__(self):
        self.parent = []
        self.lines = []

    def node(self):
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def add(self, name, a, b, value, comment=""):
        self.lines.append((name, a, b, value, comment))


def build(
    intent,
    geometry,
    *,
    corner,
    cable_m,
    driver,
    library,
    st,
    window,
    td_ns=None,
    series_ohm=None,
    max_section_ps=50.0,
    cable_z0=None,
):
    """Deck template text and metadata.

    ``driver``: KIBIS meta (subckt, rail_v, sha256). ``window``: edge window (ns).
    ``series_ohm``: override every series resistor (what-if runs; recorded in meta).
    ``cable_z0``: the cable impedance corner (default: the cable's nominal Z0).
    """
    prof = library.get("profile", intent["profile"])
    rx = library.get("receiver", prof["receiver"])
    cable = library.get("cable", prof["cable"])
    conn_model = library.get("connector", intent["connector"].get("model") or prof["connector"])
    td_ns = prof["stimulus"]["td_ns"] if td_ns is None else td_ns
    rail = float(driver["rail_v"])
    net = _Net()
    shunt_pf = {}
    meta = dict(
        version=DECK_VERSION,
        requirement=intent["name"],
        geometry=geometry["kind"],
        corner=corner,
        cable_m=cable_m,
        rail_v=rail,
        window_ns=window,
        td_ns=td_ns,
        segments=[],
        vias=[],
        series=[],
        shunts_pf=0.0,
        warnings=[],
    )
    drv = net.node()
    cur = drv
    counter = dict(L=0, C=0)

    def cap(node, pf, why):
        if pf > 0:
            counter["C"] += 1
            net.add("C%d" % counter["C"], node, None, pf * 1e-12, why)

    def ind(a, b, nh, why):
        counter["L"] += 1
        net.add("L%d" % counter["L"], a, b, nh * 1e-9, why)

    for li, leg in enumerate(geometry["legs"]):
        if leg.get("status") == "open":
            raise ValueError("leg %s is open" % leg["net"])
        nodes = [cur]
        for el in leg["elements"]:
            if el["kind"] == "seg":
                p = physics.line(st, el["layer"], el["width_mm"])
                L = p["l_nh_per_mm"] * el["length_mm"]
                C = p["c_pf_per_mm"] * el["length_mm"]
                td = p["td_ps_per_mm"] * el["length_mm"]
                n = max(1, int(math.ceil(td / max_section_ps - 1e-9)))
                a = cur
                for k in range(n):
                    b = net.node()
                    cap(
                        a,
                        C / n / 2,
                        "seg %s %.3fmm %s" % (leg["net"], el["length_mm"], el["layer"]),
                    )
                    ind(a, b, L / n, "")
                    cap(b, C / n / 2, "")
                    a = b
                cur = a
                meta["segments"].append(
                    dict(
                        net=leg["net"],
                        layer=el["layer"],
                        width_mm=el["width_mm"],
                        length_mm=round(el["length_mm"], 4),
                        z0_ohm=round(p["z0_ohm"], 2),
                        td_ps=round(td, 2),
                        l_nh=round(L, 4),
                        c_pf=round(C, 4),
                        sections=n,
                        model=p["model"],
                    )
                )
            elif el["kind"] == "via":
                v = physics.via(st, el["from"], el["to"], el.get("drill_mm"), el.get("diameter_mm"))
                b = net.node()
                cap(cur, v["c_pf"] / 2, "via %s-%s" % (el["from"], el["to"]))
                ind(cur, b, v["l_nh"], "")
                cap(b, v["c_pf"] / 2, "")
                cur = b
                meta["vias"].append(
                    dict(
                        net=leg["net"],
                        **{"from": el["from"], "to": el["to"]},
                        l_nh=round(v["l_nh"], 4),
                        c_pf=round(v["c_pf"], 4),
                        span_mm=round(v["span_mm"], 4),
                    )
                )
            else:
                raise ValueError("unknown element %r" % el["kind"])
            nodes.append(cur)
        # land capacitance of the SMD pads at both ends of the leg
        for key, node in (("source_pad", nodes[0]), ("target_pad", nodes[-1])):
            pad = leg.get(key)
            if pad and pad.get("smd") and pad.get("area_mm2"):
                cap(
                    node,
                    physics.pad_c_pf(st, pad["layers"][0], pad["area_mm2"]),
                    "land %s.%s" % (pad["ref"], pad["pad"]),
                )
        for at in leg.get("attach", []):
            node = nodes[min(at["at"], len(nodes) - 1)]
            if at["kind"] == "stub":
                pf = physics.line(st, at["layer"], at["width_mm"])["c_pf_per_mm"] * at["length_mm"]
            elif at["kind"] == "via_stub":
                pf = physics.via(
                    st,
                    physics.copper_layers(st)[0],
                    physics.copper_layers(st)[-1],
                    at.get("drill_mm"),
                    at.get("diameter_mm"),
                )["c_pf"]
            elif at["kind"] == "pad":
                pf = library.default_pad_pf()
                meta["warnings"].append(
                    "shunt pad %s.%s: default %.2f pF" % (at["ref"], at["pad"], pf)
                )
            else:
                raise ValueError("unknown attachment %r" % at["kind"])
            meta["shunts_pf"] += pf
            cap(node, pf, "%s at %s" % (at["kind"], leg["net"]))
        if li < len(intent["series"]):
            s = intent["series"][li]
            ohm = float(series_ohm) if series_ohm is not None else s["ohm"]
            mid, nxt = net.node(), net.node()
            net.add("RS%d" % li, cur, mid, ohm, "series %s %s" % (s["ref"], s["part"]))
            if s.get("esl_nh"):
                ind(mid, nxt, s["esl_nh"], "series ESL")
            else:
                net.parent[nxt] = mid
            meta["series"].append(
                dict(
                    ref=s["ref"],
                    ohm=ohm,
                    esl_nh=s.get("esl_nh", 0.0),
                    overridden=series_ohm is not None,
                )
            )
            cur = nxt
    conn = cur
    cn = conn_model["per_pin"]
    cab = net.node()
    cap(conn, cn["c_pf"] / 2, "connector %s" % conn_model["id"])
    ind(conn, cab, cn["l_nh"], "")
    cap(cab, cn["c_pf"] / 2, "")
    z0 = nominal_z0(cable) if cable_z0 is None else float(cable_z0)
    lc = physics.cable_lc(z0, cable["per_metre"]["nvp"])
    rxn = net.node()
    cable_td_ns = cable_m * lc["td_ns_per_m"]
    cable_r_ohm, cable_segments, ladder = 0.0, 0, None
    loss = cable.get("loss")
    if cable_m > 0 and cable_td_ns * 1000 >= 1.0:
        tline = (cab, rxn)
        if loss:
            cable_segments = max(
                1, int(math.ceil(cable_m / float(loss.get("segment_m", 1.0)) - 1e-9))
            )
            cable_r_ohm = physics.cable_r_ohm_per_m(loss) * cable_m
            if loss.get("model", "skin_ladder") == "skin_ladder":
                ladder = physics.skin_ladder(loss)
        else:
            cable_segments = 1
    else:
        tline = None
        net.parent[rxn] = net.find(cab)
    names = {}

    def nm(i):
        r = net.find(i)
        if r not in names:
            names[r] = {net.find(drv): "drv", net.find(conn): "conn", net.find(rxn): "rx"}.get(
                r, "n%d" % (len(names) + 1)
            )
        return names[r]

    for special in (drv, conn, rxn):
        nm(special)
    tstop = td_ns + 2 * window
    an = prof["analysis"]
    out = [
        "* %s %s %s corner=%s cable=%gm z0=%g"
        % (DECK_VERSION, intent["name"], geometry["kind"], corner, cable_m, z0),
        '.include "%s"' % PLACEHOLDER,
        "XDRV 0 drv %s" % driver["subckt"],
    ]
    for name, a, b, value, comment in net.lines:
        if comment:
            out.append("* " + comment)
        if name.startswith("C") and b is None:
            out.append("%s %s 0 %s" % (name, nm(a), _fmt(value)))
        elif nm(a) != nm(b):
            out.append("%s %s %s %s" % (name, nm(a), nm(b), _fmt(value)))
    if tline:
        out.append(
            "* cable %s %gm Z0=%g nvp=%g R(f_ref)=%.4g ohm in %d segment(s)%s"
            % (
                cable["id"],
                cable_m,
                z0,
                cable["per_metre"]["nvp"],
                cable_r_ohm,
                cable_segments,
                (
                    " skin-effect ladder (R_dc + %d R||L stages, fit %.1f %%)"
                    % (len(ladder["stages"]), 100 * ladder["fit"])
                    if ladder
                    else ""
                ),
            )
        )
        a = nm(tline[0])
        seg_m = cable_m / cable_segments
        for k in range(cable_segments):
            b = nm(tline[1]) if k == cable_segments - 1 else "cab%d" % (k + 1)
            if ladder:
                mid = "cabr%d" % k
                out.append("RCAB%d %s %s %s" % (k, a, mid, _fmt(ladder["r_dc"] * seg_m)))
                for j, (r_j, l_j) in enumerate(ladder["stages"]):
                    nxt = "cabs%d_%d" % (k, j)
                    out.append("RSK%d_%d %s %s %s" % (k, j, mid, nxt, _fmt(r_j * seg_m)))
                    out.append("LSK%d_%d %s %s %s" % (k, j, mid, nxt, _fmt(l_j * seg_m)))
                    mid = nxt
            elif cable_r_ohm > 0:
                mid = "cabr%d" % k
                out.append("RCAB%d %s %s %s" % (k, a, mid, _fmt(cable_r_ohm / cable_segments)))
            else:
                mid = a
            out.append(
                "TCAB%s %s 0 %s 0 Z0=%s TD=%s"
                % (
                    "" if cable_segments == 1 and cable_r_ohm == 0 else k,
                    mid,
                    b,
                    _fmt(z0),
                    _fmt(cable_td_ns * 1e-9 / cable_segments),
                )
            )
            a = b
    clamp = rx["clamps"]["spice_model"]
    out += [
        "* receiver %s" % rx["id"],
        "CRX rx 0 %s" % _fmt(rx["c_in_pf"] * 1e-12),
        "DRXH rx vrx DCLAMP",
        "DRXL 0 rx DCLAMP",
        ".model DCLAMP %s" % clamp,
        "VRX vrx 0 %s" % _fmt(rail),
        ".tran %sp %sn 0 %sp" % (_fmt(an["tstep_ps"]), _fmt(tstop), _fmt(an["max_step_ps"])),
        ".end",
    ]
    meta.update(
        tstop_s=tstop * 1e-9,
        t_rise_s=td_ns * 1e-9,
        t_fall_s=(td_ns + window) * 1e-9,
        t_end_s=tstop * 1e-9,
        vih_v=rx["vih_frac_vdd"] * rail,
        vil_v=rx["vil_frac_vdd"] * rail,
        cable_td_ns=round(cable_td_ns, 4),
        cable_z0_ohm=z0,
        cable_r_ohm=round(cable_r_ohm, 4),
        cable_segments=cable_segments,
        cable_loss=(("skin_ladder" if ladder else "flat") if cable_r_ohm > 0 else None),
        nodes=["drv", "conn", "rx"],
    )
    meta["shunts_pf"] = round(meta["shunts_pf"], 4)
    return "\n".join(out) + "\n", meta


def deck_hash(template, driver_sha256):
    return hashlib.sha256((template + "\n* driver " + driver_sha256).encode()).hexdigest()


def materialize(template, driver_path):
    return template.replace(PLACEHOLDER, driver_path)
