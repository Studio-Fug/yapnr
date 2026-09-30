"""SI geometry: routed copper path per requirement, the ideal board, a placement estimate.

Post-route (:func:`read_board` + :func:`routed_geometry`): one pcbnew read of the
board in a KiCad-python worker (``python3 -m pnr.si.extract dump <board> <out.json>
--net N ...``; the headless KiCad copy's interpreter is preferred, ``PNR_SI_KICAD_PYTHON``
overrides) dumps the tracks, vias and pads of the requirement nets, with KiCad's own
pad hit tests at every track end. The rest is pure Python in the pnr runtime: a copper
graph per net (track ends, T-junctions, via barrels layer by layer, pad lands), the
shortest copper path driver pad -> series pad -> connector pad, and everything off that
path (dangling stubs, extra vias, other pads) folded into shunt loads at the path node
where it attaches. Consecutive same-layer same-width segments are merged: the deck
lumps each segment anyway.

Ideal (:func:`ideal_geometry`): zero-length legs, no vias - the classification
reference (a failure that persists here is a *design* failure). Pre-layout
(:func:`estimate_geometry`): per link, the Manhattan pad distance x a detour factor
on the top copper layer (``nominal``) or split top / deepest inner signal layer / top
with 2 vias (``pessimistic``; layers from the stackup, never hard-coded).

Module level is stdlib only and Python 3.9 compatible (KiCad's interpreter).
"""

from __future__ import annotations

import collections
import hashlib
import heapq
import json
import math
import os
import sys
import tempfile
from pathlib import Path

PNR_ROOT = Path(__file__).resolve().parents[2]
HEADLESS_PY = (
    Path.home()
    / "Applications/KiCad-headless.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
)
GUI_PY = (
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
)
TOL = 1e-3  # mm


# ------------------------------------------------------------------ KiCad worker


def dump_main(src, dst, nets):
    """Dump copper of ``nets`` (KiCad python only)."""
    import pcbnew

    want = set(nets)
    b = pcbnew.LoadBoard(str(src))
    mm = pcbnew.ToMM
    out = dict(
        board=str(src),
        nets=sorted(want),
        tracks=[],
        vias=[],
        pads=[],
        zones=[],
        copper_layers=[b.GetLayerName(l) for l in b.GetEnabledLayers().CuStack()],
        kicad=pcbnew.Version(),
    )
    pads = []
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() not in want:
                continue
            q = pad.GetPosition()
            bb = pad.GetBoundingBox()
            lids = list(pad.GetLayerSet().CuStack())
            try:
                shape = int(pad.GetShape(lids[0] if lids else pcbnew.F_Cu))
            except Exception:
                shape = int(pad.GetShape())
            pads.append(pad)
            out["pads"].append(
                dict(
                    ref=fp.GetReference(),
                    name=pad.GetNumber(),
                    net=pad.GetNetname(),
                    x=mm(q.x),
                    y=mm(q.y),
                    bbox=[mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetRight()), mm(bb.GetBottom())],
                    smd=pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD,
                    shape=shape,
                    layers=[b.GetLayerName(l) for l in lids],
                    fpid=fp.GetFPIDAsString(),
                )
            )

    def hits(net, pt, layer):
        return [
            [p.GetParentFootprint().GetReference(), p.GetNumber()]
            for p in pads
            if p.GetNetname() == net and p.IsOnLayer(layer) and p.HitTest(pt, 0)
        ]

    for t in b.GetTracks():
        net = t.GetNetname()
        if net not in want:
            continue
        if t.GetClass() == "PCB_VIA":
            q = t.GetPosition()
            try:
                d = mm(t.GetWidth(pcbnew.F_Cu))
            except Exception:
                d = mm(t.GetWidth())
            out["vias"].append(
                dict(
                    net=net,
                    x=mm(q.x),
                    y=mm(q.y),
                    d=d,
                    drill=mm(t.GetDrillValue()),
                    top=b.GetLayerName(t.TopLayer()),
                    bottom=b.GetLayerName(t.BottomLayer()),
                )
            )
            continue
        s, e = t.GetStart(), t.GetEnd()
        lay = t.GetLayer()
        out["tracks"].append(
            dict(
                net=net,
                layer=b.GetLayerName(lay),
                x1=mm(s.x),
                y1=mm(s.y),
                x2=mm(e.x),
                y2=mm(e.y),
                w=mm(t.GetWidth()),
                length=mm(t.GetLength()),
                arc=t.GetClass() == "PCB_ARC",
                pads1=hits(net, s, lay),
                pads2=hits(net, e, lay),
            )
        )
    for z in b.Zones():
        if z.GetNetname() in want and not z.GetIsRuleArea():
            out["zones"].append(
                dict(
                    net=z.GetNetname(),
                    layers=[b.GetLayerName(l) for l in z.GetLayerSet().CuStack()],
                )
            )
    # Poured copper per layer (any net): reference-plane detection (pnr.si.report.board_planes)
    bb = b.GetBoardEdgesBoundingBox()
    out["board_area_mm2"] = mm(bb.GetWidth()) * mm(bb.GetHeight())
    out["pours"] = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        for l in z.GetLayerSet().CuStack():
            try:
                filled = z.GetFilledPolysList(l).Area() / 1e12  # nm^2 -> mm^2
            except Exception:
                filled = 0.0
            if filled > 0:
                out["pours"].append(
                    dict(net=z.GetNetname(), layer=b.GetLayerName(l), filled_mm2=round(filled, 3))
                )
    Path(dst).write_text(json.dumps(out))


