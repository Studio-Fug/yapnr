"""Make-room world: native items as primitives, joints, movability and the QP.

Built once per transaction from the live pcbnew board (KiCad Python, headless).
Primitives are exact copper outlines reduced to segments (tracks, via disks as
zero-length segments with a radius), polygons (pads, courtyards, rule areas) and
drill holes. Variables are 2-D offsets of *joints*:

* coincident same-net endpoints share a joint; a vertex inside a same-net pad is
  bound to that pad (its part joint, or FIX), so pad entries stay rigid;
* a neck (power/plane copper narrower than the land's required entry width, the
  source-bounded short neck of native_electrical) is rigid with its land: BOTH its
  vertices bind to the land's joint, so its length and loss/drop never change;
* same-net via banks (vias of one net within 1.2 mm joined by feed copper) are one
  joint: a bank only translates, all-or-none;
* other same-net overlaps (T-junctions, wide copper over a pad) become weld
  equalities, and segments whose two vertices are variables keep their angle;
* vertices beyond the soft radius stay FIX unless they ride on a moving part.

The movability firewall keeps FIX: locked items, pads of parts that are not
nudgeable, filled in-pad vias (removed unused pads) and their arrays, all copper of
coupled-pair and length-match nets, rule areas, and everything outside the region.
Width, layer, via size and drill are never variables. Clearances mirror the
native Oracle (net-class clearance + 1 um, via-to-SMD 5A rule, hole-to-hole) and
are never reduced; the native worker, DRC and the whole-transaction gate decide.
"""

import math
from collections import defaultdict

from pnr.shove.geom import add, closest_on_seg, point_in_poly, poly_edges, seg_seg, sub
from pnr.shove.qp import hildreth

FIX = "FIX"
DRIFT_SIDES = 16  # polygon rows approximating the cumulative part-displacement disc


def line_length_limit(length_mm):
    """G1 per moved power/plane line: |length change| <= min(0.5 mm, max(5 %, 0.15 mm)).

    The 0.15 mm floor (user decision 2026-09-28) admits the small stretches a slightly
    different original placement would have produced; widths/necks stay rigid and every
    transaction still passes the electrical audit."""
    return min(0.5, max(0.05 * length_mm, 0.15))


def _xy(p):
    return (p.x / 1e6, p.y / 1e6)


def _uid(t):
    return t.m_Uuid.AsString()


def _rings(poly):
    out = []
    for i in range(poly.OutlineCount()):
        chain = poly.COutline(i)
        out.append([_xy(chain.CPoint(j)) for j in range(chain.PointCount())])
    return out


def soft_set(board, net, centers, radius, protected=(), parts=(), fixed_ids=()):
    """UUIDs of existing copper the make-room model may move: unlocked foreign
    tracks/vias within ``radius`` of a centre, never pair/length-match
    (``protected``) nets or filled in-pad vias, plus the short fanout (stub <=
    1.2 mm and its via) of each nudgeable part in ``parts``."""
    from pnr.via_in_pad import removes_unused_pads

    tracks = list(board.GetTracks())
    protected = set(protected)
    fixed_ids = set(fixed_ids)

    def near(t):
        if t.GetClass() == "PCB_VIA":
            p = _xy(t.GetPosition())
            return min(math.dist(p, c) for c in centers)
        a, z = _xy(t.GetStart()), _xy(t.GetEnd())
        return min(math.dist(c, closest_on_seg(c, a, z)[1]) for c in centers)

    soft = {
        _uid(t)
        for t in tracks
        if t.GetClass() in ("PCB_TRACK", "PCB_VIA")
        and t.GetNetname() != net
        and not t.IsLocked()
        and t.GetNetname() not in protected
        and not removes_unused_pads(t)
        and _uid(t) not in fixed_ids
        and near(t) <= radius
    }
    footprints = {f.GetReference(): f for f in board.GetFootprints()}
    part_pads = [p for ref in parts if ref in footprints for p in footprints[ref].Pads()]
    for t in tracks:
        if (
            t.GetClass() != "PCB_TRACK"
            or t.GetNetname() in protected
            or t.IsLocked()
            or t.GetLength() / 1e6 > 1.2
        ):
            continue
        for p in part_pads:
            if p.GetNetCode() != t.GetNetCode():
                continue
            hit = [e for e in (t.GetStart(), t.GetEnd()) if p.HitTest(e)]
            if not hit:
                continue
            soft.add(_uid(t))
            other = t.GetEnd() if hit[0] == t.GetStart() else t.GetStart()
            for v in tracks:
                if (
                    v.GetClass() == "PCB_VIA"
                    and v.GetNetCode() == t.GetNetCode()
                    and (v.GetPosition() - other).EuclideanNorm() < 2000
                    and not removes_unused_pads(v)
                ):
                    soft.add(_uid(v))
    return soft


