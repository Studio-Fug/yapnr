"""Inspectable static-pose global objective; shared allocations sum to board cost.

This evaluates the actual global placer's formula at rigid, observed poses. It
is not the historical soft-rotation loss or the legalizer/route-probe objective.
A counterfactual field uses whole-board delta, not an allocated component share.

PNR_COMPACT (:mod:`pnr.place.compact`, default off): as in the global placer, a part's
offset courtyard (``COURTYARD``) spreads, stays in the outline, aligns and avoids
keep-outs with its body box centred at ``pos`` plus its offset, and the overlap and
keep-out terms keep the courtyard gap (``LEGALIZE``).
"""

import fnmatch
import math

import numpy as np

from .geometry import keepout_rects, occupied_sides, outline_size, resolve_fixed_poses

TERMS = (
    "wirelength",
    "spread_overlap",
    "outline",
    "plane_area",
    "plane_separation",
    "edge_alignment",
    "group_radius",
    "keepout",
    "capacitor_loop",
    "power_trunk",
    "power_loop",
    "power_tap",
    "lex_guard",
)
LABELS = (
    "Smooth wirelength",
    "Courtyard spreading",
    "Outline penalty",
    "Plane bounding-box area",
    "Plane separation",
    "Edge alignment",
    "Group radius",
    "Keepout overlap",
    "Local capacitor loop",
    "Power trunk wirelength (stage 1, shown)",
    "Power hot loop (stage 1, shown)",
    "Power tap gap",
    "Lexicographic stage guard",
)
UNITS = ("mm", "mm²", "mm²", "mm²", "mm²", "mm²", "mm²", "mm²", "mm", "mm", "mm", "mm", "mm")
DEFAULTS = dict(
    gamma=1.0, spread=1.0, w_spread=1.0, w_bound=20.0, w_keep=40.0, w_plane=0.05, w_plane_sep=0.35
)
# Power-first terms (indices 9..12) exist only when ``roles`` is given: then the
# objective is the final (stage-3) staged loss of pnr.place.power_first, i.e.
# 'wirelength' holds J3's nets, 'power_tap' J3's taps, 'lex_guard' the weighted
# guards on J1/J2, and 'power_trunk'/'power_loop' show raw J1 parts at weight 0.
BASE_TERMS = 9
# The global placer's region and align terms (pnr.place.regions.GlobalTerms) are
# appended after the others only when a design declares a region or an align.
RELATED_TERMS = (("region", "Region protrusion", "mm²"), ("alignment", "Alignment spread", "mm²"))