def kicad_python(env=None):
    """``(interpreter, warning)`` for the pcbnew board read.

    ``PNR_SI_KICAD_PYTHON``, else ``PNR_KICAD_PYTHON`` (src15: the engine-wide KiCad
    python, hier/env2.json), else the headless KiCad copy's python. The GUI bundle's
    python is only a fallback (a pcbnew-reading worker, which registers no Dock app,
    unlike the bundle's kicad-cli); using it returns a warning that is printed to
    stderr and recorded in the dump and the report. ``PNR_SI_NO_GUI_PYTHON=1`` refuses
    it instead, like the ngspice runner refuses GUI-bundle interpreters; so does a set
    ``PNR_KICAD_PYTHON`` (never a /Applications/KiCad path when the env var is set).
    """
    env = os.environ if env is None else env
    for cand in (env.get("PNR_SI_KICAD_PYTHON"), env.get("PNR_KICAD_PYTHON"), str(HEADLESS_PY)):
        if cand and os.access(cand, os.X_OK):
            return cand, None
    if env.get("PNR_KICAD_PYTHON"):
        raise FileNotFoundError(
            "PNR_KICAD_PYTHON=%s is not executable (the GUI bundle python is never used "
            "when it is set)" % env["PNR_KICAD_PYTHON"]
        )
    if os.access(GUI_PY, os.X_OK):
        if (env.get("PNR_SI_NO_GUI_PYTHON") or "").strip() == "1":
            raise FileNotFoundError(
                "headless KiCad python missing (%s) and PNR_SI_NO_GUI_PYTHON=1 refuses the GUI "
                "bundle; set PNR_SI_KICAD_PYTHON" % HEADLESS_PY
            )
        warning = (
            "pcbnew read uses the GUI bundle python %s (headless copy %s missing; set PNR_SI_KICAD_PYTHON)"
            % (GUI_PY, HEADLESS_PY)
        )
        sys.stderr.write("pnr.si: warning: %s\n" % warning)
        return GUI_PY, warning
    raise FileNotFoundError("no KiCad python with pcbnew (set PNR_SI_KICAD_PYTHON)")