class World:
    """One make-room problem around ``centers`` with the wish ``plan`` as claims.

    ``plan`` is a routed native_electrical.power_plan result (tracks as
    (layer, a, z, width), banks as (center, points, layers), in_pad_vias). ``parts``
    names footprints that may be nudged (at most ``part_cap`` mm).
    """

    def __init__(
        self,
        board,
        rules,
        net,
        plan,
        centers,
        radius,
        *,
        parts=(),
        protected_nets=(),
        caps=None,
        part_cap=0.5,
        weights=None,
        horizon=0.6,
        step=0.12,
        margin=0.0,
        fixed_ids=(),
        part_drift=None,
        total_part_cap=None
    ):
        import pcbnew as k

        from pnr.fab_profile import geometry
        from pnr.via_in_pad import removes_unused_pads

        self.k = k
        self.b = board
        self.rules = rules
        self.net = net
        self.plan = plan
        self.centers = [tuple(c) for c in centers]
        self.radius = radius
        self.parts = set(parts)
        self.protected = set(protected_nets)
        self.g = geometry(rules)
        self.fab = rules.get("fab", {})
        self.caps = dict(signal=0.6, power=0.35, via=0.25, claim=0.6)
        self.caps.update(caps or {})
        self.part_cap = part_cap
        # Cumulative cap: a nudged part stays within ``total_part_cap`` of its
        # ORIGIN pose (the placement the routing flow started from), whatever
        # earlier transactions already moved it by (``part_drift``: ref -> current
        # minus origin, native mm). Enforced by inscribed-polygon QP rows.
        self.part_drift = {ref: tuple(o) for ref, o in (part_drift or {}).items()}
        self.total_part_cap = part_cap if total_part_cap is None else total_part_cap
        self.weights_spec = dict(part=6.0, claim=2.0)
        self.weights_spec.update(weights or {})
        self.horizon = horizon
        self.step = step
        self.margin = margin
        self.fixed_ids = set(fixed_ids)
        self.removes_unused_pads = removes_unused_pads
        self.parent = {}
        self.prims = []
        self.equalities = []
        self.log = {}
        self._build()

    # ------------------------------------------------------------------ union-find
    def find(self, a):
        parent = self.parent
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(self, a, c):
        ra, rc = self.find(a), self.find(c)
        if ra == rc:
            return
        if rc == FIX:
            ra, rc = rc, ra
        self.parent[rc] = ra  # FIX stays root

    # ------------------------------------------------------------------ build
    def _near(self, p):
        return min(math.dist(p, c) for c in self.centers) if self.centers else 0.0

    def policy_mode(self, net):
        from pnr.electrical import net_policy

        if net not in self._modes:
            self._modes[net] = net_policy(net, self.rules)["mode"] if net else None
        return self._modes[net]

    def prim(self, **kw):
        kw.setdefault("hole", None)
        kw.setdefault("smd", False)
        kw.setdefault("via", False)
        kw.setdefault("claim", False)
        kw.setdefault("zone", None)
        kw.setdefault("ref", None)
        self.prims.append(kw)
        return kw

    def _build(self):
        k = self.k
        b = self.b
        self._modes = {}
        cu = list(b.GetEnabledLayers().CuStack())
        self.cu = cu
        tracks = list(b.GetTracks())
        self.tracks = tracks
        self.items = {_uid(t): t for t in tracks}
        self.footprints = {f.GetReference(): f for f in b.GetFootprints()}
        soft = soft_set(
            b, self.net, self.centers, self.radius, self.protected, self.parts, self.fixed_ids
        )
        self.soft = soft
        self.find(FIX)
        plan = self.plan
        claim_points = [tuple(a) for _, a, z, _ in plan.get("tracks", []) for a in (a, z)]
        claim_points += [tuple(p) for c, pts, _ in plan.get("banks", []) for p in [c] + list(pts)]
        claim_points += [tuple(v[0]) for v in plan.get("in_pad_vias") or []]
        xs = [c[0] for c in self.centers + claim_points]
        ys = [c[1] for c in self.centers + claim_points]
        pad = self.radius + 1.2
        self.region = (
            (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad) if xs else (0, 0, 0, 0)
        )
        part_key = lambda ref: ("P", ref) if ref in self.parts else FIX
        self.pad_objs = {}
        for f in b.GetFootprints():
            ref = f.GetReference()
            for p in f.Pads():
                bb = p.GetBoundingBox()
                box = (
                    bb.GetLeft() / 1e6,
                    bb.GetTop() / 1e6,
                    bb.GetRight() / 1e6,
                    bb.GetBottom() / 1e6,
                )
                if not self._in_region(box):
                    continue
                self.pad_objs[_uid(p)] = p
                for la in cu:
                    if not p.IsOnLayer(la):
                        continue
                    poly = k.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(poly, la, 0, 1000, k.ERROR_INSIDE)
                    self.prim(
                        id=_uid(p),
                        label=ref + "." + p.GetNumber(),
                        net=p.GetNetname(),
                        kind="poly",
                        layers={la},
                        keys=[part_key(ref)],
                        rings=_rings(poly),
                        r=0.0,
                        smd=p.GetAttribute() == k.PAD_ATTRIB_SMD,
                        ref=ref,
                    )
                drill = max(p.GetDrillSize().x, p.GetDrillSize().y)
                if drill > 0:
                    self.prim(
                        id=_uid(p) + ":hole",
                        label=ref + "." + p.GetNumber() + ":hole",
                        net=p.GetNetname(),
                        kind="disc",
                        layers={"HOLE"},
                        keys=[part_key(ref)],
                        p=_xy(p.GetPosition()),
                        r=drill / 2e6,
                        hole_kind="pth" if p.GetAttribute() != k.PAD_ATTRIB_NPTH else "npth",
                        ref=ref,
                    )
            # Courtyards of both sides: parts on one side may not overlap (gap 0).
            for side, layer in (("F", k.F_CrtYd), ("B", k.B_CrtYd)):
                courtyard = f.GetCourtyard(layer)
                if not courtyard.OutlineCount():
                    continue
                bb = courtyard.BBox()
                box = (
                    bb.GetLeft() / 1e6,
                    bb.GetTop() / 1e6,
                    bb.GetRight() / 1e6,
                    bb.GetBottom() / 1e6,
                )
                if ref in self.parts or self._in_region(box):
                    self.prim(
                        id="crt:" + ref + ("" if side == "F" else ":B"),
                        label="courtyard " + ref + " " + side,
                        net=None,
                        kind="poly",
                        layers={"CRT"},
                        keys=[part_key(ref)],
                        rings=_rings(courtyard),
                        r=0.0,
                        ref=ref,
                        crt_side=side,
                    )
        for t in tracks:
            bb = t.GetBoundingBox()
            box = (bb.GetLeft() / 1e6, bb.GetTop() / 1e6, bb.GetRight() / 1e6, bb.GetBottom() / 1e6)
            if not self._in_region(box):
                continue
            movable = _uid(t) in soft
            if t.GetClass() == "PCB_VIA":
                key = ("V", _uid(t))
                if not movable:
                    self.union(FIX, key)
                self.prim(
                    id=_uid(t),
                    label="via:" + t.GetNetname(),
                    net=t.GetNetname(),
                    kind="disc",
                    layers=set(cu),
                    keys=[key],
                    p=_xy(t.GetPosition()),
                    r=t.GetWidth(k.F_Cu) / 2e6,
                    via=True,
                )
                self.prim(
                    id=_uid(t) + ":hole",
                    label="via-hole:" + t.GetNetname(),
                    net=t.GetNetname(),
                    kind="disc",
                    layers={"HOLE"},
                    keys=[key],
                    p=_xy(t.GetPosition()),
                    r=t.GetDrillValue() / 2e6,
                    hole_kind="inpad" if self.removes_unused_pads(t) else "via",
                )
            elif t.GetClass() == "PCB_TRACK":
                ka, kz = ("T", _uid(t), 0), ("T", _uid(t), 1)
                if not movable:
                    self.union(FIX, ka)
                    self.union(FIX, kz)
                self.prim(
                    id=_uid(t),
                    label="trk:" + t.GetNetname() + ":" + t.GetLayerName(),
                    net=t.GetNetname(),
                    kind="seg",
                    layers={t.GetLayer()},
                    keys=[ka, kz],
                    a=_xy(t.GetStart()),
                    z=_xy(t.GetEnd()),
                    r=t.GetWidth() / 2e6,
                    width=t.GetWidth() / 1e6,
                )
        # The board's rule areas and those built into footprints (KiCad judges both).
        for z in list(b.Zones()) + [z for fp in b.GetFootprints() for z in fp.Zones()]:
            if not z.GetIsRuleArea():
                continue
            bb = z.GetBoundingBox()
            box = (bb.GetLeft() / 1e6, bb.GetTop() / 1e6, bb.GetRight() / 1e6, bb.GetBottom() / 1e6)
            if not self._in_region(box):
                continue
            self.prim(
                id=_uid(z),
                label="rule:" + z.GetZoneName(),
                net=None,
                kind="poly",
                layers={la for la in cu if z.IsOnLayer(la)},
                keys=[FIX],
                rings=_rings(z.Outline()),
                r=0.0,
                zone=dict(tracks=z.GetDoNotAllowTracks(), vias=z.GetDoNotAllowVias()),
            )
        self._claims()
        self._joints()
        self._variables()

    def _in_region(self, box):
        r = self.region
        return not (box[2] < r[0] or box[0] > r[2] or box[3] < r[1] or box[1] > r[3])

    def _claims(self):
        """The wish plan's new copper. Vertices on the net's own pads or existing
        copper are FIX (the terminal attaches); every other vertex is a variable,
        so the solver may bend the new route rather than push a blocker that cannot
        move. A bank (and an in-pad array) is one rigid joint."""
        k = self.k
        plan = self.plan
        own = [
            pm
            for pm in self.prims
            if pm["net"] == self.net
            and pm["kind"] in ("poly", "seg", "disc")
            and "HOLE" not in pm["layers"]
            and "CRT" not in pm["layers"]
        ]

        def anchored(p):
            """The joint a claim vertex attaches to: a nudgeable part's land binds
            it to that part (the attach moves with the pad); any other own pad or
            existing copper is FIX; None for a free vertex."""
            for pm in own:
                if pm["kind"] == "poly":
                    if any(point_in_poly(p, ring) for ring in pm["rings"]):
                        key = pm["keys"][0]
                        return key if isinstance(key, tuple) and key[0] == "P" else FIX
            for pm in own:
                if pm["kind"] == "seg":
                    if math.dist(p, closest_on_seg(p, pm["a"], pm["z"])[1]) <= pm["r"] + 1e-6:
                        return FIX
                elif pm["kind"] == "disc" and math.dist(p, pm["p"]) <= pm["r"] + 1e-6:
                    return FIX
            return None

        self.claim_key = {}

        def ckey(p):
            p = (round(p[0], 6), round(p[1], 6))
            if p not in self.claim_key:
                joint = anchored(p)
                self.claim_key[p] = joint if joint is not None else ("C", p[0], p[1])
            return self.claim_key[p]

        for center, points, _ in plan.get("banks", []):
            root = ckey(center)
            for p in points:
                self.union(root, ckey(p))
        for p, d, h in plan.get("in_pad_vias") or []:
            self.union(FIX, ckey(p))
        for la, a, z, w in plan.get("tracks", []):
            if math.dist(a, z) < 1e-9:
                self.prim(
                    id="claim",
                    label="claim pt",
                    net=self.net,
                    kind="seg",
                    layers={la},
                    keys=[ckey(a), ckey(a)],
                    a=tuple(a),
                    z=tuple(a),
                    r=w / 2,
                    claim=True,
                )
                continue
            self.prim(
                id="claim",
                label="claim trk",
                net=self.net,
                kind="seg",
                layers={la},
                keys=[ckey(a), ckey(z)],
                a=tuple(a),
                z=tuple(z),
                r=w / 2,
                claim=True,
            )
        sizing = (plan.get("policy") or {}).get("via_array") or {}
        for center, points, layers in plan.get("banks", []):
            # add_bank's feed copper (centre to every via, each bank layer at its
            # width) is claim copper too: it rides rigidly with the bank.
            for p in points:
                for la, w in layers:
                    if math.dist(center, p) > 1e-9:
                        self.prim(
                            id="claim",
                            label="claim bank feed",
                            net=self.net,
                            kind="seg",
                            layers={la},
                            keys=[ckey(center), ckey(p)],
                            a=tuple(center),
                            z=tuple(p),
                            r=w / 2,
                            claim=True,
                        )
            for p in points:
                self.prim(
                    id="claim",
                    label="claim via",
                    net=self.net,
                    kind="disc",
                    layers=set(self.cu),
                    keys=[ckey(p)],
                    p=tuple(p),
                    r=sizing.get("diameter_mm", 0.45) / 2,
                    via=True,
                    claim=True,
                )
                self.prim(
                    id="claim:hole",
                    label="claim via hole",
                    net=self.net,
                    kind="disc",
                    layers={"HOLE"},
                    keys=[ckey(p)],
                    p=tuple(p),
                    r=sizing.get("drill_mm", 0.3) / 2,
                    claim=True,
                    hole_kind="via",
                )
        for p, d, h in plan.get("in_pad_vias") or []:
            self.prim(
                id="claim",
                label="claim in-pad via",
                net=self.net,
                kind="disc",
                layers={k.F_Cu, k.B_Cu},
                keys=[FIX],
                p=tuple(p),
                r=d / 2,
                via=True,
                claim=True,
                inpad=True,
            )
            self.prim(
                id="claim:hole",
                label="claim in-pad hole",
                net=self.net,
                kind="disc",
                layers={"HOLE"} | {la for la in self.cu if la not in (k.F_Cu, k.B_Cu)},
                keys=[FIX],
                p=tuple(p),
                r=h / 2,
                claim=True,
                hole_kind="inpad",
            )

    @staticmethod
    def vloc(pm, i):
        if pm["kind"] == "seg":
            return pm["a"] if i == 0 else pm["z"]
        if pm["kind"] == "disc":
            return pm["p"]
        return None

    def _joints(self):
        prims = self.prims
        copper = [
            pm
            for pm in prims
            if not pm["claim"] and pm["kind"] in ("seg", "disc") and "HOLE" not in pm["layers"]
        ]
        self.copper = copper
        index = defaultdict(list)
        for pm in copper:
            for i, key in enumerate(pm["keys"]):
                loc = self.vloc(pm, i)
                index[(pm["net"], round(loc[0], 4), round(loc[1], 4))].append(key)
        for keys in index.values():
            for key in keys[1:]:
                self.union(keys[0], key)
        pads = [
            pm
            for pm in prims
            if pm["kind"] == "poly" and pm["zone"] is None and "CRT" not in pm["layers"]
        ]
        self.pad_prims = pads
        inside = defaultdict(list)  # (prim index, vertex) -> same-net pad prims holding it
        for n, pm in enumerate(copper):
            for i, key in enumerate(pm["keys"]):
                loc = self.vloc(pm, i)
                for pp in pads:
                    if pp["net"] != pm["net"] or not (pp["layers"] & pm["layers"]):
                        continue
                    if any(point_in_poly(loc, ring) for ring in pp["rings"]):
                        self.union(pp["keys"][0], key)
                        inside[n, i].append(pp)
        # G1 necks: power/plane copper narrower than a land's required entry width
        # that enters the land (native_electrical's source-bounded neck, whose
        # loss/drop budget is length-bounded) is rigid with the land: its outer
        # vertex binds to the land's joint too, so it can neither stretch nor turn.
        self.rigid_necks = []
        for n, pm in enumerate(copper):
            if (
                pm["kind"] != "seg"
                or pm["via"]
                or self.policy_mode(pm["net"]) not in ("power", "plane")
            ):
                continue
            for i in (0, 1):
                for pp in inside.get((n, i), ()):
                    if pm["width"] + 1e-6 < self.entry_width(pp["id"]):
                        self.union(pp["keys"][0], pm["keys"][1 - i])
                        self.rigid_necks.append(pm["id"])
        self.log["rigid_necks"] = sorted(set(self.rigid_necks))
        # Banks: same-net vias within 1.2 mm that share feed copper translate together.
        vias = [pm for pm in copper if pm["via"]]
        far = defaultdict(set)  # via id -> joints at the far end of its short feeds
        for pm in copper:
            if pm["kind"] != "seg" or math.dist(pm["a"], pm["z"]) > 1.2 + 1e-9:
                continue
            for v in vias:
                if v["net"] != pm["net"]:
                    continue
                if math.dist(pm["a"], v["p"]) < 1e-4:
                    far[v["id"]].add(pm["keys"][1])
                elif math.dist(pm["z"], v["p"]) < 1e-4:
                    far[v["id"]].add(pm["keys"][0])
        for i, v in enumerate(vias):
            for w in vias[i + 1 :]:
                if v["net"] != w["net"] or math.dist(v["p"], w["p"]) > 1.2 + 1e-9:
                    continue
                fv = {self.find(key) for key in far[v["id"]]} - {FIX}
                fw = {self.find(key) for key in far[w["id"]]} - {FIX}
                direct = self.find(w["keys"][0]) in {self.find(key) for key in far[v["id"]]}
                shared = fv & fw
                if shared or direct:
                    self.union(v["keys"][0], w["keys"][0])
                    for root in shared:
                        self.union(v["keys"][0], root)
        is_part = lambda r: isinstance(r, tuple) and r[0] == "P"
        for pm in copper:
            # A short stub from a part-bound vertex to a via rides with the part.
            if pm["kind"] != "seg" or math.dist(pm["a"], pm["z"]) > 1.2:
                continue
            ra, rz = self.find(pm["keys"][0]), self.find(pm["keys"][1])
            for rp, other in ((ra, pm["keys"][1]), (rz, pm["keys"][0])):
                if (
                    is_part(rp)
                    and self.find(other) != FIX
                    and any(
                        v["via"] and self.find(v["keys"][0]) == self.find(other)
                        for v in copper
                        if v["kind"] == "disc"
                    )
                ):
                    self.union(rp, other)
        for pm in copper:
            for i, key in enumerate(pm["keys"]):
                if self._near(self.vloc(pm, i)) > self.radius and not is_part(self.find(key)):
                    self.union(FIX, key)
        # Welds: any other same-net contact moves identically (first order).
        equalities = []
        by_net = defaultdict(list)
        for pm in copper + pads:
            by_net[pm["net"]].append(pm)
        for net, items in by_net.items():
            for i, P in enumerate(items):
                for Q in items[i + 1 :]:
                    if not (P["layers"] & Q["layers"]):
                        continue
                    if P["kind"] == "poly" and Q["kind"] == "poly":
                        continue
                    if P["kind"] == "poly":
                        P, Q = Q, P
                    rp = {self.find(x) for x in P["keys"]}
                    rq = {self.find(x) for x in Q["keys"]}
                    if rp == {FIX} and rq == {FIX}:
                        continue
                    if rp & rq and (len(rp) == 1 or len(rq) == 1):
                        continue
                    pa, pz = (P["a"], P["z"]) if P["kind"] == "seg" else (P["p"], P["p"])
                    if Q["kind"] == "poly":
                        best = min(
                            (seg_seg(pa, pz, c, d) for c, d in poly_edges(Q["rings"])),
                            key=lambda r: r[0],
                        )
                        inside = any(
                            point_in_poly(pa, ring) or point_in_poly(pz, ring)
                            for ring in Q["rings"]
                        )
                        if best[0] - P["r"] > 1e-6 and not inside:
                            continue
                        if rp & rq:
                            continue
                        equalities.append((self.weights_of(P, best[1]), [(Q["keys"][0], 1.0)]))
                        continue
                    qa, qz = (Q["a"], Q["z"]) if Q["kind"] == "seg" else (Q["p"], Q["p"])
                    d, t, u, _, _ = seg_seg(pa, pz, qa, qz)
                    if d - P["r"] - Q["r"] > 1e-6:
                        continue
                    if rp & rq:
                        continue
                    equalities.append((self.weights_of(P, t), self.weights_of(Q, u)))
        self.equalities = equalities

    def entry_width(self, pad_id):
        """The land's required entry width (its terminal contract's outer width,
        else the net's class/electrical width: pnr.pad_entry.required_width)."""
        from pnr.pad_entry import required_width

        if not hasattr(self, "_entry"):
            self._entry = {}
        if pad_id not in self._entry:
            pad = self.pad_objs.get(pad_id)
            try:
                self._entry[pad_id] = required_width(pad, self.rules) if pad is not None else 0.0
            except ValueError:
                self._entry[pad_id] = math.inf  # ambiguous contract: never let it move freely
        return self._entry[pad_id]

    @staticmethod
    def weights_of(pm, param):
        if pm["kind"] == "seg":
            return [(pm["keys"][0], 1 - param), (pm["keys"][1], param)]
        return [(pm["keys"][0], 1.0)]

    def _variables(self):
        roots = sorted({self.find(key) for pm in self.prims for key in pm["keys"]} - {FIX}, key=str)
        self.roots = roots
        self.var = {r: i for i, r in enumerate(roots)}
        members = defaultdict(list)
        for pm in self.prims:
            for key in pm["keys"]:
                r = self.find(key)
                if r != FIX:
                    members[r].append(pm)
        self.members = members
        weights, caps, kinds = [], [], []
        for r in roots:
            ms = members[r]
            if isinstance(r, tuple) and r[0] == "P":
                weights.append(self.weights_spec["part"])
                caps.append(self.part_cap)
                kinds.append("part")
                continue
            if all(m["claim"] for m in ms):
                weights.append(self.weights_spec["claim"])
                caps.append(self.caps["claim"])
                kinds.append("claim")
                continue
            modes = {self.policy_mode(m["net"]) for m in ms if not m["claim"] and m["net"]}
            has_via = any(m["via"] and not m["claim"] for m in ms)
            weights.append(1.0 + 0.5 * sum(1 for m in ms if m["via"] and "HOLE" not in m["layers"]))
            if has_via and modes & {"power", "plane"}:
                caps.append(self.caps["via"])
                kinds.append("power_via")
            elif modes & {"power", "plane"}:
                caps.append(self.caps["power"])
                kinds.append("power")
            else:
                caps.append(self.caps["signal"])
                kinds.append("signal")
        self.weights = weights
        self.var_caps = caps
        self.var_kinds = kinds
        self.log["variables"] = len(roots)

    # ------------------------------------------------------------------ geometry
    def off(self, key, x):
        r = self.find(key)
        if r == FIX:
            return (0.0, 0.0)
        i = self.var[r]
        return (x[2 * i], x[2 * i + 1])

    def geom(self, pm, x):
        if pm["kind"] == "seg":
            return (
                "seg",
                add(pm["a"], self.off(pm["keys"][0], x)),
                add(pm["z"], self.off(pm["keys"][1], x)),
            )
        if pm["kind"] == "disc":
            p = add(pm["p"], self.off(pm["keys"][0], x))
            return ("seg", p, p)
        o = self.off(pm["keys"][0], x)
        return ("poly", [[add(p, o) for p in ring] for ring in pm["rings"]])

    @staticmethod
    def features(G):
        if G[0] == "seg":
            return [(G[1], G[2])]
        return list(poly_edges(G[1]))

    def distance(self, P, Q, x):
        """Signed surface distance and the linearisation data (t on P, s on Q, unit
        normal from P to Q) of two primitives at offsets ``x``."""
        GP, GQ = self.geom(P, x), self.geom(Q, x)
        best = None
        for a, z in self.features(GP):
            for c, d in self.features(GQ):
                r = seg_seg(a, z, c, d)
                if best is None or r[0] < best[0]:
                    best = r
        dd, t, s, cp, cq = best
        inside = False
        if GQ[0] == "poly" and GP[0] == "seg" and any(point_in_poly(cp, ring) for ring in GQ[1]):
            inside = True
            n = sub(cp, cq)
        elif GP[0] == "poly" and GQ[0] == "seg" and any(point_in_poly(cq, ring) for ring in GP[1]):
            inside = True
            n = sub(cp, cq)
        else:
            n = sub(cq, cp)
        length = math.hypot(*n)
        if length < 1e-9:
            base = (
                (GP[2][0] - GP[1][0], GP[2][1] - GP[1][1])
                if GP[0] == "seg" and GP[1] != GP[2]
                else (1.0, 0.0)
            )
            n = (-base[1], base[0])
            length = math.hypot(*n)
        n = (n[0] / length, n[1] / length)
        return (-dd if inside else dd) - P["r"] - Q["r"], t, s, n

    def clearance(self, net):
        from pnr.electrical import net_policy

        return net_policy(net, self.rules)["clearance_mm"] if net else 0.0

    def needs(self, P, Q):
        """Required surface gap (mm) between two primitives, or None (no rule)."""
        if P is Q:
            return None
        lp, lq = P["layers"], Q["layers"]
        fab = self.fab
        if "CRT" in lp or "CRT" in lq:
            return (
                0.0
                if (
                    "CRT" in lp
                    and "CRT" in lq
                    and P["ref"] != Q["ref"]
                    and P.get("crt_side") == Q.get("crt_side")
                )
                else None
            )
        if "HOLE" in lp and "HOLE" in lq:
            if P["id"].split(":")[0] == Q["id"].split(":")[0] and P["id"] != "claim:hole":
                return None
            kinds = {P.get("hole_kind"), Q.get("hole_kind")}
            if "npth" in kinds:
                return None
            if kinds <= {"via", "inpad"}:
                return fab.get("hole_to_hole_mm", 0.25) + 0.001
            if kinds == {"pth"}:
                return fab.get("pth_hole_to_hole_mm", fab.get("hole_to_hole_mm", 0.25)) + 0.001
            return fab.get("hole_to_hole_mm", 0.25) + 0.001
        if "HOLE" in lp or "HOLE" in lq:
            hole, other = (P, Q) if "HOLE" in lp else (Q, P)
            if (
                hole.get("hole_kind") != "inpad"
                or not (hole["layers"] & other["layers"])
                or other["net"] == hole["net"]
            ):
                return None
            return fab.get("hole_clearance_mm", 0.2) + 0.001
        if not (lp & lq):
            return None
        if P["zone"] or Q["zone"]:
            zone, other = (P, Q) if P["zone"] else (Q, P)
            if other["zone"]:
                return None
            if (other["via"] and zone["zone"]["vias"]) or (
                other["kind"] == "seg" and not other["via"] and zone["zone"]["tracks"]
            ):
                return 0.001
            return None
        need = None
        if P["net"] != Q["net"]:
            need = max(self.clearance(P["net"]), self.clearance(Q["net"])) + 0.001
        smd_rule = self.g.via_to_smd_pad
        for V, S in ((P, Q), (Q, P)):
            if V["via"] and S["kind"] == "poly" and S["smd"] and smd_rule is not None:
                if V.get("inpad") and V["net"] == S["net"]:
                    continue
                if V["net"] == S["net"] and any(point_in_poly(V["p"], ring) for ring in S["rings"]):
                    continue
                need = max(need or 0, smd_rule + 0.001)
        return need

    def movable(self, pm):
        return any(self.find(key) != FIX for key in pm["keys"])

    # ------------------------------------------------------------------ solve
    def pairs(self):
        if hasattr(self, "_pairs"):
            return self._pairs
        prims = self.prims
        movable = [self.movable(pm) for pm in prims]
        out = []
        zero = [0.0] * (2 * len(self.roots))
        for i, P in enumerate(prims):
            for j in range(i + 1, len(prims)):
                Q = prims[j]
                if not (movable[i] or movable[j]):
                    continue
                if P["claim"] and Q["claim"]:
                    continue
                need = self.needs(P, Q)
                if need is None:
                    continue
                if self.margin:
                    gap = self.distance(P, Q, zero)[0]
                    need = need + self.margin if gap < need else min(need + self.margin, gap)
                out.append((P, Q, need))
        self._pairs = out
        return out

    def power_lines(self):
        """Existing power/plane track prims with at least one free, non-rigid vertex."""
        if not hasattr(self, "_power_lines"):
            self._power_lines = [
                pm
                for pm in self.prims
                if pm["kind"] == "seg"
                and not pm["claim"]
                and not pm["via"]
                and self.policy_mode(pm["net"]) in ("power", "plane")
                and self.find(pm["keys"][0]) != self.find(pm["keys"][1])
                and self.movable(pm)
            ]
        return self._power_lines

    def length_rows(self, pm, x, margin=1e-4):
        """G1 as QP rows: the line's length change, linearised at ``x``, stays
        within :func:`line_length_limit` of its ORIGINAL length (less ``margin``).
        Returns ``(rows, exact excess at x)``."""
        a = add(pm["a"], self.off(pm["keys"][0], x))
        z = add(pm["z"], self.off(pm["keys"][1], x))
        v = sub(z, a)
        length = math.hypot(*v)
        original = math.dist(pm["a"], pm["z"])
        limit = max(0.0, line_length_limit(original) - margin)
        excess = abs(length - original) - line_length_limit(original)
        if length < 1e-9:
            return [], excess
        u = (v[0] / length, v[1] / length)
        c = defaultdict(float)
        for key, sign in ((pm["keys"][1], 1.0), (pm["keys"][0], -1.0)):
            r = self.find(key)
            if r != FIX:
                j = self.var[r]
                c[2 * j] += sign * u[0]
                c[2 * j + 1] += sign * u[1]
        c = {j: val for j, val in c.items() if abs(val) > 1e-12}
        if not c:
            return [], excess
        # length(x') ~ length + c.(x' - x): keep it in [original - limit, original + limit]
        base = length - sum(val * x[j] for j, val in c.items())
        upper = (
            {j: -val for j, val in c.items()},
            base - original - limit,
            False,
        )  # -c.x' >= base - (orig + lim)
        lower = (dict(c), original - limit - base, False)  #  c.x' >= (orig - lim) - base
        return [upper, lower], excess

    def drift_rows(self, root, i):
        """Linear rows keeping every part of the joint ``root`` (variable ``i``)
        inside the regular polygon inscribed in the circle of radius
        ``total_part_cap`` around its ORIGIN pose: n_k . (drift + d) <= R cos(pi/N)."""
        rows = []
        inner = self.total_part_cap * math.cos(math.pi / DRIFT_SIDES)
        for ref in sorted(self.parts):
            if self.find(("P", ref)) != root:
                continue
            o = self.part_drift.get(ref, (0.0, 0.0))
            for kk in range(DRIFT_SIDES):
                theta = 2 * math.pi * kk / DRIFT_SIDES
                n = (math.cos(theta), math.sin(theta))
                # rows are c.x >= rhs:  -n.d >= n.o - inner
                rows.append(
                    ({2 * i: -n[0], 2 * i + 1: -n[1]}, n[0] * o[0] + n[1] * o[1] - inner, False)
                )
        return rows

    def fixed_claim_conflicts(self):
        """Claims colliding with FIX copper: no displacement can resolve them."""
        zero = [0.0] * (2 * len(self.roots))
        out = []
        for P in self.prims:
            if not P["claim"]:
                continue
            for Q in self.prims:
                if Q["claim"] or self.movable(Q) or self.movable(P):
                    continue
                need = self.needs(P, Q)
                if need is None:
                    continue
                gap = self.distance(P, Q, zero)[0]
                if gap < need - 1e-6:
                    out.append(
                        dict(
                            claim=P["label"],
                            item=Q["id"],
                            label=Q["label"],
                            gap=round(gap, 4),
                            need=round(need, 4),
                        )
                    )
        return out

    def solve(self, iterations=10, deadline=None, tolerance=1e-5):
        """SQP over linearised separation rows. Returns a dict with offsets,
        residual violations and the certificate (largest dual multipliers)."""
        import time

        nv = len(self.roots)
        x = [0.0] * (2 * nv)
        pairs = self.pairs()
        history = []
        certificate = []
        for it in range(iterations):
            cons, meta, worst = [], [], []
            for P, Q, need in pairs:
                s, t, u, n = self.distance(P, Q, x)
                if s > need + self.horizon:
                    continue
                if s < need - tolerance:
                    worst.append((need - s, P, Q))
                c = defaultdict(float)
                for key, alpha in self.weights_of(Q, u):
                    r = self.find(key)
                    if r != FIX:
                        i = self.var[r]
                        c[2 * i] += n[0] * alpha
                        c[2 * i + 1] += n[1] * alpha
                for key, alpha in self.weights_of(P, t):
                    r = self.find(key)
                    if r != FIX:
                        i = self.var[r]
                        c[2 * i] -= n[0] * alpha
                        c[2 * i + 1] -= n[1] * alpha
                c = {i: a for i, a in c.items() if abs(a) > 1e-12}
                if not c:
                    continue
                cons.append((c, need - s + sum(a * x[i] for i, a in c.items()), False))
                meta.append(("sep", P, Q, round(need - s, 4)))
            for lhs, rhs in self.equalities:
                for dim in (0, 1):
                    c = defaultdict(float)
                    for key, alpha in lhs:
                        r = self.find(key)
                        if r != FIX:
                            c[2 * self.var[r] + dim] += alpha
                    for key, alpha in rhs:
                        r = self.find(key)
                        if r != FIX:
                            c[2 * self.var[r] + dim] -= alpha
                    c = {i: a for i, a in c.items() if abs(a) > 1e-12}
                    if c:
                        cons.append((c, 0.0, True))
                        meta.append(("weld", None, None, 0))
            for pm in self.prims:
                if pm["kind"] != "seg" or pm["claim"]:
                    continue
                ra, rz = self.find(pm["keys"][0]), self.find(pm["keys"][1])
                if ra == FIX or rz == FIX or ra == rz:
                    continue
                u = sub(pm["z"], pm["a"])
                length = math.hypot(*u)
                if length < 1e-6:
                    continue
                u = (u[0] / length, u[1] / length)
                ia, iz = self.var[ra], self.var[rz]
                c = defaultdict(float)
                c[2 * iz + 1] += u[0]
                c[2 * ia + 1] -= u[0]
                c[2 * iz] -= u[1]
                c[2 * ia] += u[1]
                cons.append(({i: a for i, a in c.items() if abs(a) > 1e-12}, 0.0, True))
                meta.append(("angle", pm, None, 0))
            for r in self.roots:
                i = self.var[r]
                cap = self.var_caps[i]
                for dim in (0, 1):
                    j = 2 * i + dim
                    cons.append(({j: 1.0}, max(-cap, x[j] - self.step), False))
                    meta.append(("box", r, None, 0))
                    cons.append(({j: -1.0}, -min(cap, x[j] + self.step), False))
                    meta.append(("box", r, None, 0))
                if self.var_kinds[i] == "part":
                    rows = self.drift_rows(r, i)
                    cons.extend(rows)
                    meta.extend(("drift", r, None, 0) for _ in rows)
            stretched = []
            for pm in self.power_lines():
                rows, excess = self.length_rows(pm, x)
                cons.extend(rows)
                meta.extend(("length", pm, None, 0) for _ in rows)
                if excess > 0:
                    stretched.append((excess, pm["id"]))
            weights = [self.weights[i // 2] for i in range(2 * nv)]
            solved, lam, ok, its = hildreth(cons, weights, deadline=deadline)
            worst.sort(key=lambda w: -w[0])
            binding = sorted(
                (
                    (lam[k], meta[k])
                    for k in range(len(cons))
                    if lam[k] > 1e-6 and meta[k][0] in ("sep", "box")
                ),
                key=lambda v: -v[0],
            )[:12]
            certificate = [
                dict(
                    multiplier=round(l, 4),
                    kind=m[0],
                    a=(m[1]["id"] if isinstance(m[1], dict) else str(m[1])),
                    a_label=(m[1]["label"] if isinstance(m[1], dict) else None),
                    b=(m[2]["id"] if isinstance(m[2], dict) else None),
                    b_label=(m[2]["label"] if isinstance(m[2], dict) else None),
                    b_fixed=(not self.movable(m[2]) if isinstance(m[2], dict) else None),
                    a_fixed=(not self.movable(m[1]) if isinstance(m[1], dict) else None),
                )
                for l, m in binding
            ]
            history.append(
                dict(
                    iteration=it,
                    violations=len(worst),
                    stretched=len(stretched),
                    worst=[(round(a, 4), P["label"], Q["label"]) for a, P, Q in worst[:6]],
                    qp_converged=ok,
                    qp_iterations=its,
                    rows=len(cons),
                    max_offset=round(
                        max(
                            (math.hypot(solved[2 * i], solved[2 * i + 1]) for i in range(nv)),
                            default=0,
                        ),
                        4,
                    ),
                )
            )
            if not worst and not stretched and it > 0:
                break
            x = solved
            if deadline is not None and time.monotonic() > deadline:
                break
        final = []
        for P, Q, need in pairs:
            s = self.distance(P, Q, x)[0]
            if s < need - tolerance:
                final.append(
                    dict(
                        gap=round(s, 4),
                        need=round(need, 4),
                        a=P["id"],
                        a_label=P["label"],
                        b=Q["id"],
                        b_label=Q["label"],
                    )
                )
        self.x = x
        return dict(
            offsets={
                str(r): [round(x[2 * self.var[r]], 5), round(x[2 * self.var[r] + 1], 5)]
                for r in self.roots
                if math.hypot(x[2 * self.var[r]], x[2 * self.var[r] + 1]) > 1e-5
            },
            final_violations=final,
            history=history,
            certificate=certificate,
            variables=len(self.roots),
            pairs=len(pairs),
            equalities=len(self.equalities),
        )

    # ------------------------------------------------------------------ results
    def moved(self, x=None):
        """Per moved native item: uid, net, kind, from, to, displacement and the
        track length change."""
        x = self.x if x is None else x
        out = []
        seen = set()
        for pm in self.prims:
            if pm["claim"] or "HOLE" in pm["layers"] or "CRT" in pm["layers"] or pm["id"] in seen:
                continue
            if pm["kind"] == "seg":
                a, z = add(pm["a"], self.off(pm["keys"][0], x)), add(
                    pm["z"], self.off(pm["keys"][1], x)
                )
                disp = max(math.dist(a, pm["a"]), math.dist(z, pm["z"]))
                if disp < 1e-6:
                    continue
                seen.add(pm["id"])
                out.append(
                    dict(
                        uid=pm["id"],
                        net=pm["net"],
                        kind="track",
                        layer=pm["label"].rsplit(":", 1)[-1],
                        width_mm=pm.get("width"),
                        **{"from": [list(pm["a"]), list(pm["z"])], "to": [list(a), list(z)]},
                        disp_mm=round(disp, 4),
                        length_delta_mm=round(math.dist(a, z) - math.dist(pm["a"], pm["z"]), 4),
                        length_mm=round(math.dist(pm["a"], pm["z"]), 4),
                        mode=self.policy_mode(pm["net"])
                    )
                )
            elif pm["kind"] == "disc" and pm["via"]:
                p = add(pm["p"], self.off(pm["keys"][0], x))
                disp = math.dist(p, pm["p"])
                if disp < 1e-6:
                    continue
                seen.add(pm["id"])
                out.append(
                    dict(
                        uid=pm["id"],
                        net=pm["net"],
                        kind="via",
                        **{"from": list(pm["p"]), "to": list(p)},
                        disp_mm=round(disp, 4),
                        mode=self.policy_mode(pm["net"])
                    )
                )
        return out

    def nudges(self, x=None):
        x = self.x if x is None else x
        out = []
        for ref in sorted(self.parts):
            o = self.off(("P", ref), x)
            if math.hypot(*o) > 1e-6:
                drift = self.part_drift.get(ref, (0.0, 0.0))
                out.append(
                    dict(
                        ref=ref,
                        dx=round(o[0], 5),
                        dy=round(o[1], 5),
                        disp_mm=round(math.hypot(*o), 4),
                        total_disp_mm=round(math.hypot(o[0] + drift[0], o[1] + drift[1]), 4),
                    )
                )
        return out

    def moved_plan(self, x=None):
        """The wish plan with every claim vertex at its solved position."""
        x = self.x if x is None else x
        mv = lambda p: tuple(
            round(v, 6)
            for v in add(
                tuple(p), self.off(self.claim_key.get((round(p[0], 6), round(p[1], 6)), FIX), x)
            )
        )
        plan = dict(self.plan)
        plan["tracks"] = [(la, mv(a), mv(z), w) for la, a, z, w in self.plan.get("tracks", [])]
        plan["banks"] = [
            (mv(c), [mv(p) for p in pts], layers) for c, pts, layers in self.plan.get("banks", [])
        ]
        if self.plan.get("in_pad_vias"):
            plan["in_pad_vias"] = [(tuple(p), d, h) for p, d, h in self.plan["in_pad_vias"]]
        return plan

    def apply(self, x=None):
        """Write solved offsets into the live board, keeping every uuid (tracks
        SetStart/SetEnd, vias SetPosition, footprints SetPosition; 1 nm snap)."""
        k = self.k
        x = self.x if x is None else x
        vec = lambda p: k.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6))
        done = set()
        for pm in self.prims:
            if (
                pm["claim"]
                or "HOLE" in pm["layers"]
                or "CRT" in pm["layers"]
                or pm["kind"] == "poly"
            ):
                continue
            if pm["id"] in done:
                continue
            item = self.items.get(pm["id"])
            if item is None:
                continue
            done.add(pm["id"])
            if pm["kind"] == "seg":
                a, z = add(pm["a"], self.off(pm["keys"][0], x)), add(
                    pm["z"], self.off(pm["keys"][1], x)
                )
                if a != pm["a"] or z != pm["z"]:
                    item.SetStart(vec(a))
                    item.SetEnd(vec(z))
            else:
                p = add(pm["p"], self.off(pm["keys"][0], x))
                if p != pm["p"]:
                    item.SetPosition(vec(p))
        for ref in self.parts:
            o = self.off(("P", ref), x)
            if o != (0.0, 0.0) and ref in self.footprints:
                f = self.footprints[ref]
                f.SetPosition(f.GetPosition() + k.VECTOR2I(round(o[0] * 1e6), round(o[1] * 1e6)))