class Objective:
    def __init__(
        self,
        graph,
        constraints,
        *,
        parameters=None,
        inflation=None,
        effective_offsets=None,
        effective_half=None,
        roles=None,
        pf_state=None,
        effective_shift=None
    ):
        self.graph = graph
        self.constraints = constraints
        self.cfg = dict(DEFAULTS, **(parameters or {}))
        self.refs = [c.ref for c in graph.components]
        self.index = {r: i for i, r in enumerate(self.refs)}
        self.n = len(self.refs)
        if set(self.cfg) - set(DEFAULTS):
            raise ValueError("Unknown objective parameter")
        if pf_state is not None and roles is None:
            raise ValueError("pf_state requires power-first roles")
        self.roles = roles
        self.nterms = len(TERMS) if roles is not None else BASE_TERMS
        self.pf = None
        if any(not math.isfinite(v) or v < 0 for v in self.cfg.values()) or self.cfg["gamma"] <= 0:
            raise ValueError("Invalid objective parameter")
        self.positions = np.array([c.pos for c in graph.components], dtype=float)
        self.fixed = resolve_fixed_poses(graph, constraints)
        self.movable = np.array([c.ref not in self.fixed for c in graph.components])
        self.pin_owner = []
        offsets = []
        self.keys = {}
        self.all_keys = {}
        halves = []
        shifts = []
        from .geometry import body_shift, compact_body

        for i, c in enumerate(graph.components):
            t = math.radians(c.rot)
            ct, st = math.cos(t), math.sin(t)
            scale = max(1.0, self.cfg["spread"], (inflation or {}).get(c.ref, 1.0))
            # Same cardinal extent model as global_place at one-hot rotation.
            if abs(c.rot / 90 - round(c.rot / 90)) > 1e-5:
                raise ValueError("Global placer only supports cardinal rotations: " + c.ref)
            body = compact_body(c)  # PNR_COMPACT offset courtyard (None: centred)
            if body is None:
                h = np.array(c.courtyard) / 2 * scale
            else:
                h = np.array((body[2] - body[0], body[3] - body[1])) / 2 * scale
            halves.append(h[::-1] if round(c.rot / 90) % 2 else h)
            shifts.append(body_shift(c) or (0.0, 0.0))
            for pad in c.pads:
                self.keys[c.ref, pad.name] = len(offsets)
                self.all_keys.setdefault((c.ref, pad.name), []).append(len(offsets))
                self.pin_owner.append(i)
                x, y = pad.offset
                offsets.append((x * ct - y * st, x * st + y * ct))
        self.half = np.array(halves)
        # Body-centre offsets from the origins (PNR_COMPACT offset courtyards), else None.
        self.shift = None
        if any(s != (0.0, 0.0) for s in shifts) or effective_shift is not None:
            self.shift = np.array(shifts if effective_shift is None else effective_shift, float)
        self.pin_owner = np.array(self.pin_owner, dtype=int)
        self.offsets = np.array(offsets, dtype=float).reshape((-1, 2))
        if effective_offsets is not None:
            self.offsets = np.asarray(effective_offsets, dtype=float)
        if effective_half is not None:
            self.half = np.asarray(effective_half, dtype=float)
        self.nets = []
        patterns = [p for nc in constraints.net_classes if nc.plane_layer for p in nc.nets]
        for net in graph.nets:
            pins = [self.keys[tuple(p)] for p in net.pins if tuple(p) in self.keys]
            if len(pins) >= 2:
                self.nets.append(
                    dict(
                        name=net.name,
                        pins=pins,
                        owners=sorted(set(self.pin_owner[pins])),
                        plane=any(fnmatch.fnmatch(net.name, p) for p in patterns),
                    )
                )
        self.overlap = np.array(
            [
                [bool(set(occupied_sides(a)) & set(occupied_sides(b))) for b in graph.components]
                for a in graph.components
            ]
        )
        self.overlap = np.triu(self.overlap, 1)
        from .compact import placement_clearance

        self.clearance = placement_clearance(constraints)
        self.width, self.height = outline_size(graph, constraints)
        self.keepouts = keepout_rects(graph, constraints, self.fixed)
        self.cap = []
        self.edges = []
        self.groups = []
        for con in constraints.constraints:
            if con.kind == "edge_align":
                for ref in con.refs:
                    if ref in self.index:
                        self.edges.append((self.index[ref], con.params["edge"], con.weight or 1.0))
            if con.kind == "group" and con.params.get("anchor") in self.index:
                anchor = self.index[con.params["anchor"]]
                for ref in con.refs:
                    if ref in self.index and self.index[ref] != anchor:
                        self.groups.append(
                            (
                                self.index[ref],
                                anchor,
                                float(con.params.get("radius_mm") or 5.0),
                                con.weight or 1.0,
                            )
                        )
            if con.kind == "capacitor_loop":
                for link in con.params["links"]:
                    src = self.all_keys[tuple(link["from"])]
                    dst = [i for key in link["to"] for i in self.all_keys[tuple(key)]]
                    self.cap.append((src, dst, sorted(set(self.pin_owner[src + dst])), con.weight))
        if roles is not None:
            self._power_first(roles, pf_state)
        self.related = []
        from .regions import declared

        if declared(constraints):
            self._related()
        self.term_keys = list(TERMS[: self.nterms]) + (
            [k for k, _, _ in RELATED_TERMS] if self.related else []
        )
        self.nall = len(self.term_keys)

    def _related(self):
        """Region/align terms at the observed rigid poses (GlobalTerms at one-hot
        rotations): ('region', comp, corner offsets, area, weight) and ('align',
        members, anchor offsets, axis index, weight)."""
        from pnr.constraints import Enforcement

        from .regions import (
            GP_ALIGN_WEIGHT,
            GP_REGION_WEIGHT,
            align_rules,
            anchor_offset,
            anchor_spec,
            area_of,
            placed_boxes,
            region_rules,
        )

        comps = self.graph.components
        for con in region_rules(self.constraints):
            weight = GP_REGION_WEIGHT if con.enforcement is Enforcement.HARD else con.weight
            for ref in con.refs:
                i = self.index.get(ref)
                if i is None or not self.movable[i]:
                    continue
                c = comps[i]
                offsets = []
                for b in placed_boxes(c, con, pos=(0.0, 0.0)):
                    offsets += [(b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])]
                self.related.append(("region", i, np.array(offsets), area_of(con), weight))
        for con in align_rules(self.constraints):
            members = [self.index[r] for r in con.refs if r in self.index]
            if len(members) < 2:
                continue
            axis = con.params["axis"]
            offsets = np.array(
                [anchor_offset(comps[i], anchor_spec(con, comps[i].ref), axis) for i in members]
            )
            weight = GP_ALIGN_WEIGHT if con.enforcement is Enforcement.HARD else con.weight
            self.related.append(("align", members, offsets, 0 if axis == "x" else 1, weight))

    def _power_first(self, roles, pf_state):
        """Final-stage staged loss factors; without pf_state the stage-3 end values are used and the guard is 0."""
        from pnr.constraints import Enforcement

        from .power_first import (
            EPS,
            GAP_EPS2,
            GUARD_SCALE,
            OMEGA,
            OVERLAP_RAMP,
            RHO_RAMP,
            T_SOFT,
            Compiled,
        )

        W = float(roles["W"])
        state = dict(pf_state or {})
        if (
            abs(state.get("t", T_SOFT) - T_SOFT) > 1e-12
            or abs(state.get("gap_eps2", GAP_EPS2) - GAP_EPS2) > 1e-12
        ):
            raise ValueError("pf_state softmin constants differ from runtime")
        self.pf = dict(
            compiled=Compiled(self.graph, roles),
            w_ov=float(state.get("w_ov", self.cfg["w_spread"] * OVERLAP_RAMP[2][1])),
            bound=float(state.get("bound", self.cfg["w_bound"] * W)),
            group=float(state.get("group", 0.5 * W * RHO_RAMP[1])),
            clearance=float(state.get("overlap_clearance", self.clearance)),
            stars={int(k): float(v) for k, v in (state.get("stars") or {}).items()},
            eps=tuple(state.get("eps", EPS)),
            omega=float(state.get("omega", OMEGA)),
            scale=float(state.get("guard_scale", GUARD_SCALE)),
        )
        self.pf_groups = []
        for con in self.constraints.constraints:
            if con.kind == "group" and con.params.get("anchor") in self.index:
                anchor = self.index[con.params["anchor"]]
                radius = float(con.params.get("radius_mm") or 5.0)
                margin = min(0.5, radius / 4) if con.enforcement is Enforcement.HARD else 0.0
                for ref in con.refs:
                    if ref in self.index and self.index[ref] != anchor:
                        self.pf_groups.append(
                            (self.index[ref], anchor, radius - margin, con.weight or 1.0)
                        )

    def evaluate(self, positions=None):
        pos = np.asarray(self.positions if positions is None else positions, dtype=float)
        if pos.ndim == 2:
            pos = pos[None, :, :]
        if pos.shape[1:] != (self.n, 2):
            raise ValueError("position shape")
        b = len(pos)
        raw = np.zeros((b, self.n, self.nall))
        weighted = np.zeros_like(raw)
        pf = self.pf

        def add(term, value, owners, weight=1.0):
            owners = list(owners)
            v = np.broadcast_to(np.asarray(value, dtype=float), (b,)) / len(owners)
            for i in owners:
                raw[:, i, term] += v
                weighted[:, i, term] += v * weight

        pp = pos[:, self.pin_owner, :] + self.offsets
        gamma = self.cfg["gamma"]
        boxes = []

        def bbox(pins):
            q = pp[:, pins, :]
            return -gamma * np.logaddexp.reduce(-q / gamma, axis=1), gamma * np.logaddexp.reduce(
                q / gamma, axis=1
            )

        for net in self.nets:
            lo, hi = bbox(net["pins"])
            if pf is None:
                add(0, (hi - lo).sum(1), net["owners"])
            if net["plane"]:
                boxes.append((lo, hi, net["owners"]))
                add(3, np.prod(hi - lo, axis=1), net["owners"], self.cfg["w_plane"])
        if pf is not None:
            J = {1: 0.0, 2: 0.0, 3: 0.0}
            owners = {1: set(), 2: set(), 3: set()}
            for e in pf["compiled"].numpy_terms(pp, gamma):
                s = e["stage"]
                v = e["weight"] * e["value"]
                J[s] = J[s] + v
                owners[s].update(e["owners"])
                if s == 3:
                    add(0 if e["kind"] == "bbox" else 11, v, e["owners"])
                elif s == 1:
                    add(9 if e["kind"] == "bbox" else 10, v, e["owners"], 0.0)
            for j in (1, 2):
                if j in pf["stars"] and owners[j]:
                    star = pf["stars"][j]
                    scale = pf["scale"] * star + 1e-6
                    add(
                        12,
                        pf["omega"]
                        * scale
                        * np.logaddexp(0.0, (J[j] - (1 + pf["eps"][j - 1]) * star) / scale),
                        sorted(owners[j]),
                    )
        # Courtyard centres (PNR_COMPACT offset courtyards: pos plus the body offsets).
        body = pos if self.shift is None else pos + self.shift
        delta = np.abs(body[:, :, None, :] - body[:, None, :, :])
        span = (
            self.half[:, None, :]
            + self.half[None, :, :]
            + (self.clearance if pf is None else pf["clearance"])
        )
        pairs = np.prod(np.maximum(span - delta, 0), axis=-1) * self.overlap
        contribution = (pairs.sum(1) + pairs.sum(2)) / 2
        raw[:, :, 1] = contribution
        weighted[:, :, 1] = contribution * (self.cfg["w_spread"] if pf is None else pf["w_ov"])
        bound = (
            np.maximum(self.half - body, 0) ** 2
            + np.maximum(body + self.half - np.array([self.width, self.height]), 0) ** 2
        ).sum(2) * self.movable
        raw[:, :, 2] = bound
        weighted[:, :, 2] = bound * (self.cfg["w_bound"] if pf is None else pf["bound"])
        for ia, (lo, hi, owners) in enumerate(boxes):
            for lo2, hi2, owners2 in boxes[ia + 1 :]:
                add(
                    4,
                    np.maximum(np.minimum(hi, hi2) - np.maximum(lo, lo2), 0).prod(1),
                    sorted(set(owners + owners2)),
                    self.cfg["w_plane_sep"],
                )
        for i, edge, weight in self.edges:
            axis = 1 if edge in ("south", "north") else 0
            extent = self.half[i, axis]
            target = (
                extent
                if edge in ("south", "west")
                else (self.height if axis else self.width) - extent
            )
            add(5, (body[:, i, axis] - target) ** 2, [i], weight)
        for i, j, radius, weight in self.groups if pf is None else self.pf_groups:
            add(
                6,
                np.maximum(np.linalg.norm(pos[:, i] - pos[:, j], axis=1) - radius, 0) ** 2,
                [i, j],
                weight * (1.0 if pf is None else pf["group"]),
            )
        for k in self.keepouts:
            delta = np.abs(body - np.array([k.cx, k.cy]))
            area = (
                np.maximum(
                    self.half + np.array([k.w / 2, k.h / 2]) + self.clearance - delta, 0
                ).prod(2)
                * self.movable
            )
            raw[:, :, 7] += area
            weighted[:, :, 7] += area * self.cfg["w_keep"]
        for src, dst, owners, weight in self.cap:
            dist = np.linalg.norm(pp[:, src, None, :] - pp[:, None, dst, :], axis=-1).min(
                axis=(1, 2)
            )
            add(8, dist, owners, weight)
        for kind, who, offsets, data, weight in self.related:
            if kind == "region":
                pts = pos[:, who, None, :] + offsets[None]
                add(self.nterms, data.dist2(pts[..., 0], pts[..., 1]).sum(1), [who], weight)
            else:
                anchors = pos[:, who, data] + offsets[None]
                spread = ((anchors - anchors.mean(1, keepdims=True)) ** 2).sum(1)
                add(self.nterms + 1, spread, who, weight)
        return raw, weighted

    def report(self):
        raw, w = self.evaluate()
        components = {}
        for i, ref in enumerate(self.refs):
            weights = [
                [1.0],
                [self.cfg["w_spread"]],
                [self.cfg["w_bound"]],
                [self.cfg["w_plane"]],
                [self.cfg["w_plane_sep"]],
                sorted(set(v[2] for v in self.edges if v[0] == i)),
                sorted(set(v[3] for v in self.groups if i in v[:2])),
                [self.cfg["w_keep"]],
                sorted(set(v[3] for v in self.cap if i in v[2])),
            ]
            if self.related:
                extra = [
                    sorted(
                        set(
                            v[4]
                            for v in self.related
                            if v[0] == kind and (v[1] == i if kind == "region" else i in v[1])
                        )
                    )
                    for kind in ("region", "align")
                ]
            if self.pf is not None:
                weights[1:3] = [[self.pf["w_ov"]], [self.pf["bound"]]]
                weights[6] = sorted(
                    set(v[3] * self.pf["group"] for v in self.pf_groups if i in v[:2])
                )
                weights += [[0.0], [0.0], [1.0], [1.0]]
            labels, units = list(LABELS[: self.nterms]), list(UNITS[: self.nterms])
            if self.related:
                weights += extra
                labels += [label for _, label, _ in RELATED_TERMS]
                units += [unit for _, _, unit in RELATED_TERMS]
            terms = [
                dict(
                    key=k,
                    label=labels[t],
                    raw_share=float(raw[0, i, t]),
                    weighted=float(w[0, i, t]),
                    unit=units[t],
                    weights=weights[t],
                    effective_weight=float(w[0, i, t] / raw[0, i, t]) if raw[0, i, t] else None,
                )
                for t, k in enumerate(self.term_keys)
            ]
            locks = [
                c.params
                for c in self.constraints.constraints
                if c.kind == "fixed" and ref in c.refs
            ]
            components[ref] = dict(
                mobility=dict(
                    source_fixed=any(not x.get("row_trial") for x in locks),
                    row_trial=next((x["row_trial"] for x in locks if x.get("row_trial")), None),
                ),
                ref=ref,
                position=self.positions[i].tolist(),
                rotation=self.graph.components[i].rot,
                side=self.graph.components[i].side,
                fixed=ref in self.fixed,
                terms=terms,
                total=sum(v["weighted"] for v in terms),
            )
        out = dict(
            schema="pnr-placement-cost-v1",
            scope="retrospective-rigid-pose-global-objective",
            parameters=self.cfg,
            allocation="Each shared term is split equally among distinct participants; all component totals sum to the board objective.",
            units="weighted objective units; raw term units differ",
            board_total=float(w.sum()),
            components=components,
            not_in_objective=[
                "Legalizer nearest-target and channel cost",
                "Native routing failures",
                "Full-electrical acceptance",
                "Detailed routed-copper congestion",
            ],
            limitations=[
                "The historical relaxed-rotation state and per-round inflation may not have been recorded.",
                "This static objective evaluation is not a DRC, timing, power or manufacturing certificate.",
            ],
        )
        if self.pf is not None:
            out["power_first"] = dict(
                objective="final stage-3 staged loss (pnr.place.power_first)",
                stars={str(k): v for k, v in self.pf["stars"].items()},
                w_ov=self.pf["w_ov"],
                bound=self.pf["bound"],
                group=self.pf["group"],
                overlap_clearance=self.pf["clearance"],
                eps=list(self.pf["eps"]),
                omega=self.pf["omega"],
                guard_scale=self.pf["scale"],
            )
        return out

    def field(self, ref, pitch=1.5):
        from .geometry import courtyard_rect, hard_group_limits, placement_rects, resolve_hard_sides
        from .metrics import hard_violations

        if ref not in self.index:
            raise ValueError("Unknown component")
        if not 0.5 <= pitch <= 5:
            raise ValueError("Field pitch out of range")
        idx = self.index[ref]
        xs = np.arange(pitch / 2, self.width, pitch)
        ys = np.arange(pitch / 2, self.height, pitch)
        sites = np.array([(x, y) for y in ys for x in xs])
        _, base = self.evaluate()
        board_base = float(base.sum())
        local_base = float(base[0, idx].sum())
        values = []
        shares = []
        term_delta = []
        for start in range(0, len(sites), 64):
            chunk = sites[start : start + 64]
            p = np.broadcast_to(self.positions, (len(chunk), self.n, 2)).copy()
            p[:, idx] = chunk
            _, w = self.evaluate(p)
            values.extend((w.sum((1, 2)) - board_base).tolist())
            shares.extend(w[:, idx, :].sum(1).tolist())
            term_delta.extend((w.sum(1) - base.sum(1)).tolist())
        baseline_violations = hard_violations(self.graph, self.constraints)
        regions = {c.ref: placement_rects(c) for c in self.graph.components}
        limits = hard_group_limits(self.constraints, {c.ref: c.pos for c in self.graph.components})
        sides = resolve_hard_sides(self.constraints)
        from .regions import ref_ok

        def legal(c):
            rect = courtyard_rect(c)
            return (
                rect.inside(self.width, self.height)
                and (
                    not self.related
                    or ref_ok(
                        [c if x.ref == c.ref else x for x in self.graph.components],
                        self.constraints,
                        c.ref,
                    )
                )
                and (c.ref not in sides or c.side == sides[c.ref])
                and not any(rect.overlaps(k) for k in self.keepouts)
                and not any(
                    math.dist(c.pos, (x, y)) > radius + 1e-9
                    for x, y, radius in limits.get(c.ref, ())
                )
                and not any(
                    c.ref != ref and side == other_side and area.overlaps(other)
                    for ref, rs in regions.items()
                    for other_side, other in rs
                    for side, area in placement_rects(c)
                )
            )

        comp = self.graph.components[idx]
        old = comp.pos
        valid = []
        try:
            for site in sites:
                comp.pos = tuple(map(float, site))
                valid.append(False if ref in self.fixed or comp.locked else bool(legal(comp)))
        finally:
            comp.pos = old
        return dict(
            ref=ref,
            scope="Counterfactual board-objective change; all other components, side and rotation held fixed",
            pitch_mm=pitch,
            nx=len(xs),
            ny=len(ys),
            origin=[float(xs[0]), float(ys[0])],
            values=values,
            component_scores=shares,
            term_delta=term_delta,
            term_keys=list(self.term_keys),
            legal=valid,
            current_position=list(old),
            current_component_score=local_base,
            current_board_score=board_base,
            fixed=ref in self.fixed or comp.locked,
            mobility=self.report()["components"][ref].get("mobility"),
            baseline_placement_findings=baseline_violations,
            legality="Candidate outline/courtyard/group/keepout screen, even when other baseline components have conflicts; not copper DRC or rerouting",
        )