def read_board(board, nets, *, timeout=180.0, env=None):
    """One time-bounded pcbnew read of ``board`` for ``nets``; returns the dump dict.

    Runs niced with its own deadline (:func:`pnr.si.runner.bounded_cmd`) while holding
    a machine-wide SI slot (:func:`pnr.si.runner.slot`).
    """
    from pnr.proc import run_status
    from pnr.si.runner import bounded_cmd, slot

    py, warning = kicad_python(env)
    with tempfile.TemporaryDirectory(prefix="pnr-si-dump-") as tmp:
        out, log = Path(tmp) / "dump.json", Path(tmp) / "dump.log"
        args = [py, "-m", "pnr.si.extract", "dump", str(board), str(out)]
        for n in sorted(set(nets)):
            args += ["--net", n]
        with open(log, "wb") as f:
            extra = (env if env is not None else os.environ).get("PNR_SI_KICAD_PYTHONPATH")
            pp = os.pathsep.join([str(PNR_ROOT)] + ([extra] if extra else []))
            with slot(env, what="pcbnew board read"):
                code, timed_out = run_status(
                    bounded_cmd(args, timeout + 5.0, env),
                    timeout=timeout,
                    env=dict(os.environ, PYTHONPATH=pp),
                    stdout=f,
                    stderr=f,
                )
        if code != 0 or not out.exists():
            why = "timeout after %.0f s" % timeout if timed_out else "exit %s" % code
            raise RuntimeError(
                "pcbnew dump failed (%s): %s" % (why, log.read_text(errors="replace")[-400:])
            )
        d = json.loads(out.read_text())
    d["board_sha256"] = hashlib.sha256(Path(board).read_bytes()).hexdigest()
    d["python"] = py
    if warning:
        d.setdefault("warnings", []).append(warning)
    return d


# ------------------------------------------------------------------ copper graph


def _key(layer, x, y):
    return ("pt", layer, round(x, 4), round(y, 4))


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay), 0.0
    u = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    u = max(0.0, min(1.0, u))
    return math.hypot(p[0] - (ax + u * dx), p[1] - (ay + u * dy)), u


class Copper:
    """Copper graph of one net: nodes = pads, track ends/junctions, via layer nodes."""

    def __init__(self, dump, net, layers, span):
        self.net = net
        self.edges = []
        self.adj = collections.defaultdict(list)
        tracks = [t for t in dump["tracks"] if t["net"] == net]
        vias = [v for v in dump["vias"] if v["net"] == net]
        self.pads = {(p["ref"], p["name"]): p for p in dump["pads"] if p["net"] == net}
        self.vias = vias
        ends = collections.defaultdict(list)
        for t in tracks:
            ends[t["layer"]] += [(t["x1"], t["y1"]), (t["x2"], t["y2"])]
        for t in tracks:
            a, b = (t["x1"], t["y1"]), (t["x2"], t["y2"])
            cuts = [0.0, 1.0]
            if not t["arc"]:
                for p in ends[t["layer"]] + [(v["x"], v["y"]) for v in vias]:
                    d, u = _seg_dist(p, a, b)
                    if d <= TOL and TOL < u * t["length"] < t["length"] - TOL:
                        cuts.append(u)
            cuts = sorted(set(round(u, 9) for u in cuts))
            pts = [(a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])) for u in cuts]
            for i in range(len(pts) - 1):
                frac = cuts[i + 1] - cuts[i]
                self._edge(
                    _key(t["layer"], *pts[i]),
                    _key(t["layer"], *pts[i + 1]),
                    "track",
                    length=t["length"] * frac,
                    layer=t["layer"],
                    width=t["w"],
                )
            for pt, hit in (((t["x1"], t["y1"]), t["pads1"]), ((t["x2"], t["y2"]), t["pads2"])):
                for ref, name in hit:
                    if (ref, name) in self.pads:
                        self._edge(_key(t["layer"], *pt), ("pad", ref, name), "land", length=0.0)
        # track ends inside a via's land connect to that via's layer node
        pts_by_layer = collections.defaultdict(set)
        for e in self.edges:
            for k in (e["a"], e["b"]):
                if k[0] == "pt":
                    pts_by_layer[k[1]].add(k)
        for i, v in enumerate(vias):
            try:
                i0, i1 = layers.index(v["top"]), layers.index(v["bottom"])
            except ValueError:
                continue
            vl = layers[min(i0, i1) : max(i0, i1) + 1]
            for j in range(len(vl) - 1):
                self._edge(
                    ("via", i, vl[j]),
                    ("via", i, vl[j + 1]),
                    "barrel",
                    length=span(vl[j], vl[j + 1]),
                    via=i,
                    la=vl[j],
                    lb=vl[j + 1],
                )
            for L in vl:
                for k in pts_by_layer.get(L, ()):
                    if math.hypot(k[2] - v["x"], k[3] - v["y"]) <= v["d"] / 2 + TOL:
                        self._edge(k, ("via", i, L), "land", length=0.0)
                for (ref, name), p in self.pads.items():
                    bb = p["bbox"]
                    if (
                        L in p["layers"]
                        and bb[0] - TOL <= v["x"] <= bb[2] + TOL
                        and bb[1] - TOL <= v["y"] <= bb[3] + TOL
                    ):
                        self._edge(("via", i, L), ("pad", ref, name), "land", length=0.0)
        for pk in self.pads:
            self.adj[("pad",) + pk]  # every pad is a node, even if unrouted

    def _edge(self, a, b, kind, **kw):
        e = dict(kw, a=a, b=b, kind=kind, id=len(self.edges))
        self.edges.append(e)
        self.adj[a].append((b, e["id"]))
        self.adj[b].append((a, e["id"]))

    def shortest(self, src, dst):
        """Edge ids of the shortest copper path src -> dst (node keys), or None."""
        dist, prev = {src: 0.0}, {}
        heap = [(0.0, 0, src)]
        tie = 0
        while heap:
            d, _, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, eid in self.adj[u]:
                nd = d + self.edges[eid]["length"] + 1e-9  # tie-break on hop count
                if nd < dist.get(v, float("inf")):
                    dist[v], prev[v] = nd, (u, eid)
                    tie += 1
                    heapq.heappush(heap, (nd, tie, v))
        if dst not in dist:
            return None
        path, node = [], dst
        while node != src:
            u, eid = prev[node]
            path.append((u, node, eid))
            node = u
        return list(reversed(path))


def _leg(cu, src, dst, st):
    """Geometry of one net leg src pad -> dst pad (``(ref, pad)`` tuples)."""
    s, d = ("pad",) + src, ("pad",) + dst
    leg = dict(
        net=cu.net, source="%s.%s" % src, target="%s.%s" % dst, elements=[], attach=[], warnings=[]
    )
    for k, p in (("source_pad", src), ("target_pad", dst)):
        pad = cu.pads.get(p)
        if pad is not None:
            bb = pad["bbox"]
            leg[k] = dict(
                ref=p[0],
                pad=p[1],
                smd=pad["smd"],
                layers=pad["layers"][:1] if pad["smd"] else pad["layers"],
                area_mm2=round((bb[2] - bb[0]) * (bb[3] - bb[1]), 5),
            )
    path = cu.shortest(s, d) if s in cu.adj and d in cu.adj else None
    if path is None:
        leg["status"] = "open"
        return leg
    leg["status"] = "ok"
    on_path_nodes = {s: 0}
    elements = []
    for u, v, eid in path:
        e = cu.edges[eid]
        if e["kind"] == "track":
            last = elements[-1] if elements else None
            if (
                last
                and last["kind"] == "seg"
                and last["layer"] == e["layer"]
                and abs(last["width_mm"] - e["width"]) < 1e-6
            ):
                last["length_mm"] += e["length"]
                last["n_merged"] += 1
            else:
                elements.append(
                    dict(
                        kind="seg",
                        layer=e["layer"],
                        width_mm=e["width"],
                        length_mm=e["length"],
                        n_merged=1,
                    )
                )
        elif e["kind"] == "barrel":
            last = elements[-1] if elements else None
            la, lb = (e["la"], e["lb"]) if u == ("via", e["via"], e["la"]) else (e["lb"], e["la"])
            if last and last["kind"] == "via" and last["via_index"] == e["via"]:
                last["to"] = lb
            else:
                via = cu.vias[e["via"]]
                elements.append(
                    dict(
                        kind="via",
                        via_index=e["via"],
                        **{"from": la, "to": lb},
                        drill_mm=via["drill"],
                        diameter_mm=via["d"],
                    )
                )
        on_path_nodes.setdefault(v, len(elements))
    for el in elements:
        if el["kind"] == "seg":
            el["length_mm"] = round(el["length_mm"], 5)
    leg["elements"] = elements
    # off-path copper -> shunt loads at the attaching path node
    path_edges = {eid for _, _, eid in path}
    seen_edges = set(path_edges)
    seen_vias = {el["via_index"] for el in elements if el["kind"] == "via"}
    reached_pads = {src, dst}
    for node, at in sorted(on_path_nodes.items(), key=lambda kv: kv[1]):
        stack = [node]
        visited = {node}
        while stack:
            u = stack.pop()
            for v, eid in cu.adj[u]:
                if eid in seen_edges:
                    continue
                seen_edges.add(eid)
                e = cu.edges[eid]
                if e["kind"] == "track":
                    leg["attach"].append(
                        dict(
                            at=at,
                            kind="stub",
                            layer=e["layer"],
                            width_mm=e["width"],
                            length_mm=round(e["length"], 5),
                        )
                    )
                elif e["kind"] == "barrel" and e["via"] not in seen_vias:
                    seen_vias.add(e["via"])
                    via = cu.vias[e["via"]]
                    leg["attach"].append(
                        dict(at=at, kind="via_stub", drill_mm=via["drill"], diameter_mm=via["d"])
                    )
                if v[0] == "pad" and (v[1], v[2]) not in reached_pads:
                    reached_pads.add((v[1], v[2]))
                    leg["attach"].append(dict(at=at, kind="pad", ref=v[1], pad=v[2]))
                if v not in visited and v not in on_path_nodes:
                    visited.add(v)
                    stack.append(v)
    for pk in cu.pads:
        if pk not in reached_pads:
            leg["warnings"].append(
                "pad %s.%s on %s not connected to the path (island or other leg)"
                % (pk[0], pk[1], cu.net)
            )
    per_layer = collections.defaultdict(float)
    for el in elements:
        if el["kind"] == "seg":
            per_layer[el["layer"]] += el["length_mm"]
    leg["copper_mm"] = {k: round(v, 4) for k, v in sorted(per_layer.items())}
    leg["length_mm"] = round(sum(per_layer.values()), 4)
    leg["vias"] = sum(1 for el in elements if el["kind"] == "via")
    return leg


def routed_geometry(dump, intent, st):
    """Per-leg routed geometry of a resolved intent from a board dump."""
    from pnr.si import physics

    layers = physics.copper_layers(st)
    cu_layers = dump.get("copper_layers") or layers
    missing = [l for l in cu_layers if l not in layers]
    if missing:
        raise ValueError("board copper layers %s not in the SI stackup %s" % (missing, layers))
    span = lambda a, b: physics.via_span_mm(st, a, b)
    points = [(intent["driver"]["ref"], intent["driver"]["pad"])]
    for s in intent["series"]:
        points += [(s["ref"], s["in_pad"]), (s["ref"], s["out_pad"])]
    points.append((intent["connector"]["ref"], intent["connector"]["pad"]))
    legs = []
    for i, net in enumerate(intent["nets"]):
        cu = Copper(dump, net, layers, span)
        leg = _leg(cu, points[2 * i], points[2 * i + 1], st)
        if any(z["net"] == net for z in dump.get("zones", [])):
            leg["warnings"].append("zone copper on %s ignored" % net)
        legs.append(leg)
    return dict(
        kind="routed",
        legs=legs,
        board=dump.get("board"),
        board_sha256=dump.get("board_sha256"),
        status="open" if any(l["status"] == "open" for l in legs) else "ok",
    )


def ideal_geometry(intent):
    """Zero-length legs, no vias, design shunts only: the classification reference."""
    legs = []
    points = [(intent["driver"]["ref"], intent["driver"]["pad"])]
    for s in intent["series"]:
        points += [(s["ref"], s["in_pad"]), (s["ref"], s["out_pad"])]
    points.append((intent["connector"]["ref"], intent["connector"]["pad"]))
    for i, net in enumerate(intent["nets"]):
        a, b = points[2 * i], points[2 * i + 1]
        attach = [
            dict(at=0, kind="pad", ref=s["ref"], pad=s["pad"])
            for s in intent.get("shunts", [])
            if s["net"] == net
        ]
        legs.append(
            dict(
                net=net,
                source="%s.%s" % a,
                target="%s.%s" % b,
                status="ok",
                elements=[],
                attach=attach,
                warnings=[],
                copper_mm={},
                length_mm=0.0,
                vias=0,
            )
        )
    return dict(kind="ideal", legs=legs, status="ok")


def _pad_xy(c, name):
    for p in c.pads:
        if p.name == name:
            a = math.radians(c.rot)
            ox, oy = p.offset
            return c.pos[0] + ox * math.cos(a) - oy * math.sin(a), c.pos[1] + ox * math.sin(
                a
            ) + oy * math.cos(a)
    raise KeyError(name)


def estimate_layers(st=None):
    """``(top, deep)`` layers of the estimates: the top copper layer, and the inner
    signal (non-plane) layer farthest from it (the bottom layer if every inner layer
    is a plane) - the longest via transition a detour could take."""
    from pnr.si import physics

    st = st or physics.stackup(None)
    layers = physics.copper_layers(st)
    planes = set(st.get("planes") or [])
    inner = [l for l in layers[1:-1] if l not in planes]
    return layers[0], (inner[-1] if inner else layers[-1])


def estimate_geometry(intent, components, *, detour=1.1, variant="nominal", width_mm=0.2, st=None):
    """Pre-layout legs from placement: Manhattan pad distance x ``detour``.

    ``nominal``: all on the top layer, no vias. ``pessimistic``: top / deepest inner
    signal layer / top thirds with 2 vias (:func:`estimate_layers` of ``st``).
    """
    top, deep = estimate_layers(st)
    by_ref = {c.ref: c for c in components}
    points = [(intent["driver"]["ref"], intent["driver"]["pad"])]
    for s in intent["series"]:
        points += [(s["ref"], s["in_pad"]), (s["ref"], s["out_pad"])]
    points.append((intent["connector"]["ref"], intent["connector"]["pad"]))
    legs = []
    for i, net in enumerate(intent["nets"]):
        a, b = points[2 * i], points[2 * i + 1]
        pa, pb = _pad_xy(by_ref[a[0]], a[1]), _pad_xy(by_ref[b[0]], b[1])
        length = (abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])) * detour
        if variant == "pessimistic" and length > 0:
            els = [
                dict(kind="seg", layer=top, width_mm=width_mm, length_mm=length / 3, n_merged=1),
                dict(kind="via", **{"from": top, "to": deep}),
                dict(kind="seg", layer=deep, width_mm=width_mm, length_mm=length / 3, n_merged=1),
                dict(kind="via", **{"from": deep, "to": top}),
                dict(kind="seg", layer=top, width_mm=width_mm, length_mm=length / 3, n_merged=1),
            ]
        else:
            els = (
                [dict(kind="seg", layer=top, width_mm=width_mm, length_mm=length, n_merged=1)]
                if length > 0
                else []
            )
        attach = [
            dict(at=0, kind="pad", ref=s["ref"], pad=s["pad"])
            for s in intent.get("shunts", [])
            if s["net"] == net
        ]
        legs.append(
            dict(
                net=net,
                source="%s.%s" % a,
                target="%s.%s" % b,
                status="ok",
                elements=els,
                attach=attach,
                warnings=[],
                length_mm=round(length, 4),
                vias=sum(e["kind"] == "via" for e in els),
            )
        )
    return dict(kind="estimate-" + variant, detour=detour, legs=legs, status="ok")


def estimate_geometries(intents, components, *, detour=1.1, variant="nominal", st=None):
    """``{name: estimate geometry}`` for resolved intents from a placed component list.

    Pure Python (usable in the pcbnew ``prepare`` worker). A requirement whose parts
    or pads are missing from ``components`` gets ``{'kind': ..., 'error': ...}``.
    """
    out = {}
    for intent in intents:
        try:
            out[intent["name"]] = estimate_geometry(
                intent, components, detour=detour, variant=variant, st=st
            )
        except Exception as error:
            out[intent["name"]] = dict(
                kind="estimate-" + variant, error="placement estimate: %r" % (error,)
            )
    return out


def subset_dump(dump, nets):
    """The part of a dump on ``nets`` (for frozen test fixtures)."""
    keep = set(nets)
    return dict(
        board=dump.get("board"),
        board_sha256=dump.get("board_sha256"),
        nets=sorted(keep),
        copper_layers=dump.get("copper_layers"),
        kicad=dump.get("kicad"),
        tracks=[t for t in dump["tracks"] if t["net"] in keep],
        vias=[v for v in dump["vias"] if v["net"] in keep],
        pads=[p for p in dump["pads"] if p["net"] in keep],
        zones=[z for z in dump.get("zones", []) if z["net"] in keep],
    )


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "dump":
        nets = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--net"]
        dump_main(sys.argv[2], sys.argv[3], nets)
    else:
        sys.exit(
            "usage: python3 -m pnr.si.extract dump <board.kicad_pcb> <out.json> --net NET [...]"
        )
