"""Native, transactional power/plane and coupled-pair routing adapters.

Add-only power repair preserves current-carrying trunks/arrays. A layer change
uses a complete source-sized via bank, not a single signal via. Pair replacement
is atomic for both nets; all old pad connectivity must survive. This module is
run by native_loop under KiCad Python, never in the torch interpreter.

Under a fab profile with a filled via-in-pad policy (jlc-pofv, 5B) a terminal
land without any budgeted surface attach may instead be attached by a
current-sized in-pad via array whose trunk continues on another layer
(:func:`power_plan`, pnr.via_in_pad.array_attach); legacy rules never do.

Pair-chain opt-in flags (environment, default off; unset/any other value keeps
the original behaviour exactly):

PNR_PAIR_POST_BRIDGE_SURFACE=1
    Every routed surface leg of a pair chain is checked with the exact endpoint
    graph (path_metrics from the declared origin to this leg's end, over all
    planned copper plus the new leg) before it is committed. Oracle.clear ignores
    same-net copper, so a leg can cross an earlier same-net stub (e.g. the
    via-to-pad fanout of a preceding layer bridge) and close a loop that the
    final check could only reject as pair_endpoint_graph_invalid after the whole
    chain was planned. A leg whose graph is not a valid tree is treated as
    unrouted (status pair_surface_leg_cycle) and the stage falls back. When the
    previous stage ended on a via bridge, the first fallback is a surface leg
    that starts at that bridge's via pair (offsets = measured prefix minus the
    via-to-pad fanout), so the intermediate pad fanout becomes a stub: the same
    topology the existing bridge-to-bridge via reuse already produces, with no
    new vias. Only if that also fails does the stage use the existing via-reuse
    layer bridge. No electrical limit (uncoupled budget, skew, clearance,
    reference plane, via geometry) is changed; every leg is still judged by
    solve_pair, the reference validator and the final endpoint/skew checks.
PNR_PAIR_JOINT_FAIR=1
    Joint-topology scheduling in pair_plan: the first joint configuration of each
    bridge hand is capped at (joint window remaining)/(hands not yet tried), so
    the second hand is not starved when the first one uses the whole window
    (60 s of the default 90 s trial). Later configurations are unchanged.
PNR_PAIR_FALLBACK_RESERVE_SECONDS=<s>   (default 30 = unchanged)
    Upper bound of the time pair_plan keeps after the joint-topology window for
    the ten legacy topologies (min(<s>, remaining/3)). With the default 90 s trial
    those get 3 s each and, on the Splanc USB chain, never route; a smaller value
    widens the joint window that PNR_PAIR_JOINT_FAIR splits between the hands.
PNR_PAIR_JOINT_HAND_FIRST=+1|-1   (normally set by paired_bootstrap, see
    PNR_PAIR_HAND_SWAP_TRIAL there): the named bridge hand runs first within each
    joint seed, and so gets the whole first-config window instead of the other hand.
PNR_PAIR_PREFER_INLINE=1
    Route choice in pair_plan: the score (vias, copper length) becomes (vias,
    stub legs, copper length), so among complete routes with equal via count one
    that keeps every intermediate device (ESD) in line beats one that leaves its
    pad fanout as a stub. Selection only; no route is added or relaxed.

src13 flags (same convention: unset = src12b behaviour, byte for byte):

PNR_PAIR_PER_RUN_UNCOUPLED=1
    max_uncoupled_mm bounds each CONTINUOUS uncoupled run separately (owner
    decision 2026-09-29), not one budget shared by both ends of a layer bridge.
    pair_layer_bridge gives the bridge head cap-(source surface fanout+entry
    budget+uncoupled run already arriving at the start junction) and the tail
    cap-(target surface fanout); a candidate with no budget left at an end is
    skipped. The search first runs exactly the legacy search (shared budget),
    then, if that finds nothing, a second pass where solve_pair bounds each end
    by its own budget; surface legs keep the legacy fanout bound. Budgets only
    bound fanout geometry; the exact run measurement accepts or rejects. Every
    leg (surface or bridge) that starts where the previous leg ended (an
    intermediate pad, or the previous bridge's via pair) is charged the
    uncoupled run already present there (measured on the planned copper, so no
    continuous run through a pad or via exceeds the cap). Reference-plane
    trimming stays the legacy one (PNR_PAIR_REF_TRIM_PER_END below changes it);
    a surface leg is judged with the clearance apertures of the vias already
    planned, as the post-fill check sees the plane. Each leg is re-measured
    (pnr.route.detail.coupled.uncoupled_runs) before acceptance and the whole
    endpoint path of each net is measured at the end: a run above the cap is
    pair_uncoupled_run_limit; results carry uncoupled_run_metrics (max
    continuous run and every run per net). The measured path starts at the
    declared connector origin and nothing on it is exempt: under the joint
    topology the declared contact's own leg to the join via is part of the
    first run (and is charged to the stage-0 bridge head as arriving run); only
    the duplicate contact's leg to the join (never on that path) keeps its own
    authored max_length_mm. A run ends at any coupled stretch (literal reading;
    PNR_PAIR_RUN_MIN_COUPLED_MM restores the earlier merge of short ones).
    pair_bridge_ports adds the exact minimum legal via run
    (bisection between the last illegal and the first legal grid run, oracle.via
    is the judge) to the run grid. Via geometry/clearances still come from rules.
PNR_PAIR_REF_TRIM_PER_END=1   (only with PNR_PAIR_PER_RUN_UNCOUPLED=1)
    Reference-plane trimming follows the per-end uncoupled budgets (surface leg:
    max_uncoupled - arriving run at its start; bridge: each end's own budget)
    instead of the legacy trim (max_uncoupled at both ends of a surface leg, the
    shared bridge budget at both ends of a bridge). Stricter than src12b near a
    leg start that inherits an arriving run; an owner decision, not part of A.
PNR_PAIR_RUN_MIN_COUPLED_MM=<mm>   (only with PNR_PAIR_PER_RUN_UNCOUPLED=1)
    A coupled stretch shorter than this does not end a continuous uncoupled run
    (it is counted into it). Default 0.001 (numerical floor: any coupled copper
    ends a run, the literal reading); 0.35 (= width + gap) reproduces the first
    src13 convention.
PNR_PAIR_RUN_BARREL=1   (only with PNR_PAIR_PER_RUN_UNCOUPLED=1)
    Copper reading of decision 2: a via hop adds the barrel (board thickness) to
    the continuous uncoupled run it lies in (a pair via pair is always farther
    apart than the coupled pitch), in the final measurement and in every per-end
    search budget. Default: barrels add 0 to runs. The consistent readings are
    STUB_PLANAR=1 (barrel 0 in stubs and runs) or RUN_BARREL=1 with the default
    stub (barrel counted in both).
PNR_PAIR_RUN_BREAK_AT_PADS=1   (only with PNR_PAIR_PER_RUN_UNCOUPLED=1)
    Alternative reading of the owner decision: an intermediate terminal pad of
    terminal_chain (the in-line ESD pad) ends a continuous uncoupled run, so the
    breakout arriving at the pad and the one leaving it are budgeted separately
    (as the two ends of a bridge are). Default (unset): the copper through the
    pad is one continuous run, charged to the departing leg.
PNR_PAIR_STUB_MAX_MM=<mm>   (unset = no cap)
    Maximum copper length from the pair trunk (endpoint path) to an intermediate
    terminal pad of terminal_chain that the route leaves as a branch (ESD stub),
    measured on the exact copper graph (path_metrics), via barrels included
    (graph via hops use the board thickness), from the junction on the trunk to
    where the copper enters the pad (the part of the track inside the pad's own
    copper is pad, not stub; stub_metrics also report stub_centre_mm, the length
    to the pad centre). In-line terminals have 0. The
    post-bridge via-start leg (stub = its via-to-pad fanout) and the via-reuse
    bridge (stub = barrel + fanout) are skipped when their stub exceeds the cap;
    when the reuse bridge is skipped, a bridge from the intermediate pad itself
    (D in line) is tried instead. A stage-0 bridge that ends at an intermediate
    terminal prefers target ports whose fanout fits the cap. The final check
    rejects any route above the cap (pair_stub_limit); results carry
    stub_metrics. PNR_PAIR_PREFER_INLINE ranking is unchanged.
PNR_PAIR_STUB_PLANAR=1   (only with PNR_PAIR_STUB_MAX_MM)
    Alternative reading of the stub decision: only planar copper counts, a via
    barrel between a B.Cu trunk and the F.Cu fanout adds 0 (default: the board
    thickness, since that copper is part of the branch).
PNR_BUS_CLASSES=1   (src15; the pair carries a bus class, pnr.si.bus_classes)
    The pair's rules entry (compiled from the @pnr-pair "class") may carry a
    stub_limit. kind 'delay' (class-derived stub_k * t_rise_min, or an explicit
    annotation stub_delay_max_ps): the final check measures every intermediate
    terminal's stub as copper DELAY - per-layer ps/mm from the stackup, the via
    barrel INCLUDED (board thickness x barrel ps/mm, PNR_PAIR_STUB_PLANAR does not
    apply), measured to the pad CENTRE (from the copper's entry vertex, plus the
    straight entry-to-centre distance when the track does not end at the centre);
    stub_metrics add stub_delay_ps and its breakdown; above the limit (or
    unmeasurable) is pair_stub_limit. The pad's own copper layer is used (a
    bottom-side or through-hole terminal is read on B.Cu / either side). The search
    keeps the src13 machinery with an mm cap no stub within the delay limit can
    exceed (limit / fastest layer ps/mm), plus the delay itself as estimated from the
    planar fanout (to the pad entry, at the leg layer's ps/mm, + barrels): target ports
    are the src order interleaved with short-stub-first by it (order_bridge_candidates),
    and via-start / via-reuse legs whose estimated
    stub delay exceeds the limit are skipped (review fix 2026-09-30: with only the
    70 mm cap the src13 short-stub-first order was lost). The estimate never exceeds
    the pad-centre reading, so pruning never rejects a route the delay check accepts.
    kind 'mm' (annotation
    stub_max_mm) behaves exactly as PNR_PAIR_STUB_MAX_MM. PNR_PAIR_STUB_MAX_MM, when
    set, overrides both (explicit mm override).
PNR_PAIR_EARLY_EXIT=1
    pair_plan returns as soon as one configuration yields a route that passed
    every _pair_plan_order check (endpoint graph, skew, duplicate origins and the
    src13 run/stub checks) instead of spending the rest of the joint window and
    the legacy topologies. With PNR_PAIR_PREFER_INLINE=1 a route with no stub
    legs stops it at once; after a stub route (V vias) the remaining joint
    configurations only look for an in-line route with at most V vias (stub
    legs skipped, bridges that would exceed V vias skipped) within a grace
    budget equal to the time the stub route's configuration took, then stop
    (the stub route stays the fallback best). Legacy topologies are not run
    after a route was found.
"""

import argparse
from collections import Counter, defaultdict
import json, math, shutil, subprocess, time, os
from pathlib import Path
from types import SimpleNamespace
from pnr.electrical import net_policy, current_width, terminal_policy, neck_budget
from pnr.plane_intent import size_array
from pnr.route.detail.keyhole import route, length, elbows
from pnr.route.detail.coupled import solve_pair, path_metrics
from pnr.pad_entry import snapshot, witness
from pnr.via_coalesce import partition, preserved, acceptable
from pnr.via_in_pad import smd_keepout_violated, hole_keepouts


def shove_enabled():
    """PNR_SHOVE=1 enables the make-room/scheduling extensions; unset is the default path."""
    return os.environ.get("PNR_SHOVE") == "1"


def post_bridge_surface_enabled():
    """PNR_PAIR_POST_BRIDGE_SURFACE=1: loop-checked surface legs + via-start after a bridge."""
    return os.environ.get("PNR_PAIR_POST_BRIDGE_SURFACE") == "1"


def joint_fair_enabled():
    """PNR_PAIR_JOINT_FAIR=1: first joint config of each bridge hand gets a fair share."""
    return os.environ.get("PNR_PAIR_JOINT_FAIR") == "1"


def fallback_reserve_seconds():
    """PNR_PAIR_FALLBACK_RESERVE_SECONDS: legacy-topology reserve after the joint window (default 30)."""
    value = float(os.environ.get("PNR_PAIR_FALLBACK_RESERVE_SECONDS", "30"))
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid pair fallback reserve seconds")
    return value


def hand_first_order(joint, first):
    """PNR_PAIR_JOINT_HAND_FIRST=+1|-1 (set per trial by paired_bootstrap under
    PNR_PAIR_HAND_SWAP_TRIAL=1): within each (join fraction, timing target) seed
    the named bridge hand runs first. Unset/empty keeps the enumeration order.
    (Implementation shared with the controller: pnr.pair_joint.hand_first_order.)"""
    from pnr.pair_joint import hand_first_order as order

    return order(joint, first)


def per_run_uncoupled_enabled():
    """PNR_PAIR_PER_RUN_UNCOUPLED=1: max_uncoupled_mm per continuous uncoupled run."""
    return os.environ.get("PNR_PAIR_PER_RUN_UNCOUPLED") == "1"


def run_break_at_pads_enabled():
    """PNR_PAIR_RUN_BREAK_AT_PADS=1 (with PNR_PAIR_PER_RUN_UNCOUPLED): an intermediate
    terminal pad (ESD) ends a continuous uncoupled run."""
    return os.environ.get("PNR_PAIR_RUN_BREAK_AT_PADS") == "1"


def ref_trim_per_end_enabled():
    """PNR_PAIR_REF_TRIM_PER_END=1 (with PNR_PAIR_PER_RUN_UNCOUPLED): reference-plane
    trimming follows the per-end uncoupled budgets instead of the legacy trim."""
    return per_run_uncoupled_enabled() and os.environ.get("PNR_PAIR_REF_TRIM_PER_END") == "1"


RUN_MIN_COUPLED_FLOOR = 0.001  # mm: numerical floor, any real coupled copper ends a run


def run_min_coupled_mm():
    """PNR_PAIR_RUN_MIN_COUPLED_MM: shortest coupled stretch that ends a continuous
    uncoupled run (default RUN_MIN_COUPLED_FLOOR, i.e. any coupled copper)."""
    raw = os.environ.get("PNR_PAIR_RUN_MIN_COUPLED_MM")
    if raw is None or raw == "":
        return RUN_MIN_COUPLED_FLOOR
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid PNR_PAIR_RUN_MIN_COUPLED_MM")
    return max(value, RUN_MIN_COUPLED_FLOOR)


def run_barrel_mm(thickness):
    """Via barrel length a continuous uncoupled run includes (PNR_PAIR_RUN_BARREL=1:
    the board thickness; default 0)."""
    return thickness if os.environ.get("PNR_PAIR_RUN_BARREL") == "1" else 0.0


def src13_trace_enabled():
    """Any src13 pair flag set: attempts also record where a timed-out search was."""
    return per_run_uncoupled_enabled() or stub_max_mm() is not None or early_exit_enabled()


def stub_max_mm():
    """PNR_PAIR_STUB_MAX_MM: cap on intermediate-terminal stubs (None = no cap)."""
    raw = os.environ.get("PNR_PAIR_STUB_MAX_MM")
    if raw is None or raw == "":
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid PNR_PAIR_STUB_MAX_MM")
    return value


def stub_barrel_mm(thickness):
    """Via barrel length counted in an intermediate-terminal stub (PNR_PAIR_STUB_MAX_MM).

    Default: the full barrel (board thickness) - copper from a B.Cu trunk up to
    an F.Cu fanout. PNR_PAIR_STUB_PLANAR=1: 0 (only planar copper is measured)."""
    return 0.0 if os.environ.get("PNR_PAIR_STUB_PLANAR") == "1" else thickness


def early_exit_enabled():
    """PNR_PAIR_EARLY_EXIT=1: pair_plan stops at the first fully checked route."""
    return os.environ.get("PNR_PAIR_EARLY_EXIT") == "1"


def prefer_inline_enabled():
    """PNR_PAIR_PREFER_INLINE=1: among equal-via routes prefer fewer intermediate-pad stubs."""
    return os.environ.get("PNR_PAIR_PREFER_INLINE") == "1"


def stub_legs(segments):
    """Chain legs that start at the previous bridge's vias (its pad fanout is left as a stub).

    That is a surface leg started there (PNR_PAIR_POST_BRIDGE_SURFACE) or a layer
    bridge reusing those vias (its source fanout lengths are all zero).
    """
    count = 0
    for index, segment in enumerate(segments):
        if "post_bridge_start" in segment:
            count += 1
        elif (
            index
            and segments[index - 1].get("bridge_target")
            and segment.get("fanout_lengths")
            and not any(segment["fanout_lengths"][0].values())
        ):
            count += 1
    return count


def xy(p):
    return (p.x / 1e6, p.y / 1e6)


def uid(t):
    return t.m_Uuid.AsString()


def vec(p):
    import pcbnew as k

    return k.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6))


class Oracle:
    """Actual native copper shapes, filled zones, rule areas, drills and edges."""

    def __init__(self, b, rules, ignored=(), deadline=math.inf):
        import pcbnew as k

        self.b = b
        self.rules = rules
        self.deadline = deadline
        self.hits = Counter()
        self.via_hits = Counter()
        self.cache = {}
        self.items = [p for f in b.GetFootprints() for p in f.Pads()] + list(b.GetTracks())
        self.layers = list(b.GetEnabledLayers().CuStack())
        self.obstacles = []
        self.buckets = defaultdict(set)
        from pnr.writeback import outline_bounds
        from pnr.fab_profile import geometry

        # Fab numbers from the rules; per-hole-kind values are None (unused) for
        # legacy rules, which keep the original single hole clearance exactly.
        self.geometry = g = geometry(rules)
        npth_gap = 0.201 if g.npth_hole_clearance is None else g.npth_hole_clearance + 0.001
        self.box = outline_bounds(b)
        self.ignored = set(ignored)
        self.pair_reserved = {}
        for t in self.items:
            if uid(t) in self.ignored:
                continue
            gap = net_policy(t.GetNetname(), rules)["clearance_mm"] + 0.001
            for la in self.layers:
                if t.IsOnLayer(la):
                    self.add(
                        t.GetEffectiveShape(la), t.GetBoundingBox(), gap, t.GetNetname(), uid(t), la
                    )
                if t.GetClass() == "PAD" and t.GetAttribute() == k.PAD_ATTRIB_NPTH:
                    self.add(
                        t.GetEffectiveHoleShape(), t.GetBoundingBox(), npth_gap, None, uid(t), la
                    )
                elif (
                    g.pth_hole_clearance is not None
                    and t.GetClass() == "PAD"
                    and t.IsOnLayer(la)
                    and t.GetAttribute() == k.PAD_ATTRIB_PTH
                    and max(t.GetDrillSize().x, t.GetDrillSize().y) > 0
                ):
                    # Plated pad hole to foreign copper (same net may enter its own land):
                    # component PTH 0.35; a footprint's via-class hole (drill < 0.30) 0.20.
                    kind = g.hole_kind(
                        False, True, max(t.GetDrillSize().x, t.GetDrillSize().y) / 1e6
                    )
                    self.add(
                        t.GetEffectiveHoleShape(),
                        t.GetBoundingBox(),
                        g.pad_hole_clearance(kind) + 0.001,
                        t.GetNetname(),
                        uid(t),
                        la,
                    )
            # A via with removed unused pads (5B in-pad) is its bare hole where it has
            # no pad: foreign copper keeps the via hole clearance (0.20) from the wall.
            for la, hole, hole_gap in hole_keepouts(g, t, self.layers):
                self.add(hole, t.GetBoundingBox(), hole_gap, t.GetNetname(), uid(t), la)
        for z in b.Zones():
            for la in self.layers:
                if not z.IsOnLayer(la):
                    continue
                if z.GetIsRuleArea():
                    if z.GetDoNotAllowTracks():
                        self.add(z.Outline(), z.GetBoundingBox(), 0.001, None, uid(z), la)
                else:
                    self.add(
                        z.GetFilledPolysList(la),
                        z.GetBoundingBox(),
                        net_policy(z.GetNetname(), rules)["clearance_mm"] + 0.001,
                        z.GetNetname(),
                        uid(z),
                        la,
                    )
        self.probe = k.PCB_TRACK(b)
        self.drilled = [
            t
            for t in self.items
            if t.GetClass() == "PCB_VIA"
            or t.GetClass() == "PAD"
            and max(t.GetDrillSize().x, t.GetDrillSize().y) > 0
        ]
        # Cache exact physical shapes in 1 mm buckets. Vias query every crossed
        # layer and drill neighborhood; foreign filled planes remain cuttable.
        self.physical = []
        self.physical_buckets = defaultdict(set)
        self.holes = []
        self.hole_gaps = []
        self.hole_buckets = defaultdict(set)
        self.clearance_cap = 0.05
        for item in self.items:
            self.index_physical(item)
        from pnr.reference_guard import ReferenceGuard

        self.reference_guard = ReferenceGuard(b, rules)

    def fork(self, deadline=None):
        """Isolate provisional copper while retaining the caller's obstacles."""
        import copy, pcbnew as k

        clone = copy.copy(self)
        for name in ("items", "obstacles", "physical", "holes", "hole_gaps", "drilled"):
            setattr(clone, name, list(getattr(self, name)))
        for name in ("buckets", "physical_buckets", "hole_buckets"):
            setattr(
                clone,
                name,
                defaultdict(set, {key: set(values) for key, values in getattr(self, name).items()}),
            )
        clone.ignored = set(self.ignored)
        clone.cache = dict(self.cache)
        clone.pair_reserved = dict(self.pair_reserved)
        clone.hits = Counter()
        clone.via_hits = Counter()
        clone.probe = k.PCB_TRACK(self.b)
        if deadline is not None:
            clone.deadline = min(self.deadline, deadline)
        return clone

    def index_physical(self, item):
        import pcbnew as k

        if uid(item) in self.ignored:
            return
        net = item.GetNetname()
        gap = net_policy(net, self.rules)["clearance_mm"] + 0.001
        self.clearance_cap = max(self.clearance_cap, gap)
        # SMD pads carry the pad itself: pnr.via_in_pad judges vias against them.
        smd = item if item.GetClass() == "PAD" and item.GetAttribute() == k.PAD_ATTRIB_SMD else None
        for layer in self.layers:
            if not item.IsOnLayer(layer):
                continue
            shape = item.GetEffectiveShape(layer)
            box = shape.BBox()
            i = len(self.physical)
            self.physical.append((shape, net, gap, smd, uid(item)))
            for x in range(math.floor(box.GetLeft() / 1e6), math.floor(box.GetRight() / 1e6) + 1):
                for y in range(
                    math.floor(box.GetTop() / 1e6), math.floor(box.GetBottom() / 1e6) + 1
                ):
                    self.physical_buckets[layer, x, y].add(i)
        # Removed-pad via: its hole wall keeps the via hole clearance (hole_keepouts).
        for layer, hole, hole_gap in hole_keepouts(self.geometry, item, self.layers):
            box = hole.BBox()
            i = len(self.physical)
            self.clearance_cap = max(self.clearance_cap, hole_gap)
            self.physical.append((hole, net, hole_gap, None, uid(item)))
            for x in range(math.floor(box.GetLeft() / 1e6), math.floor(box.GetRight() / 1e6) + 1):
                for y in range(
                    math.floor(box.GetTop() / 1e6), math.floor(box.GetBottom() / 1e6) + 1
                ):
                    self.physical_buckets[layer, x, y].add(i)
        if (
            item.GetClass() == "PCB_VIA"
            or item.GetClass() == "PAD"
            and max(item.GetDrillSize().x, item.GetDrillSize().y) > 0
        ):
            shape = item.GetEffectiveHoleShape()
            box = shape.BBox()
            i = len(self.holes)
            self.holes.append(shape)
            # Drill gap a new via needs to this hole (profile: via/PTH/NPTH differ;
            # a footprint's via-class pad hole counts as a via).
            via = item.GetClass() == "PCB_VIA"
            kind = self.geometry.hole_kind(
                via,
                None if via else item.GetAttribute() != k.PAD_ATTRIB_NPTH,
                0 if via else max(item.GetDrillSize().x, item.GetDrillSize().y) / 1e6,
            )
            self.hole_gaps.append(self.geometry.via_hole_gap(kind))
            for x in range(math.floor(box.GetLeft() / 1e6), math.floor(box.GetRight() / 1e6) + 1):
                for y in range(
                    math.floor(box.GetTop() / 1e6), math.floor(box.GetBottom() / 1e6) + 1
                ):
                    self.hole_buckets[x, y].add(i)

    def add(self, shape, box, gap, net, identity, la):
        index = len(self.obstacles)
        self.obstacles.append((shape, gap, net, identity))
        for x in range(math.floor(box.GetLeft() / 1e6) - 1, math.floor(box.GetRight() / 1e6) + 2):
            for y in range(
                math.floor(box.GetTop() / 1e6) - 1, math.floor(box.GetBottom() / 1e6) + 2
            ):
                self.buckets[la, x, y].add(index)

    def clear(self, net, la, a, z, width, ignore_nets=(), pair=None):
        if time.monotonic() > self.deadline:
            raise TimeoutError("electrical search time budget")
        pair_key = (pair["p"], pair["n"], pair["gap_mm"]) if pair else None
        key = (net, la, tuple(a), tuple(z), width, tuple(ignore_nets), pair_key)
        if key in self.cache:
            return self.cache[key]
        edge = width / 2 + self.rules.get("fab", {}).get("edge_clearance_mm", 0.2) + 0.001
        if any(
            not (
                self.box.GetLeft() / 1e6 + edge <= p[0] <= self.box.GetRight() / 1e6 - edge
                and self.box.GetTop() / 1e6 + edge <= p[1] <= self.box.GetBottom() / 1e6 - edge
            )
            for p in (a, z)
        ):
            return False
        self.probe.SetLayer(la)
        self.probe.SetStart(vec(a))
        self.probe.SetEnd(vec(z))
        self.probe.SetWidth(round(width * 1e6))
        shape = self.probe.GetEffectiveShape(la)
        r = width / 2 + max(0.201, net_policy(net, self.rules)["clearance_mm"] + 0.001)
        found = set()
        for x in range(math.floor(min(a[0], z[0]) - r), math.floor(max(a[0], z[0]) + r) + 1):
            for y in range(math.floor(min(a[1], z[1]) - r), math.floor(max(a[1], z[1]) + r) + 1):
                found.update(self.buckets[la, x, y])
        for i in found:
            other, gap, n, identity = self.obstacles[i]
            if n == net or n in ignore_nets:
                continue
            if pair and {net, n} == {pair["p"], pair["n"]} and identity in self.pair_reserved:
                from pnr.route.detail.regional import segment_distance

                other_a, other_z, other_width = self.pair_reserved[identity]
                required = (round(width * 1e6) / 1e6 + other_width) / 2 + max(
                    pair["gap_mm"],
                    net_policy(net, self.rules)["clearance_mm"],
                    net_policy(n, self.rules)["clearance_mm"],
                )
                actual = segment_distance(
                    xy(self.probe.GetStart()), xy(self.probe.GetEnd()), other_a, other_z
                )
                if actual + 1e-9 < required:
                    self.hits[identity] += 1
                    self.cache[key] = False
                    return False
                continue
            if other.Collide(
                shape,
                round(
                    max(
                        gap,
                        (
                            net_policy(net, self.rules)["clearance_mm"] + 0.001
                            if n is not None
                            else gap
                        ),
                    )
                    * 1e6
                ),
            ):
                self.hits[identity] += 1
                self.cache[key] = False
                return False
        self.cache[key] = True
        return True

    def reserve_track(self, net, layer, a, z, width):
        """Make already planned pair copper an obstacle for later stages/legs."""
        import pcbnew as k

        track = k.PCB_TRACK(self.b)
        track.SetLayer(layer)
        track.SetStart(vec(a))
        track.SetEnd(vec(z))
        track.SetWidth(round(width * 1e6))
        track.SetNetCode(self.b.FindNet(net).GetNetCode())
        self.items.append(track)
        self.index_physical(track)
        self.pair_reserved[uid(track)] = (
            xy(track.GetStart()),
            xy(track.GetEnd()),
            track.GetWidth() / 1e6,
        )
        self.add(
            track.GetEffectiveShape(layer),
            track.GetBoundingBox(),
            net_policy(net, self.rules)["clearance_mm"] + 0.001,
            net,
            uid(track),
            layer,
        )
        self.cache.clear()

    def reserve_via(self, net, point, diameter=0.6, drill=0.3):
        import pcbnew as k

        via = k.PCB_VIA(self.b)
        via.SetPosition(vec(point))
        via.SetFrontWidth(round(diameter * 1e6))
        via.SetDrill(round(drill * 1e6))
        via.SetViaType(k.VIATYPE_THROUGH)
        via.SetLayerPair(k.F_Cu, k.B_Cu)
        via.SetNetCode(self.b.FindNet(net).GetNetCode())
        self.items.append(via)
        self.drilled.append(via)
        self.index_physical(via)
        for layer in self.layers:
            self.add(
                via.GetEffectiveShape(layer),
                via.GetBoundingBox(),
                net_policy(net, self.rules)["clearance_mm"] + 0.001,
                net,
                uid(via),
                layer,
            )
        self.cache.clear()

    def via(self, net, p, diameter, drill):
        import pcbnew as k

        # Through vias may cut clearance voids in foreign planes, but cannot
        # collide with tracks/pads/keepouts. Plane contact is checked after fill.
        v = k.PCB_VIA(self.b)
        v.SetPosition(vec(p))
        v.SetFrontWidth(round(diameter * 1e6))
        v.SetDrill(round(drill * 1e6))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        if not self.reference_guard.via_clear(net, p, diameter):
            return False
        hole = v.GetEffectiveHoleShape()
        hg = self.geometry.max_via_hole_gap + 0.001
        box = hole.BBox()
        near = set()
        for x in range(
            math.floor(box.GetLeft() / 1e6 - hg), math.floor(box.GetRight() / 1e6 + hg) + 1
        ):
            for y in range(
                math.floor(box.GetTop() / 1e6 - hg), math.floor(box.GetBottom() / 1e6 + hg) + 1
            ):
                near.update(self.hole_buckets[x, y])
        if any(self.holes[i].Collide(hole, round((self.hole_gaps[i] + 0.001) * 1e6)) for i in near):
            return False
        gap = net_policy(net, self.rules)["clearance_mm"] + 0.001
        r = max(gap, self.clearance_cap, 0.05)
        for la in self.layers:
            near = set()
            shape = v.GetEffectiveShape(la)
            box = shape.BBox()
            for x in range(
                math.floor(box.GetLeft() / 1e6 - r), math.floor(box.GetRight() / 1e6 + r) + 1
            ):
                for y in range(
                    math.floor(box.GetTop() / 1e6 - r), math.floor(box.GetBottom() / 1e6 + r) + 1
                ):
                    near.update(self.physical_buckets[la, x, y])
            for i in sorted(near):
                other, other_net, other_gap, smd, identity = self.physical[i]
                # Legacy: no via within 0.05 mm of any SMD pad. Profile: 5A 0.127 from
                # every SMD pad unless a qualified 5B filled in-pad via of its own net.
                if smd is not None and smd_keepout_violated(
                    self.geometry, smd, other, shape, net, p, diameter, drill, la
                ):
                    return False
                if other_net != net and other.Collide(shape, round(max(gap, other_gap) * 1e6)):
                    self.via_hits[identity] += 1
                    return False
        for z in self.b.Zones():
            if (
                z.GetIsRuleArea()
                and z.GetDoNotAllowVias()
                and z.Outline().Collide(v.GetEffectiveShape(k.F_Cu), 1000)
            ):
                return False
        g = self.geometry
        edge = (
            diameter / 2 + 0.201 if g.hole_to_edge is None else g.via_edge_margin(diameter, drill)
        )
        return (
            self.box.GetLeft() / 1e6 + edge <= p[0] <= self.box.GetRight() / 1e6 - edge
            and self.box.GetTop() / 1e6 + edge <= p[1] <= self.box.GetBottom() / 1e6 - edge
        )


def connected_items(b, seed):
    cn = b.GetConnectivity()
    return list(
        {
            uid(t): t
            for t in [seed] + list(cn.GetConnectedItems(seed))
            if t.GetNetCode() == seed.GetNetCode()
            and t.GetClass() in ("PAD", "PCB_TRACK", "PCB_VIA")
        }.values()
    )


def access(items, layer, width, towards=(), point_clear=None):
    """Existing narrow signal fanouts cannot become anchors for a power trunk."""
    out = set()
    for t in items:
        if not t.IsOnLayer(layer):
            continue
        if t.GetClass() == "PAD":
            import pcbnew as k

            if t.GetShape() == k.PAD_SHAPE_CUSTOM:
                polygon = k.SHAPE_POLY_SET()
                t.TransformShapeToPolygon(polygon, layer, 0, 1000, k.ERROR_INSIDE)
                polygon.Inflate(
                    -round(width * 500000) - 2000, k.CORNER_STRATEGY_ROUND_ALL_CORNERS, 1000
                )
                for index in range(polygon.OutlineCount()):
                    outline = polygon.COutline(index)
                    center = outline.BBox().GetCenter()
                    if polygon.Contains(center):
                        out.add(xy(center))
                    out.update(xy(outline.CPoint(j)) for j in range(outline.PointCount()))
            # Full-width copper may land on a smaller pad. The actual proposed
            # width still clears all foreign copper, and pad-entry checks require
            # full land contact rather than a grazing overlap.
            elif min(xy(t.GetSize())) > 0:
                center = xy(t.GetPosition())
                out.add(center)
                sx, sy = xy(t.GetSize())
                contact = min(width, sx, sy)
                if (
                    width > contact + 1e-6
                    and (point_clear is None or not point_clear(center))
                    and t.GetShape()
                    in (
                        k.PAD_SHAPE_RECT,
                        k.PAD_SHAPE_ROUNDRECT,
                        k.PAD_SHAPE_OVAL,
                        k.PAD_SHAPE_CIRCLE,
                    )
                ):
                    # Full-width round caps can cover the complete land contact
                    # disk without their centerline entering the pad. This is
                    # NOT a narrowed neck: width and all current budgets stay.
                    offset = max(sx, sy) / 2 - min(sx, sy) / 2
                    angle = math.radians(-t.GetOrientation().AsDegrees())
                    vx, vy = (offset, 0) if sx >= sy else (0, offset)
                    vx, vy = vx * math.cos(angle) - vy * math.sin(angle), vx * math.sin(
                        angle
                    ) + vy * math.cos(angle)
                    origins = [
                        center,
                        (center[0] + vx, center[1] + vy),
                        (center[0] - vx, center[1] - vy),
                    ]
                    reach = max(0.0, (width - contact) / 2 - 0.002)
                    probe = k.PCB_TRACK(t.GetBoard())
                    probe.SetLayer(layer)
                    probe.SetWidth(round(width * 1e6))
                    for origin in origins:
                        for dx, dy in (
                            (1, 0),
                            (-1, 0),
                            (0, 1),
                            (0, -1),
                            (0.7071067812, 0.7071067812),
                            (0.7071067812, -0.7071067812),
                            (-0.7071067812, 0.7071067812),
                            (-0.7071067812, -0.7071067812),
                        ):
                            pt = (origin[0] + dx * reach, origin[1] + dy * reach)
                            probe.SetStart(vec(pt))
                            probe.SetEnd(vec(pt))
                            if witness(t, probe, width):
                                out.add(pt)
        elif t.GetClass() == "PCB_TRACK" and t.GetWidth() / 1e6 + 1e-6 >= width:
            from pnr.pad_entry import closest

            a, z = xy(t.GetStart()), xy(t.GetEnd())
            out.update([a, z])
            out.update(closest(point, a, z) for point in towards)
        # A lone existing via is not a proven power-array port. Banks generated
        # here are handled as aggregate transitions, never inferred by proximity.
    return sorted(out)


def add_track(b, net, la, a, z, width):
    import pcbnew as k

    if math.dist(a, z) < 1e-8:
        return None
    t = k.PCB_TRACK(b)
    t.SetNetCode(b.FindNet(net).GetNetCode())
    t.SetLayer(la)
    t.SetStart(vec(a))
    t.SetEnd(vec(z))
    t.SetWidth(round(width * 1e6))
    b.Add(t)
    return t


def bank_points(center, count, diameter, drill, hole_clearance):
    pitch = max(diameter + 0.05, drill + hole_clearance + 0.002)
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    return [
        (
            center[0] + (i % cols - (cols - 1) / 2) * pitch,
            center[1] + (i // cols - (rows - 1) / 2) * pitch,
        )
        for i in range(count)
    ]


def bank_clear(oracle, net, center, policy, layers):
    sizing = policy.get("via_array")
    if not sizing:
        return None
    fab = oracle.rules.get("fab", {})
    points = bank_points(
        center,
        sizing["count"],
        sizing["diameter_mm"],
        sizing["drill_mm"],
        fab.get("hole_to_hole_mm", fab.get("hole_clearance_mm", 0.2)),
    )
    if not all(oracle.via(net, p, sizing["diameter_mm"], sizing["drill_mm"]) for p in points):
        return None
    for la, width in layers:
        if not all(oracle.clear(net, la, center, p, width) for p in points):
            return None
    return points


def add_in_pad_vias(b, net, vias, geometry):
    """Filled 5B in-pad through vias ``[(point, diameter, drill)]`` with their unused
    inner pads removed (via_in_pad.style_in_pad_via, 5B "Inner layers")."""
    import pcbnew as k
    from pnr.via_in_pad import style_in_pad_via

    keep = []
    for point, diameter, drill in vias:
        v = k.PCB_VIA(b)
        v.SetNetCode(b.FindNet(net).GetNetCode())
        v.SetPosition(vec(point))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetFrontWidth(round(diameter * 1e6))
        v.SetDrill(round(drill * 1e6))
        style_in_pad_via(geometry, v)
        b.Add(v)
        keep.append(v)
    return keep


def add_bank(b, net, center, points, policy, layers):
    import pcbnew as k

    sizing = policy["via_array"]
    keep = []
    for p in points:
        v = k.PCB_VIA(b)
        v.SetNetCode(b.FindNet(net).GetNetCode())
        v.SetPosition(vec(p))
        v.SetFrontWidth(round(sizing["diameter_mm"] * 1e6))
        v.SetDrill(round(sizing["drill_mm"] * 1e6))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        b.Add(v)
        keep.append(v)
        for la, width in layers:
            keep.append(add_track(b, net, la, center, p, width))
    return keep


def qualified_tree_pads(b, net, layer, policy, rules, anchors, excluded=()):
    """Reuse annotated full-current lands only along a width-qualified surface path.

    Connectivity alone is insufficient: reject thin sense links, grazing copper
    junctions and unproven via capacity. Source-authorized necks retain their full
    loss/drop/length checks. No pad or track is widened or deleted here.
    """
    if not policy.get("current_known"):
        return []
    from pnr.pad_entry import neck_witness
    from pnr.route.detail.regional import segment_distance

    tracks = [
        t
        for t in b.GetTracks()
        if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net and t.GetLayer() == layer
    ]
    anchor_ids = {uid(t) for t in anchors if t.GetLayer() == layer}
    if not anchor_ids:
        return []
    result = []
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() != net or not pad.IsOnLayer(layer) or uid(pad) in excluded:
                continue
            terminal = terminal_policy(fp.GetReference(), [pad.GetNumber()], net, rules)
            if (
                not terminal
                or terminal["rms_current_a"] < policy["rms_current_a"]
                or terminal["peak_current_a"] < policy["peak_current_a"]
            ):
                continue
            width = terminal["outer_width_mm"]
            good = [
                t
                for t in tracks
                if t.GetWidth() / 1e6 + 1e-6 >= width or neck_witness(pad, t, width, tracks, rules)
            ]
            reached = {uid(t) for t in good if witness(pad, t, min(width, t.GetWidth() / 1e6))}
            todo = [t for t in good if uid(t) in reached]
            while todo and not reached & anchor_ids:
                first = todo.pop()
                fw = first.GetWidth() / 1e6
                for other in good:
                    if uid(other) in reached:
                        continue
                    ow = other.GetWidth() / 1e6
                    contact = min(width, fw, ow)
                    distance = segment_distance(
                        xy(first.GetStart()),
                        xy(first.GetEnd()),
                        xy(other.GetStart()),
                        xy(other.GetEnd()),
                    )
                    if distance <= (fw + ow) / 2 - contact + 1e-6:
                        reached.add(uid(other))
                        todo.append(other)
            if reached & anchor_ids:
                result.append(pad)
    return result


def array_roots(b, net, layer, rules, attached, excluded, policies):
    """PNR_SHOVE=1 (A4): array-attached lands on ``layer`` that may root a branch.

    Only a full-current terminal qualifies: an explicit terminal contract whose rms
    and peak currents are at least every policy in ``policies`` (the net envelope
    and the branch), the same capacity rule as :func:`qualified_tree_pads`. Its
    qualified in-pad array and collector already carry the net's whole current,
    so a branch landing there adds no unbudgeted current. A leaf's own array (a
    10 mA sense pin with one small via) never becomes a root for other branches.
    """
    if not all(policy.get("current_known") for policy in policies):
        return []
    rms = max(policy["rms_current_a"] for policy in policies)
    peak = max(policy["peak_current_a"] for policy in policies)
    out = []
    for fp in b.GetFootprints():
        for land in fp.Pads():
            if (
                land.GetNetname() != net
                or uid(land) in excluded
                or uid(land) not in attached
                or not land.IsOnLayer(layer)
            ):
                continue
            try:
                terminal = terminal_policy(fp.GetReference(), [land.GetNumber()], net, rules)
            except ValueError:
                terminal = None
            if (
                not terminal
                or terminal["rms_current_a"] + 1e-12 < rms
                or terminal["peak_current_a"] + 1e-12 < peak
            ):
                continue
            out.append(land)
    return out


def power_plan(
    b,
    net,
    source,
    target,
    rules,
    oracle,
    bounds,
    pitch,
    *,
    prefer_tree=True,
    root_strategy=None,
    _source_only=False,
    _reverse_retry=True,
    _force_in_pad=False
):
    import pcbnew as k

    p = net_policy(net, rules)
    aa = connected_items(b, source)
    zz = connected_items(b, target)
    trunk = dict(p)
    if root_strategy is None:
        root_strategy = "existing" if prefer_tree else "target"
    for seed, group in ((source, aa),) if _source_only else ((source, aa), (target, zz)):
        ps = [t for t in group if t.GetClass() == "PAD"]
        refs = {t.GetParentFootprint().GetReference() for t in ps}
        if len(refs) != 1:
            continue
        leaf = terminal_policy(next(iter(refs)), [t.GetNumber() for t in ps], net, rules)
        if leaf:
            if seed == target:
                source, target = target, source
                aa, zz = zz, aa
            p = leaf
            break
    # Local branch budgets may reduce a lead width, never use an existing thin
    # sense lead as an anchor for a new power trunk.
    source_ids = {uid(item) for item in aa}
    trunk_anchors = [
        t
        for t in b.GetTracks()
        if t.GetNetname() == net and uid(t) not in source_ids and t.GetClass() == "PCB_TRACK"
    ]
    root_landings = {}
    from functools import lru_cache

    access_cache = {}

    def power_access(items, layer, width, towards=()):
        # A blocked round cap cannot seed any legal full-width segment. Remove
        # these endpoints once, before Cartesian elbow/grid searches. Geometry
        # stays immutable throughout this plan; the cache never crosses edits.
        items = list(items)
        key = (tuple(sorted(uid(t) for t in items)), layer, width, tuple(towards))
        if key not in access_cache:
            access_cache[key] = tuple(
                pt
                for pt in access(
                    items,
                    layer,
                    width,
                    towards,
                    point_clear=lambda q: oracle.clear(net, layer, q, q, width),
                )
                if oracle.clear(net, layer, pt, pt, width)
            )
        return list(access_cache[key])

    # Same-net copper is not automatically a safe attachment: a thin branch
    # grazing an otherwise bare power pad creates an unqualified pad entry.
    from pnr.pad_entry import required_width, array_attached_pads

    existing_entries = snapshot(b, rules)
    guarded_pads = [
        (pad, required_width(pad, rules))
        for f in b.GetFootprints()
        for pad in f.Pads()
        if pad.GetNetname() == net and pad.GetAttribute() == k.PAD_ATTRIB_SMD
    ]
    # Terminals already attached by a qualified in-pad array (profile 5B only;
    # always empty for legacy rules): their entry carries their full budget.
    attached_cache = {}

    def array_attached():
        if "ids" not in attached_cache:
            attached_cache["ids"] = (
                array_attached_pads(b, rules) if oracle.geometry.in_pad is not None else set()
            )
        return attached_cache["ids"]

    probe = k.PCB_TRACK(b)

    def entry_clear(net, layer, a, z, width):
        if not oracle.clear(net, layer, a, z, width):
            return False
        probe.SetLayer(layer)
        probe.SetStart(vec(a))
        probe.SetEnd(vec(z))
        probe.SetWidth(round(width * 1e6))
        shape = probe.GetEffectiveShape(layer)
        for pad, required in guarded_pads:
            if (
                required <= width + 1e-6
                or not pad.IsOnLayer(layer)
                or existing_entries.get(uid(pad) + ":" + str(layer))
            ):
                continue
            if not pad.GetEffectiveShape(layer).Collide(shape, 1000):
                continue
            landing_ok = False
            for (la, endpoint), landing in root_landings.items():
                if la != layer or min(math.dist(endpoint, a), math.dist(endpoint, z)) > 1e-6:
                    continue
                full = k.PCB_TRACK(b)
                full.SetLayer(layer)
                full.SetStart(vec(landing[1]))
                full.SetEnd(vec(landing[2]))
                full.SetWidth(round(landing[3] * 1e6))
                if witness(pad, full, required):
                    landing_ok = True
                    break
            if not landing_ok:
                oracle.hits[uid(pad)] += 1
                return False
        return True

    @lru_cache(maxsize=None)
    def root_access(layer):
        w = trunk["outer_width_mm"] if layer in (k.F_Cu, k.B_Cu) else trunk["inner_width_mm"]
        root_items = list(zz)
        if root_strategy == "all" and p.get("current_known") and trunk.get("current_known"):
            for fp in b.GetFootprints():
                for pad in fp.Pads():
                    if pad.GetNetname() != net or uid(pad) in source_ids:
                        continue
                    terminal = terminal_policy(fp.GetReference(), [pad.GetNumber()], net, rules)
                    if terminal and (
                        terminal["rms_current_a"] < p["rms_current_a"]
                        or terminal["peak_current_a"] < p["peak_current_a"]
                    ):
                        continue
                    root_items.append(pad)
        root_items = list({uid(item): item for item in root_items}.values())
        existing = (
            power_access(trunk_anchors, layer, w, towards=[xy(source.GetPosition())])
            if prefer_tree
            else []
        )
        if root_strategy == "existing" and existing:
            qualified = qualified_tree_pads(b, net, layer, trunk, rules, trunk_anchors, source_ids)
            # These lands already lead to the full-current tree. Their explicit
            # terminal budget, not a generic class-width floor, sizes this access.
            for pad in qualified:
                terminal = terminal_policy(
                    pad.GetParentFootprint().GetReference(), [pad.GetNumber()], net, rules
                )
                existing += power_access([pad], layer, terminal["outer_width_mm"])
            if shove_enabled():
                # A4: a full-current land already attached by its qualified in-pad
                # array carries the net's whole envelope; a declared branch may join
                # it at the branch's own width (as the 'target' strategy below
                # already allows), nearest root wins. Leaf arrays never root.
                branch_width = (
                    p["outer_width_mm"] if layer in (k.F_Cu, k.B_Cu) else p["inner_width_mm"]
                )
                for land in array_roots(
                    b, net, layer, rules, array_attached(), source_ids, (trunk, p)
                ):
                    existing += power_access([land], layer, branch_width)
            return sorted(set(existing))
        # Grow a multi-terminal tree toward already full-current copper, rather
        # than forcing a low-current leaf through an unqualified isolated pad.
        branch_width = p["outer_width_mm"] if layer in (k.F_Cu, k.B_Cu) else p["inner_width_mm"]
        if branch_width >= w - 1e-9:
            return existing + power_access(root_items, layer, w, towards=[xy(source.GetPosition())])
        # A low-current branch may join a high-current terminal, but its landing
        # must still satisfy the terminal's full-width entry contract. Do not
        # route a narrow branch directly to a previously unqualified rail pad.
        points = existing + power_access(
            [t for t in root_items if t.GetClass() != "PAD"],
            layer,
            w,
            towards=[xy(source.GetPosition())],
        )
        from pnr.pad_entry import required_width

        for pad in (t for t in root_items if t.GetClass() == "PAD" and t.IsOnLayer(layer)):
            landing_width = max(branch_width, required_width(pad, rules))
            # A terminal attached by its qualified in-pad array (profile 5B) already
            # has its full-budget entry, so a declared branch joins its land at the
            # branch's own width (entry_clear likewise admits a qualified entry).
            if uid(pad) in array_attached():
                landing_width = branch_width
            for center in power_access([pad], layer, landing_width):
                if landing_width <= branch_width + 1e-9:
                    points.append(center)
                    continue
                for distance in (0.25, 0.5, 0.75, 1.0):
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        end = (center[0] + dx * distance, center[1] + dy * distance)
                        if entry_clear(net, layer, center, end, landing_width):
                            points.append(end)
                            root_landings[layer, end] = (layer, center, end, landing_width)
        return points

    layers = [k.F_Cu, k.B_Cu, k.In2_Cu]
    necks = {}
    branch_counts = {}
    # Terminal in-pad array attach (profile 5B only: legacy geometry has no in_pad
    # policy, so nothing below runs). Applies to a lone SMD terminal land whose
    # own layer has no budgeted surface access (full width or source-bounded
    # neck). Its current-sized row of filled 5B vias starts the trunk on another
    # layer; in_pad_ports maps (layer, start point) to that attach.
    g = oracle.geometry
    in_pad_ports = {}
    in_pad_state = {}
    source_pads = [t for t in aa if t.GetClass() == "PAD"]
    terminal_pad = (
        source_pads[0]
        if g.in_pad is not None
        and len(source_pads) == 1
        and p.get("terminal_sources")
        and p.get("current_known")
        and source_pads[0].GetAttribute() == k.PAD_ATTRIB_SMD
        else None
    )
    terminal_layer = None
    if terminal_pad is not None:
        from pnr.via_in_pad import pad_layer

        terminal_layer = pad_layer(terminal_pad)

    @lru_cache(maxsize=None)
    def branch_access(layer):
        w = p["outer_width_mm"] if layer in (k.F_Cu, k.B_Cu) else p["inner_width_mm"]
        points = power_access(aa, layer, w)
        if layer not in (k.F_Cu, k.B_Cu):
            return points + in_pad_access(layer)
        for pad in [t for t in aa if t.GetClass() == "PAD" and t.IsOnLayer(layer)]:
            center = xy(pad.GetPosition())
            minimum = max(min(xy(pad.GetSize())), rules.get("fab", {}).get("track_width_mm", 0.2))
            if minimum >= w:
                continue
            widths = sorted(
                {minimum}
                | {i * 0.05 for i in range(math.ceil(minimum / 0.05), math.ceil(w / 0.05))}
            )
            origins = [center]
            if pad.GetShape() in (k.PAD_SHAPE_RECT, k.PAD_SHAPE_ROUNDRECT, k.PAD_SHAPE_OVAL):
                sx, sy = xy(pad.GetSize())
                offset = max(sx, sy) / 2 - min(sx, sy) / 2
                angle = math.radians(-pad.GetOrientation().AsDegrees())
                vx, vy = (offset, 0) if sx >= sy else (0, offset)
                vx, vy = vx * math.cos(angle) - vy * math.sin(angle), vx * math.sin(
                    angle
                ) + vy * math.cos(angle)
                origins.extend([(center[0] + vx, center[1] + vy), (center[0] - vx, center[1] - vy)])
            for center in origins:
                for distance in (0.15, 0.25, 0.35, 0.4, 0.5):
                    for nw in widths:
                        if nw >= w:
                            continue
                        budget = neck_budget(p, nw, distance, rules["electrical_fab"])
                        if not budget:
                            continue
                        for dx, dy in (
                            (1, 0),
                            (-1, 0),
                            (0, 1),
                            (0, -1),
                            (0.7071067812, 0.7071067812),
                            (0.7071067812, -0.7071067812),
                            (-0.7071067812, 0.7071067812),
                            (-0.7071067812, -0.7071067812),
                        ):
                            end = (center[0] + dx * distance, center[1] + dy * distance)
                            if not oracle.clear(net, layer, center, end, nw) or not entry_clear(
                                net, layer, end, end, w
                            ):
                                continue
                            neck_probe = k.PCB_TRACK(b)
                            neck_probe.SetLayer(layer)
                            neck_probe.SetStart(vec(center))
                            neck_probe.SetEnd(vec(end))
                            neck_probe.SetWidth(round(nw * 1e6))
                            shape = neck_probe.GetEffectiveShape(layer)
                            eligible = True
                            for touched, required in guarded_pads:
                                if (
                                    required <= nw + 1e-6
                                    or not touched.IsOnLayer(layer)
                                    or existing_entries.get(uid(touched) + ":" + str(layer))
                                    or not touched.GetEffectiveShape(layer).Collide(shape, 1000)
                                ):
                                    continue
                                contract = terminal_policy(
                                    touched.GetParentFootprint().GetReference(),
                                    [touched.GetNumber()],
                                    net,
                                    rules,
                                )
                                if (
                                    not contract
                                    or not neck_budget(
                                        contract, nw, distance, rules["electrical_fab"]
                                    )
                                    or not witness(touched, neck_probe, nw)
                                ):
                                    eligible = False
                                    break
                            if eligible:
                                points.append(end)
                                necks[layer, end] = (layer, center, end, nw, budget)
        points = sorted(set(points + in_pad_access(layer)))
        branch_counts[str(layer)] = len(points)
        return points

    def in_pad_access(layer):
        """Trunk start points on ``layer`` of the terminal's in-pad array attach."""
        if terminal_pad is None or layer == terminal_layer:
            return []
        if branch_access(terminal_layer) and not _force_in_pad:
            # A budgeted surface attach exists. PNR_SHOVE=1 records that the in-pad
            # escape was skipped, so a failed plan can retry with it (main()).
            if shove_enabled():
                oracle.__dict__["in_pad_skipped"] = True
            return []
        plan_in_pad()
        return sorted(pt for la, pt in in_pad_ports if la == layer)

    def plan_in_pad():
        """Choose the array once: the engine's barrel-model count for the terminal
        budget (via_in_pad.array_requirement), 5B sites (attach_windows), every via
        through the native Oracle plus same-array hole spacing, and on each other
        power layer the full-width collector along the row whose copper covers every
        via disk (the trunk's first segment; via_in_pad.array_attach re-checks it)."""
        if in_pad_state:
            return
        from pnr.via_in_pad import array_requirement, attach_windows, layer_width, trunk_contact

        need = array_requirement(p, rules, g)
        in_pad_state.update(
            pad=terminal_pad.GetParentFootprint().GetReference() + "." + terminal_pad.GetNumber(),
            root_strategy=root_strategy,
            required=need["count"],
            windows=0,
            via_blocked=0,
            ports=0,
        )
        # Diagnostic trail across the retries of this worker (result.json in_pad_attempts).
        oracle.__dict__.setdefault("in_pad_attempts", []).append(in_pad_state)
        # Barrels this worker already validated and reserved (an earlier root strategy
        # of the same plan): re-checking them would collide with their own reservation.
        reserved = oracle.__dict__.setdefault("in_pad_reserved", set())
        for window in attach_windows(b, terminal_pad, need["count"], g):
            in_pad_state["windows"] += 1
            vias = window["vias"]
            if any(
                math.dist(a[0], z[0]) - (a[2] + z[2]) / 2 < g.hole_gap - 1e-9
                for i, a in enumerate(vias)
                for z in vias[i + 1 :]
            ):
                continue
            if not all(
                (net, pt, d, h) in reserved or oracle.via(net, pt, d, h) for pt, d, h in vias
            ):
                in_pad_state["via_blocked"] += 1
                continue
            diameter = max(d for _, d, _ in vias)
            first, last = vias[0][0], vias[-1][0]
            across = window["across"]
            found = []
            for la in layers:
                if la == terminal_layer:
                    continue
                w = layer_width(p, la)
                reach = max(0.0, (w - diameter) / 2)
                offsets = sorted(
                    {0.0}
                    | {
                        s * round(i * 0.01, 6)
                        for i in range(1, int(reach / 0.01 + 1e-9) + 1)
                        for s in (-1, 1)
                    }
                    | ({-reach, reach} if reach else set()),
                    key=lambda s: (abs(s), s),
                )
                chosen = {}
                for s in offsets:
                    sides = [1, -1] if not s else [1 if s > 0 else -1]
                    if all(side in chosen for side in sides):
                        continue
                    a0 = (round(first[0] + s * across[0], 6), round(first[1] + s * across[1], 6))
                    a1 = (round(last[0] + s * across[0], 6), round(last[1] + s * across[1], 6))
                    if not all(trunk_contact(pt, d, a0, a1, w) for pt, d, _ in vias):
                        continue
                    if not entry_clear(net, la, a0, a1, w):
                        continue
                    for side in sides:
                        chosen.setdefault(side, (s, a0, a1))
                for s, a0, a1 in dict.fromkeys(chosen.values()):
                    attach = dict(
                        layer=la,
                        vias=vias,
                        collector=(la, a0, a1, w),
                        report=dict(
                            pad=terminal_pad.GetParentFootprint().GetReference()
                            + "."
                            + terminal_pad.GetNumber(),
                            pad_uuid=uid(terminal_pad),
                            layer=b.GetLayerName(la),
                            variant=window["variant"],
                            offsets_mm=window["offsets_mm"],
                            neighbour_offsets_mm=window["neighbour_offsets_mm"],
                            collector_offset_mm=s,
                            trunk_width_mm=w,
                            required=need["count"],
                            vias=[
                                dict(position=pt, diameter_mm=d, drill_mm=h) for pt, d, h in vias
                            ],
                            rms_current_a=p["rms_current_a"],
                            peak_current_a=p["peak_current_a"],
                        ),
                    )
                    mid = (round((a0[0] + a1[0]) / 2, 6), round((a0[1] + a1[1]) / 2, 6))
                    for pt in dict.fromkeys((a0, a1, mid)):
                        in_pad_ports.setdefault((la, pt), attach)
                    found.append(la)
            if found:
                # Later banks keep the drill spacing to these barrels (same-net holes).
                for pt, d, h in vias:
                    if (net, pt, d, h) not in reserved:
                        reserved.add((net, pt, d, h))
                        oracle.reserve_via(net, pt, d, h)
                in_pad_state.update(
                    variant=window["variant"],
                    layers=[b.GetLayerName(la) for la in dict.fromkeys(found)],
                    ports=len(in_pad_ports),
                )
                return

    def with_neck(plan):
        plan["root_strategy"] = root_strategy
        # Add the selected terminal escape exactly once, never every candidate.
        endpoints = {(la, tuple(pt)) for la, a, z, w in plan.get("tracks", []) for pt in (a, z)}
        candidates = [v for key, v in necks.items() if key in endpoints]
        roots = [v for key, v in root_landings.items() if key in endpoints]
        if roots:
            landing = min(roots, key=lambda v: math.dist(v[1], v[2]))
            plan["tracks"].append(landing)
            plan["root_landing"] = landing
        if candidates:
            chosen = min(candidates, key=lambda v: v[-1]["length_mm"])
            plan["tracks"].append(chosen[:4])
            plan["neck"] = chosen[-1]
        # The terminal's in-pad array attach, if the trunk starts at its collector.
        centres = {tuple(center) for center, _, _ in plan.get("banks", [])}
        attaches = [v for key, v in in_pad_ports.items() if key in endpoints or key[1] in centres]
        if attaches:
            attach = attaches[0]
            la, a0, a1, w = attach["collector"]
            if math.dist(a0, a1) > 1e-8:
                plan["tracks"].append(attach["collector"])
            plan["in_pad_vias"] = attach["vias"]
            plan["in_pad_array"] = attach["report"]
        return plan

    blocked = []
    for la in layers:
        width = p["outer_width_mm"] if la in (k.F_Cu, k.B_Cu) else p["inner_width_mm"]
        starts = branch_access(la)
        ends = root_access(la)
        if not starts or not ends:
            continue
        r = route(
            starts,
            ends,
            bounds,
            lambda a, z: entry_clear(net, la, a, z, width),
            pitch=pitch,
            max_expansions=12000,
        )
        if r.status == "routed":
            return with_neck(
                dict(
                    status="routed",
                    tracks=[(la, a, z, width) for a, z in zip(r.path, r.path[1:])],
                    banks=[],
                    mode=p["mode"],
                    policy=p,
                )
            )
        blocked.append(r.status)
    # Plane access first reuses the connected ground surface tree above. Only
    # source-budgeted transitions may create vias. Ground net without a net-wide
    # current contract can use an explicit terminal contract for this branch.
    if not p.get("via_array"):
        return dict(
            status="no_surface_channel" if blocked else "no_qualified_power_access",
            mode=p["mode"],
            policy=p,
            detail="no source current/via budget for layer transition",
        )
    if p["mode"] == "plane":
        plane = b.GetLayerID(p["plane"])
        zones = [
            z
            for z in b.Zones()
            if not z.GetIsRuleArea() and z.GetNetname() == net and z.IsOnLayer(plane)
        ]
        for surface in (k.F_Cu, k.B_Cu):
            w = p["outer_width_mm"]
            starts = power_access(aa, surface, w)
            sites = {}
            for a in starts:
                for radius in (0.65, 0.85, 1.1, 1.5, 2.0, 2.5, 3.0):
                    for angle in range(0, 360, 15):
                        center = (
                            a[0] + radius * math.cos(math.radians(angle)),
                            a[1] + radius * math.sin(math.radians(angle)),
                        )
                        if not (
                            bounds[0] <= center[0] <= bounds[2]
                            and bounds[1] <= center[1] <= bounds[3]
                        ):
                            continue
                        if not any(
                            z.GetFilledPolysList(plane).Contains(vec(center)) for z in zones
                        ):
                            continue
                        ls = [(surface, w)]
                        points = bank_clear(oracle, net, center, p, ls)
                        if not points:
                            continue
                        sites[center] = points
                        for path in elbows(a, center):
                            if all(
                                entry_clear(net, surface, x, y, w) for x, y in zip(path, path[1:])
                            ):
                                return dict(
                                    status="routed",
                                    tracks=[(surface, x, y, w) for x, y in zip(path, path[1:])],
                                    banks=[(center, points, ls)],
                                    mode=p["mode"],
                                    policy=p,
                                )
            if starts and sites:
                rr = route(
                    starts,
                    list(sites),
                    bounds,
                    lambda x, y: entry_clear(net, surface, x, y, w),
                    pitch=pitch,
                    max_expansions=12000,
                )
                if rr.status == "routed":
                    center = tuple(rr.path[-1])
                    return dict(
                        status="routed",
                        tracks=[(surface, x, y, w) for x, y in zip(rr.path, rr.path[1:])],
                        banks=[(center, sites[center], [(surface, w)])],
                        mode=p["mode"],
                        policy=p,
                    )
    # Use the layered search with width-specific clearance and aggregate via
    # bank footprints. Both ends may need layer transitions; never substitute a
    # 0.2-mm signal path or single via for a power path.
    from pnr.route.detail.layered import route_layers

    ls = [k.F_Cu, k.B_Cu, k.In2_Cu]
    widths = [p["outer_width_mm"], p["outer_width_mm"], p["inner_width_mm"]]
    starts = set()
    ends = set()
    terminal_map = defaultdict(set)
    for index, la in enumerate(ls):
        for pt in branch_access(la):
            if bounds[0] <= pt[0] <= bounds[2] and bounds[1] <= pt[1] <= bounds[3]:
                starts.add(pt)
                terminal_map[pt].add(index)
        for pt in root_access(la):
            if bounds[0] <= pt[0] <= bounds[2] and bounds[1] <= pt[1] <= bounds[3]:
                ends.add(pt)
                terminal_map[pt].add(index)
    bank_cache = {}
    port_cache = {}

    def bank_site(pt):
        if pt not in bank_cache:
            # Barrel clearance still covers every crossed copper layer. Feed
            # tracks exist only on the two layers used by this transition.
            bank_cache[pt] = bank_clear(oracle, net, pt, p, [])
        return bool(bank_cache[pt])

    def bank_ports(pt, source_layer, target_layer):
        key = (pt, tuple(sorted((source_layer, target_layer))))
        if key not in port_cache:
            port_cache[key] = (
                bank_clear(oracle, net, pt, p, [(ls[i], widths[i]) for i in key[1]])
                if bank_site(pt)
                else None
            )
        return bool(port_cache[key])

    for search_pitch in dict.fromkeys((max(0.3, pitch), pitch)) if starts and ends else ():
        rr = route_layers(
            list(starts),
            list(ends),
            bounds,
            lambda index, a, z: entry_clear(net, ls[index], a, z, widths[index]),
            bank_site,
            pitch=search_pitch,
            layers=3,
            max_expansions=30000,
            max_vias=2,
            terminal_layers=lambda pt: tuple(terminal_map.get(tuple(pt), ())),
            transition_clear=bank_ports,
            deadline=oracle.deadline,
        )
        if rr.status == "routed":
            tracks = []
            banks = {}
            for a, z in zip(rr.path, rr.path[1:]):
                if a[2] == z[2]:
                    tracks.append((ls[a[2]], a[:2], z[:2], widths[a[2]]))
                else:
                    center = tuple(a[:2])
                    ports = [(ls[i], widths[i]) for i in {a[2], z[2]}]
                    banks[center] = (
                        center,
                        port_cache[(center, tuple(sorted((a[2], z[2]))))],
                        ports,
                    )
            return with_neck(
                dict(
                    status="routed",
                    tracks=tracks,
                    banks=list(banks.values()),
                    mode=p["mode"],
                    policy=p,
                )
            )
    # Search compact banks near a qualified source and target. Short leads plus
    # a single bridge layer keep topology simple and auditable.
    outer = p["outer_width_mm"]
    inner = p["inner_width_mm"]
    for source_layer in (k.F_Cu, k.B_Cu):
        starts = power_access(aa, source_layer, outer)
        if not starts:
            continue
        for bridge_layer in (k.B_Cu, k.In2_Cu, k.F_Cu):
            if bridge_layer == source_layer:
                continue
            width = inner if bridge_layer == k.In2_Cu else outer
            ends = root_access(bridge_layer)
            for a in sorted(starts, key=lambda a: math.dist(a, xy(target.GetPosition())))[:8]:
                for radius in (1.0, 1.5, 2.0, 3.0):
                    for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                        center = (a[0] + dx * radius, a[1] + dy * radius)
                        ls = [(source_layer, outer), (bridge_layer, width)]
                        points = bank_clear(oracle, net, center, p, ls)
                        if not points or not entry_clear(net, source_layer, a, center, outer):
                            continue
                        if ends:
                            rr = route(
                                [center],
                                ends,
                                bounds,
                                lambda x, y: entry_clear(net, bridge_layer, x, y, width),
                                pitch=pitch,
                                max_expansions=3000,
                            )
                            if rr.status == "routed":
                                return with_neck(
                                    dict(
                                        status="routed",
                                        tracks=[(source_layer, a, center, outer)]
                                        + [
                                            (bridge_layer, x, y, width)
                                            for x, y in zip(rr.path, rr.path[1:])
                                        ],
                                        banks=[(center, points, ls)],
                                        mode=p["mode"],
                                        policy=p,
                                    )
                                )
    if prefer_tree and time.monotonic() < oracle.deadline:
        if root_strategy == "existing" and trunk.get("current_known"):
            return power_plan(
                b,
                net,
                source,
                target,
                rules,
                oracle,
                bounds,
                pitch,
                prefer_tree=True,
                root_strategy="all",
                _source_only=_source_only,
                _reverse_retry=_reverse_retry,
                _force_in_pad=_force_in_pad,
            )
        if trunk_anchors:
            return power_plan(
                b,
                net,
                source,
                target,
                rules,
                oracle,
                bounds,
                pitch,
                prefer_tree=False,
                _source_only=_source_only,
                _reverse_retry=_reverse_retry,
                _force_in_pad=_force_in_pad,
            )
    if _reverse_retry:
        result = power_plan(
            b,
            net,
            target,
            source,
            rules,
            oracle,
            bounds,
            pitch,
            prefer_tree=True,
            _source_only=True,
            _reverse_retry=False,
            _force_in_pad=_force_in_pad,
        )
        result["reversed_branch_retry"] = True
        # PNR_SHOVE=1: the reversed retry's policy is not the forward attempt's.
        if shove_enabled():
            result.setdefault("forward_policy", p)
        return result
    return dict(
        status="no_current_sized_channel",
        mode=p["mode"],
        policy=p,
        branch_counts=branch_counts,
        neck_count=len(necks),
    )


def bridge_half_plane(a, z, hand):
    if hand not in (-1, 0, 1):
        raise ValueError("bridge hand must be -1,0,+1")
    dx, dy = z[0] - a[0], z[1] - a[1]
    return lambda q: not hand or hand * (dx * (q[1] - a[1]) - dy * (q[0] - a[0])) >= -1e-9


def pair_topologies(pair):
    auxiliary = pair.get("auxiliary_pairs", [])
    candidates = []
    # Only the first declared connector group has interchangeable duplicate
    # contacts. Intermediate protection/load terminals remain in source order.
    if auxiliary and all("source" in g and "target" in g for g in auxiliary):
        for takeoff in (
            "declared",
            "auxiliary_source",
            "declared_p_source_n",
            "source_p_declared_n",
        ):
            for hand in (-1, 1):
                candidates.append(
                    dict(bridge_hand=hand, takeoff=takeoff, auxiliary_order=("p", "n"))
                )
    for order in [("p", "n"), ("n", "p")] if auxiliary else [("p", "n")]:
        candidates.append(dict(bridge_hand=0, takeoff="auxiliary_source", auxiliary_order=order))
    return candidates


def pair_plan(b, pair, rules, oracle, bounds, pitch):
    """Compare bounded connector embeddings and takeoffs, without net swapping."""
    deadline = oracle.deadline
    configs = pair_topologies(pair)
    attempts = []
    hits = Counter()
    via_hits = Counter()
    best = None
    retained = []
    last = {}
    joint_count = 0
    joint_deadline = time.monotonic()
    joint_trial_seconds = 90.0
    if os.environ.get("PNR_PAIR_JOINT_TOPOLOGIES", "0") == "1":
        from pnr.pair_joint import joint_topologies

        joint_trial_seconds = float(os.environ.get("PNR_PAIR_JOINT_TRIAL_SECONDS", "90"))
        if not math.isfinite(joint_trial_seconds) or joint_trial_seconds <= 0:
            raise ValueError("invalid joint topology trial seconds")
        positions = {
            f.GetReference() + "." + pad.GetNumber(): xy(pad.GetPosition())
            for f in b.GetFootprints()
            for pad in f.Pads()
        }
        joint = joint_topologies(
            pair,
            positions,
            max_trials=int(os.environ.get("PNR_PAIR_JOINT_MAX_TRIALS", "6")),
            budget_scope=os.environ.get("PNR_PAIR_AUXILIARY_SCOPE", "separate"),
        )
        joint = hand_first_order(joint, os.environ.get("PNR_PAIR_JOINT_HAND_FIRST"))
        joint_count = len(joint)
        configs = joint + configs
        now = time.monotonic()
        remaining = max(0.0, deadline - now)
        # Keep a bounded fallback reserve. Do not divide one viable 60-second
        # joint search into eighteen subsecond trials merely to exhaust seeds.
        # PNR_PAIR_FALLBACK_RESERVE_SECONDS (default 30) bounds that reserve.
        reserve = fallback_reserve_seconds()
        joint_deadline = now + (
            remaining - min(reserve, remaining / 3)
            if math.isfinite(remaining)
            else joint_count * joint_trial_seconds
        )
    fair = joint_fair_enabled()
    started_hands = set()
    prefer_inline = prefer_inline_enabled()
    early_exit = early_exit_enabled()
    stopped = None
    # PNR_PAIR_EARLY_EXIT + PNR_PAIR_PREFER_INLINE: after a stub route, the rest of
    # the joint configurations only hunt for an in-line route with no more vias,
    # within a grace budget equal to the successful configuration's time.
    hunt = None
    for index, config in enumerate(configs):
        now = time.monotonic()
        if now >= deadline:
            break
        if hunt is not None and (index >= joint_count or now >= hunt["deadline"]):
            stopped = dict(
                config_index=hunt["config_index"],
                configs=len(configs),
                joint_configs=joint_count,
                reason="inline_grace" if index < joint_count else "joint_configs_done",
                inline_configs_tried=hunt["tried"],
                grace_seconds=round(hunt["grace"], 3),
            )
            break
        if index < joint_count:
            if now >= joint_deadline:
                attempts.append(dict(config, status="joint_phase_budget", elapsed_seconds=0.0))
                continue
            trial_deadline = min(deadline, joint_deadline, now + joint_trial_seconds)
            hand = config.get("bridge_hand")
            if fair and hand not in started_hands:
                # PNR_PAIR_JOINT_FAIR: the first config of a hand must leave an
                # equal share of the joint window to every hand not yet started.
                waiting = {c.get("bridge_hand") for c in configs[index:joint_count]} - started_hands
                trial_deadline = min(
                    trial_deadline, now + max(0.0, joint_deadline - now) / max(1, len(waiting))
                )
                started_hands.add(hand)
        else:
            trial_deadline = now + (deadline - now) / (len(configs) - index)
        extra_order = {}
        if hunt is not None:
            trial_deadline = min(trial_deadline, hunt["deadline"])
            hunt["tried"] += 1
            extra_order["limits"] = dict(stub_cap=0.0, max_vias=hunt["max_vias"])
        trial = oracle.fork(trial_deadline)
        retained.append(trial)
        order = config["auxiliary_order"]
        try:
            if config["bridge_hand"]:
                result = _pair_plan_order(
                    b, pair, rules, trial, bounds, pitch, order, config, **extra_order
                )
            else:
                result = _pair_plan_order(
                    b, pair, rules, trial, bounds, pitch, order, **extra_order
                )
        except TimeoutError:
            result = dict(status="time_budget", mode="pair")
            if src13_trace_enabled() and getattr(trial, "progress", None):
                result["progress"] = dict(trial.progress)
        last = result
        hits.update(trial.hits)
        via_hits.update(trial.via_hits)
        attempt = dict(config, status=result["status"], elapsed_seconds=time.monotonic() - now)
        attempts.append(attempt)
        if extra_order:
            attempt["inline_only"] = dict(extra_order["limits"])
        if fair and index < joint_count:
            attempt["budget_seconds"] = trial_deadline - now
        for key in (
            "failed_stage",
            "endpoint_metrics",
            "connector_endpoint_metrics",
            "post_bridge_legs",
            "uncoupled_run_metrics",
            "stub_metrics",
            "stub_skips",
            "progress",
        ):
            if key in result:
                attempt[key] = result[key]
        if src13_trace_enabled():
            # Why the failing stage failed (bridge port counts, solve failure tallies).
            for key in ("source_ports", "target_ports", "failures"):
                if key in result:
                    attempt[key] = result[key]
        if result["status"] == "routed":
            score = (
                len(result.get("pair_vias", [])),
                sum(math.dist(a, z) for _, _, a, z, _ in result.get("pair_tracks", [])),
            )
            if prefer_inline:
                score = (score[0], stub_legs(result.get("segments", [])), score[1])
            attempt["route_score"] = score
            if best is None or score < best[0]:
                best = (score, result, trial)
            # PNR_PAIR_EARLY_EXIT: this route passed every check of the plan;
            # with PNR_PAIR_PREFER_INLINE only a route without stub legs ends
            # the search (the ranking could still prefer a later in-line one).
            if early_exit and (not prefer_inline or best[0][1] == 0):
                stopped = dict(config_index=index, configs=len(configs), joint_configs=joint_count)
                if hunt is not None:
                    stopped.update(
                        reason="inline_route",
                        stub_route_config_index=hunt["config_index"],
                        inline_configs_tried=hunt["tried"],
                    )
                break
            if early_exit and hunt is None:
                # Stub route: an equal-via in-line route would still be preferred.
                grace = max(0.0, time.monotonic() - now)
                hunt = dict(
                    deadline=time.monotonic() + grace,
                    grace=grace,
                    max_vias=best[0][0],
                    config_index=index,
                    tried=0,
                )
    if hunt is not None and stopped is None:
        stopped = dict(
            config_index=hunt["config_index"],
            configs=len(configs),
            joint_configs=joint_count,
            reason="configs_exhausted",
            inline_configs_tried=hunt["tried"],
            grace_seconds=round(hunt["grace"], 3),
        )
    flags = {
        name: True
        for name, on in (
            ("PNR_PAIR_POST_BRIDGE_SURFACE", post_bridge_surface_enabled()),
            ("PNR_PAIR_JOINT_FAIR", fair),
            ("PNR_PAIR_PREFER_INLINE", prefer_inline),
            ("PNR_PAIR_PER_RUN_UNCOUPLED", per_run_uncoupled_enabled()),
            (
                "PNR_PAIR_RUN_BREAK_AT_PADS",
                per_run_uncoupled_enabled() and run_break_at_pads_enabled(),
            ),
            ("PNR_PAIR_EARLY_EXIT", early_exit),
            ("PNR_PAIR_REF_TRIM_PER_END", ref_trim_per_end_enabled()),
            ("PNR_PAIR_RUN_BARREL", per_run_uncoupled_enabled() and run_barrel_mm(1.0) > 0),
        )
        if on
    }
    if per_run_uncoupled_enabled() and os.environ.get("PNR_PAIR_RUN_MIN_COUPLED_MM"):
        flags["PNR_PAIR_RUN_MIN_COUPLED_MM"] = run_min_coupled_mm()
    if stub_max_mm() is not None:
        flags["PNR_PAIR_STUB_MAX_MM"] = stub_max_mm()
    if stub_max_mm() is not None and os.environ.get("PNR_PAIR_STUB_PLANAR") == "1":
        flags["PNR_PAIR_STUB_PLANAR"] = True
    if os.environ.get("PNR_BUS_CLASSES") == "1" and pair.get("bus_class"):
        limit = pair.get("stub_limit") or {}
        flags["PNR_BUS_CLASSES"] = dict(
            bus_class=pair["bus_class"].get("id"),
            stub_limit=(
                None
                if stub_max_mm() is not None or not limit
                else {k: limit[k] for k in ("kind", "max_ps", "max_mm", "source") if k in limit}
            ),
            stub_override_mm=stub_max_mm(),
        )
    if os.environ.get("PNR_PAIR_JOINT_HAND_FIRST"):
        flags["PNR_PAIR_JOINT_HAND_FIRST"] = int(os.environ["PNR_PAIR_JOINT_HAND_FIRST"])
    if "PNR_PAIR_FALLBACK_RESERVE_SECONDS" in os.environ:
        flags["PNR_PAIR_FALLBACK_RESERVE_SECONDS"] = fallback_reserve_seconds()
    extra = dict(pair_engine_flags=flags) if flags else {}
    if stopped:
        extra["early_exit"] = stopped
    if best:
        _, result, trial = best
        oracle.__dict__.update(trial.__dict__)
        oracle.deadline = deadline
        oracle.hits = hits
        oracle.via_hits = via_hits
        return dict(result, order_attempts=attempts, **extra)
    oracle.hits.update(hits)
    oracle.via_hits.update(via_hits)
    return dict(last or dict(status="time_budget", mode="pair"), order_attempts=attempts, **extra)


def move_pair_support(b, pair, spec):
    """Translate an intermediate pair device and its isolated plane-return fanout.

    Connector/receiver positions stay fixed. Existing pair copper is replaced
    atomically by the caller; other-net layer ports or shared returns forbid a
    translation. Constraint legality is checked by the controller before this
    worker, and full native connectivity/DRC after the pair is rebuilt.
    """
    import pcbnew as k
    from pnr.plane_access import surface_group

    intermediate = {
        v.rsplit(".", 1)[0] for t in pair.get("terminal_chain", [])[1:-1] for v in t.values()
    }
    ref = spec["ref"]
    if ref not in intermediate:
        raise ValueError("only source-declared intermediate pair devices may move")
    f = next(f for f in b.GetFootprints() if f.GetReference() == ref)
    if f.IsLocked() or math.dist(xy(f.GetPosition()), spec["original"]) > 1e-6:
        raise ValueError("stale/locked pair placement")
    old_rotation = f.GetOrientationDegrees()
    if abs(old_rotation - spec.get("original_rotation", old_rotation)) > 1e-6:
        raise ValueError("stale pair orientation")
    other = [p for p in f.Pads() if p.GetNetname() not in (pair["p"], pair["n"]) and p.GetNetCode()]
    moved = {}
    for pad in other:
        if pad.GetAttribute() != k.PAD_ATTRIB_SMD:
            raise ValueError("pair support has non-SMD port")
        group = surface_group(b, [pad], pad.GetLayer())
        ids = {uid(t) for t in group}
        if any(
            t.GetClass() == "PAD" and t.GetParentFootprint().GetReference() != ref for t in group
        ):
            raise ValueError("shared surface return")
        for t in group:
            if t.GetClass() == "PAD":
                continue
            if t.IsLocked():
                raise ValueError("locked support copper")
            if t.GetClass() == "PCB_VIA":
                for external in b.GetTracks():
                    if (
                        uid(external) in ids
                        or external.GetClass() == "PCB_VIA"
                        or external.GetNetCode() != t.GetNetCode()
                    ):
                        continue
                    if t.GetEffectiveShape(external.GetLayer()).Collide(
                        external.GetEffectiveShape(external.GetLayer()), 0
                    ):
                        raise ValueError("preserve support via external port")
            moved[uid(t)] = t
    delta = vec(
        (spec["position"][0] - spec["original"][0], spec["position"][1] - spec["original"][1])
    )
    f.SetPosition(vec(spec["position"]))
    rotation = spec.get("rotation", old_rotation)
    f.SetOrientationDegrees(rotation)
    for t in moved.values():
        t.Move(delta)
        t.Rotate(vec(spec["position"]), k.EDA_ANGLE(rotation - old_rotation, k.DEGREES_T))
    b.BuildConnectivity()
    return dict(
        ref=ref,
        original=spec["original"],
        position=spec["position"],
        original_rotation=old_rotation,
        rotation=rotation,
        translated_return_items=sorted(moved),
    )


def load_replay_plan(b, spec):
    """A routed power plan saved by pnr.shove (JSON), in power_plan's own shape.

    Copper is added by the same add_track/add_bank/add_in_pad_vias calls and judged
    by the same checks, DRC and acceptance expression as a searched plan. Only
    power/plane plans; widths, via sizes and policies are taken as saved (the plan
    came from power_plan against the exact current contracts).
    """
    if spec.get("status") != "routed" or spec.get("mode") not in ("power", "plane"):
        raise ValueError("replay plan must be a routed power/plane plan")
    pt = lambda v: (float(v[0]), float(v[1]))
    plan = dict(spec, status="routed", replayed=True)
    plan["tracks"] = [(int(la), pt(x), pt(z), float(w)) for la, x, z, w in spec.get("tracks", [])]
    plan["banks"] = [
        (pt(c), [pt(v) for v in points], [(int(la), float(w)) for la, w in layers])
        for c, points, layers in spec.get("banks", [])
    ]
    if spec.get("in_pad_vias"):
        plan["in_pad_vias"] = [(pt(v), float(d), float(h)) for v, d, h in spec["in_pad_vias"]]
    for key in ("neck", "root_landing"):
        if spec.get(key):
            plan[key] = spec[key]
    if any(w <= 0 for _, _, _, w in plan["tracks"]):
        raise ValueError("invalid replay width")
    return plan


def main():
    import pcbnew as k

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--source-pad", required=True)
    ap.add_argument("--target-pad", required=True)
    ap.add_argument("--net", required=True)
    ap.add_argument("--bounds", nargs=4, type=float, required=True)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--pitch", type=float, default=0.15)
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument("--placement-spec", type=Path)
    ap.add_argument("--placement-candidates", type=Path)
    ap.add_argument("--source-pad-uuid")
    ap.add_argument("--target-pad-uuid")
    ap.add_argument(
        "--replay-plan",
        type=Path,
        help="PNR_SHOVE=1 only: add this pnr.shove plan instead of searching",
    )
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=False)
    from pnr.fab_profile import load_board  # custom rules in force for the refills below

    rules = json.loads(a.rules.read_text())
    b = load_board(a.board)
    b.BuildConnectivity()
    groups = partition(b)
    entries = snapshot(b, rules)
    policy = net_policy(a.net, rules)
    if a.placement_candidates:
        proposals = screen_pair_placements(
            a.board, rules, policy["pair"], json.loads(a.placement_candidates.read_text()), a.bounds
        )
        (a.out_dir / "result.json").write_text(
            json.dumps(dict(status="placement_screen", proposals=proposals), indent=2)
        )
        return
    from pnr.pad_identity import resolve_pad

    source = resolve_pad(b, a.source_pad, a.source_pad_uuid)
    target = resolve_pad(b, a.target_pad, a.target_pad_uuid)
    if any(uid(source) in g and uid(target) in g for g in groups):
        (a.out_dir / "result.json").write_text(
            json.dumps(dict(status="already_connected", accepted=False)) + "\n"
        )
        return
    if source.GetNetname() != a.net or target.GetNetname() != a.net:
        raise ValueError("net/terminal mismatch")
    removed = []
    placement = None
    if policy["mode"] == "pair":
        pair = policy["pair"]
        removed = [t for t in b.GetTracks() if t.GetNetname() in (pair["p"], pair["n"])]
        if any(t.IsLocked() for t in removed):
            raise ValueError("pair copper locked")
        for t in removed:
            b.Delete(t)  # the pair is rerouted; old copper is discarded
        b.BuildConnectivity()
        if a.placement_spec:
            try:
                placement = move_pair_support(b, pair, json.loads(a.placement_spec.read_text()))
            except ValueError as exc:
                (a.out_dir / "result.json").write_text(
                    json.dumps(dict(status="pair_placement_guard", accepted=False, reason=str(exc)))
                    + "\n"
                )
                return
    if policy["mode"] == "pair":
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
    oracle = Oracle(b, rules, deadline=time.monotonic() + a.seconds)
    if shove_enabled() and not a.replay_plan and policy["mode"] != "pair":
        # R1 prevention: keep the in-pad escape (via + short stub on the other
        # layer) of every still-isolated sense leaf of another net free while this
        # route searches; the leaf's own job later uses it (A1).
        from pnr.shove.targets import reserve_sense_escapes

        reserved_escapes = reserve_sense_escapes(b, rules, oracle, a.net)
        oracle.hits.clear()
        oracle.via_hits.clear()  # probing the leaves is not this route's blocker data
    else:
        reserved_escapes = None
    # Profile only (5A/5B via-to-SMD-pad rule; legacy has none and KiCad DRC is its
    # whole rule): vias breaking the rule before this transaction.
    from pnr.via_in_pad import forbidden_vias

    forbidden_before = (
        forbidden_vias(b, oracle.geometry) if oracle.geometry.via_to_smd_pad is not None else None
    )
    replay = None
    if shove_enabled() and a.replay_plan:
        # pnr.shove commit: the plan was searched on the pre-shove board; add exactly
        # that copper here so the worker's own checks/DRC/acceptance decide.
        plan = replay = load_replay_plan(b, json.loads(a.replay_plan.read_text()))
    elif shove_enabled() and policy["mode"] != "pair":
        # PNR_SHOVE=1: the box also covers the copper both ends are connected to now
        # (a trunk routed after the controller sized this job), and 40 % of the
        # budget is kept for one retry with the terminal's in-pad escape forced
        # (surface access points exist but are all trapped).
        from pnr.shove.targets import runtime_bounds

        a.bounds = runtime_bounds(b, groups, source, target, a.net, a.bounds)
        started = time.monotonic()

        def contracted(pad):
            try:
                return pad.GetAttribute() == k.PAD_ATTRIB_SMD and bool(
                    terminal_policy(
                        pad.GetParentFootprint().GetReference(), [pad.GetNumber()], a.net, rules
                    )
                )
            except ValueError:
                return False

        terminal = oracle.geometry.in_pad is not None and any(
            contracted(pad) for pad in (source, target)
        )
        if terminal:
            oracle.deadline = started + 0.6 * a.seconds
        try:
            plan = power_plan(b, a.net, source, target, rules, oracle, a.bounds, a.pitch)
        except TimeoutError:
            plan = dict(status="time_budget", mode=policy["mode"])
        oracle.deadline = started + a.seconds
        if (
            plan["status"] != "routed"
            and oracle.__dict__.pop("in_pad_skipped", False)
            and os.environ.get("PNR_SHOVE_IN_PAD", "1") != "0"
        ):
            first = plan
            try:
                plan = power_plan(
                    b, a.net, source, target, rules, oracle, a.bounds, a.pitch, _force_in_pad=True
                )
            except TimeoutError:
                plan = dict(status="time_budget", mode=policy["mode"])
            plan["forced_in_pad"] = True
            plan["unforced_status"] = first["status"]
            if plan["status"] != "routed" and first.get("forward_policy"):
                plan.setdefault("forward_policy", first["forward_policy"])
    else:
        try:
            plan = (
                pair_plan(b, policy["pair"], rules, oracle, a.bounds, a.pitch)
                if policy["mode"] == "pair"
                else power_plan(b, a.net, source, target, rules, oracle, a.bounds, a.pitch)
            )
        except TimeoutError:
            plan = dict(status="time_budget", mode=policy["mode"])
    if shove_enabled() and replay is None:
        # Same-net self hits (e.g. the target's own land) are not blockers (A5).
        own = {uid(t) for t in b.GetTracks() if t.GetNetname() == a.net} | {
            uid(p) for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == a.net
        }
        for counter in (oracle.hits, oracle.via_hits):
            for identity in list(counter):
                if identity in own:
                    del counter[identity]
    plan.update(
        accepted=False,
        placement=placement,
        static_blockers=dict(oracle.hits.most_common(30)),
        via_blockers=dict(oracle.via_hits.most_common(30)),
    )
    if reserved_escapes:
        plan["reserved_sense_escapes"] = reserved_escapes
    if getattr(oracle, "in_pad_attempts", None):
        plan["in_pad_attempts"] = oracle.in_pad_attempts
    keep = []
    if plan["status"] == "routed":
        for la, x, y, w in plan.get("tracks", []):
            keep.append(add_track(b, a.net, la, x, y, w))
        for center, points, layers in plan.get("banks", []):
            keep.extend(add_bank(b, a.net, center, points, plan["policy"], layers))
        if plan.get("in_pad_vias"):
            keep.extend(add_in_pad_vias(b, a.net, plan["in_pad_vias"], oracle.geometry))
        for net, la, x, y, w in plan.get("pair_tracks", []):
            keep.append(add_track(b, net, la, x, y, w))
        for net, pt in plan.get("pair_vias", []):
            v = k.PCB_VIA(b)
            v.SetNetCode(b.FindNet(net).GetNetCode())
            v.SetPosition(vec(pt))
            v.SetFrontWidth(round(plan["via_diameter_mm"] * 1e6))
            v.SetDrill(round(plan["via_drill_mm"] * 1e6))
            v.SetViaType(k.VIATYPE_THROUGH)
            v.SetLayerPair(k.F_Cu, k.B_Cu)
            b.Add(v)
            keep.append(v)
        b.BuildConnectivity()
        from pnr.pad_entry import repair_changed_entries

        plan.update(repair_changed_entries(b, rules, entries))
        b.BuildConnectivity()
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
        if plan.get("mode") == "pair":
            pair = policy["pair"]
            reference = pair_reference_validator(b, pair, rules)
            reference_checks = [
                reference(segment["reference_paths"], 0) for segment in plan["segments"]
            ]
            plan["postfill_reference_checks"] = reference_checks
            if not all(reference_checks):
                plan["status"] = "pair_postfill_reference_discontinuity"
        current = snapshot(b, rules)
        checks = dict(
            preserved=preserved(groups, partition(b)),
            lost_pad_entries=[i for i, v in entries.items() if v and not current.get(i, False)],
            new_bad_entries=[i for i, v in current.items() if not v and i not in entries],
        )
        checks["reference_failures"] = reference_failures(b, rules)
        if forbidden_before is not None:
            # 5B geometry KiCad cannot see (the DRU exempts the 0.20-drill class).
            checks["new_forbidden_smd_vias"] = sorted(
                forbidden_vias(b, oracle.geometry) - forbidden_before
            )
        if plan.get("in_pad_vias"):
            # The array is the terminal attach only if it qualifies as built: 5B
            # vias in the pad, one full-width trunk on the other layer, capacity.
            from pnr.via_in_pad import array_attach

            terminal = next(
                p
                for f in b.GetFootprints()
                for p in f.Pads()
                if uid(p) == plan["in_pad_array"]["pad_uuid"]
            )
            checks["in_pad_attach"] = array_attach(b, terminal, rules, oracle.geometry)
        output = a.out_dir / "candidate.kicad_pcb"
        k.SaveBoard(str(output), b)
        shutil.copyfile(a.board.with_suffix(".kicad_pro"), output.with_suffix(".kicad_pro"))
        table = a.board.parent / "fp-lib-table"
        if table.exists():
            (a.out_dir / "fp-lib-table").write_text(
                table.read_text().replace("${KIPRJMOD}", str(a.board.parent.resolve()))
            )

        def drc(board, name):
            from pnr.native_drc import run_drc

            return run_drc(a.kicad_cli, board, a.out_dir / name)

        before = drc(a.board, "baseline.drc.json")
        after = drc(output, "candidate.drc.json")
        plan.update(
            checks=checks,
            before_opens=len(before["unconnected_items"]),
            after_opens=len(after["unconnected_items"]),
            accepted=plan["status"] == "routed"
            and not checks["reference_failures"]
            and acceptable(before, after, checks)
            and not checks["new_bad_entries"]
            and len(after["unconnected_items"]) < len(before["unconnected_items"])
            and not checks.get("new_forbidden_smd_vias")
            and (
                not plan.get("in_pad_vias")
                or bool((checks.get("in_pad_attach") or {}).get("qualified"))
            ),
        )
    if not plan["accepted"]:
        diagnostic = a.out_dir / "diagnostic-NOT-ACCEPTED.kicad_pcb"
        k.SaveBoard(str(diagnostic), b)
        shutil.copyfile(a.board.with_suffix(".kicad_pro"), diagnostic.with_suffix(".kicad_pro"))
    (a.out_dir / "result.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in plan.items()
                if k not in ("static_blockers", "tracks", "banks", "pair_tracks")
            }
        )
    )


def pair_via_geometry(rules):
    fab = rules.get("fab", {})
    electrical = rules.get("electrical_fab", {})
    diameter = float(fab.get("via_diameter_mm", electrical.get("via_diameter_mm", 0.6)))
    drill = float(fab.get("via_drill_mm", electrical.get("via_drill_mm", 0.3)))
    if not 0 < drill < diameter:
        raise ValueError("invalid pair via dimensions")
    return diameter, drill


def pair_bridge_ports(pair, terminals, rules, oracle, bounds, index):
    """Exact paired surface fanouts and all-layer legal via sites."""
    import pcbnew as k
    from pnr.route.detail.coupled import geometry_ok
    from pnr.route.detail.regional import segment_distance

    p, n = pair["p"], pair["n"]
    width, gap = pair["width_mm"], pair["gap_mm"]
    diameter, drill = pair_via_geometry(rules)
    clearance = max(net_policy(net, rules)["clearance_mm"] for net in (p, n))
    hole_gap = rules.get("fab", {}).get(
        "hole_to_hole_mm", rules.get("fab", {}).get("hole_clearance_mm", 0.2)
    )
    via_spacing = max(diameter + clearance + 0.002, drill + hole_gap + 0.002)
    cap = pair.get("max_uncoupled_mm", 2)
    a, z = terminals[p][index], terminals[n][index]
    distance = math.dist(a, z)
    if distance < 1e-8:
        return []
    axis = ((a[0] - z[0]) / distance, (a[1] - z[1]) / distance)
    tangent = (-axis[1], axis[0])
    mid = tuple((a[i] + z[i]) / 2 for i in (0, 1))
    found = []

    def sites_at(run, sign, spacing, shift):
        center = tuple(mid[i] + sign * tangent[i] * run + shift * axis[i] for i in (0, 1))
        sites = {
            net: tuple(center[i] + side * axis[i] * spacing / 2 for i in (0, 1))
            for net, side in ((p, 1), (n, -1))
        }
        if any(
            not (bounds[0] <= v[0] <= bounds[2] and bounds[1] <= v[1] <= bounds[3])
            for v in sites.values()
        ):
            return None
        if not all(oracle.via(net, point, diameter, drill) for net, point in sites.items()):
            return None
        return sites

    def port(sites, shift):
        choices = {
            net: [
                path
                for path in elbows(terminals[net][index], sites[net])
                if length(path) < cap - 0.15
                and all(
                    oracle.clear(net, k.F_Cu, x, y, width, pair=pair)
                    for x, y in zip(path, path[1:])
                )
            ]
            for net in (p, n)
        }
        for pp in choices[p]:
            for nn in choices[n]:
                paths = {p: pp, n: nn}
                if not geometry_ok(
                    paths,
                    width,
                    gap,
                    lambda net, x, y, w: oracle.clear(net, k.F_Cu, x, y, w, pair=pair),
                ):
                    continue
                if any(
                    segment_distance(sites[other], sites[other], x, y)
                    < (diameter + width) / 2 + clearance + 0.001
                    for net, other in ((p, n), (n, p))
                    for x, y in zip(paths[net], paths[net][1:])
                ):
                    continue
                return dict(
                    shift_mm=shift,
                    sites=sites,
                    paths=paths,
                    lengths={net: length(path) for net, path in paths.items()},
                )
        return None

    grid = sorted(set((0.8, 1.0, 1.2, 1.5, 1.75)) | {round(i * 0.1, 10) for i in range(4, 18)})
    legal = {}
    for run in grid:
        for sign in (-1, 1):
            for spacing in sorted({via_spacing, max(via_spacing, distance)}):
                for shift in (0, -0.25, 0.25, -0.5, 0.5):
                    sites = sites_at(run, sign, spacing, shift)
                    legal[run, sign, spacing, shift] = sites is not None
                    if sites is None:
                        continue
                    entry = port(sites, shift)
                    if entry:
                        found.append(entry)
    if per_run_uncoupled_enabled():
        # PNR_PAIR_PER_RUN_UNCOUPLED: the 0.1 mm grid overshoots the exact
        # minimum legal run (e.g. 1.2 instead of 1.103 next to a module pad),
        # which the fanout pays out of the uncoupled budget. Bisect between the
        # last illegal and the first legal grid run; oracle.via stays the judge.
        # An exact run within 10 um of the grid run adds nothing but a duplicate.
        for sign in (-1, 1):
            for spacing in sorted({via_spacing, max(via_spacing, distance)}):
                for shift in (0, -0.25, 0.25, -0.5, 0.5):
                    first = next(
                        (i for i, run in enumerate(grid) if legal[run, sign, spacing, shift]), None
                    )
                    if not first:
                        continue
                    lo, hi = grid[first - 1], grid[first]
                    sites = None
                    while hi - lo > 1e-4:
                        middle = (lo + hi) / 2
                        trial = sites_at(middle, sign, spacing, shift)
                        if trial is None:
                            lo = middle
                        else:
                            hi, sites = middle, trial
                    if sites is None or grid[first] - hi < 0.01:
                        continue
                    entry = port(sites, shift)
                    if entry:
                        found.append(dict(entry, run_mm=hi, exact_min_run=True))
    return found


def pair_reference_validator(board, pair, rules, prospective_vias=(), base_center=None):
    """Validate actual tuned trunks against the saved filled reference copper."""
    import pcbnew as k
    from pnr.route.detail.coupled import trim_path

    reference = board.GetLayerID(pair.get("reference_layer", "In1.Cu"))
    nets = {
        n
        for c in rules.get("net_classes", [])
        if c.get("plane_layer") == pair.get("reference_layer", "In1.Cu")
        for n in c["nets"]
    }
    fill = k.SHAPE_POLY_SET()
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.IsOnLayer(reference) and z.GetNetname() in nets:
            fill.BooleanAdd(z.GetFilledPolysList(reference))
    # A newly drilled signal via removes reference copper after the final fill.
    # Include its clearance aperture during route search, not only afterwards.
    zones = [
        z
        for z in board.Zones()
        if not z.GetIsRuleArea() and z.IsOnLayer(reference) and z.GetNetname() in nets
    ]
    diameter, _ = pair_via_geometry(rules)
    clearance = max(
        [float(z.GetLocalClearance()) / 1e6 for z in zones]
        + [rules.get("fab", {}).get("clearance_mm", 0.2)]
    )
    aperture_boxes = []
    for net, point in prospective_vias:
        if net in nets:
            continue
        probe = k.PCB_TRACK(board)
        probe.SetLayer(k.F_Cu)
        probe.SetStart(vec(point))
        probe.SetEnd(vec(point))
        probe.SetWidth(round((diameter + 2 * clearance + 0.004) * 1e6))
        aperture = k.SHAPE_POLY_SET()
        probe.TransformShapeToPolygon(aperture, k.F_Cu, 0, 1000, k.ERROR_OUTSIDE)
        fill.BooleanSubtract(aperture)
        box = aperture.BBox()
        aperture_boxes.append(
            (box.GetLeft() / 1e6, box.GetTop() / 1e6, box.GetRight() / 1e6, box.GetBottom() / 1e6)
        )
    from functools import lru_cache

    @lru_cache(maxsize=100000)
    def center(a, z, width):
        if fill.IsEmpty():
            return False
        # Far from all new apertures, containment equals the unchanged base
        # reference. Use actual native aperture bounds plus outward error guard.
        # Near apertures, retain the exact native subtraction path below.
        margin = width / 2 + 0.002
        if base_center is not None and all(
            max(a[0], z[0]) + margin < x0
            or min(a[0], z[0]) - margin > x1
            or max(a[1], z[1]) + margin < y0
            or min(a[1], z[1]) - margin > y1
            for x0, y0, x1, y1 in aperture_boxes
        ):
            return base_center(a, z, width)
        probe = k.PCB_TRACK(board)
        probe.SetLayer(k.F_Cu)
        probe.SetStart(vec(a))
        probe.SetEnd(vec(z))
        probe.SetWidth(round(width * 1e6))
        poly = k.SHAPE_POLY_SET()
        probe.TransformShapeToPolygon(poly, k.F_Cu, 0, 1000, k.ERROR_OUTSIDE)
        poly.BooleanSubtract(fill)
        return poly.IsEmpty()

    def valid(paths, cap, tail_cap=None):
        # tail_cap (PNR_PAIR_PER_RUN_UNCOUPLED): the target end has its own
        # uncoupled budget; unset trims both ends by cap as before.
        if fill.IsEmpty():
            return False
        for path in paths.values():
            trimmed = trim_path(path, cap, cap if tail_cap is None else tail_cap)
            if any(
                not center(tuple(a), tuple(z), pair["width_mm"] + pair["gap_mm"])
                for a, z in zip(trimmed, trimmed[1:])
            ):
                return False
        return True

    valid.available = not fill.IsEmpty()
    valid.center = center
    return valid


def reference_failures(board, rules):
    """Recheck accepted pair trunk witnesses after any later copper/refill edit."""
    pairs = {p["name"]: p for p in rules.get("diff_pairs", [])}
    failures = []
    for ref in rules.get("routed_pair_references", []):
        pair = pairs[ref["pair"]]
        valid = pair_reference_validator(board, pair, rules)
        for index, segment in enumerate(ref["segments"]):
            if not valid(segment["reference_paths"], 0):
                failures.append(dict(pair=pair["name"], segment=index))
    return failures


def order_bridge_candidates(candidates, target_stub_cap, stub_of, target_stub_delay=None):
    """Order (source port, target port) bridge candidates by the stub the target port would leave.

    mm cap only (PNR_PAIR_STUB_MAX_MM / annotation stub_max_mm, src13): a stable partition,
    ports whose stub exceeds the cap last. Delay limit (PNR_BUS_CLASSES): ``target_stub_delay``
    = (estimate, limit_ps) with estimate(planar mm) -> ps. The ports within the limit (and within
    the mm cap: the search cap, or the in-line hunt's 0) are the src order (shift, then total
    length) INTERLEAVED with the same ports short-stub-first by the estimate (alternate picks,
    duplicates skipped), so the search window (pair_layer_bridge tries the first 64) is shared by
    both preferences and neither crowds the other out; ports over the limit or cap follow in the
    src order. Review fix 2026-09-30: under a delay limit the src13 partition (cap 1.0 mm) became
    a no-op (the 70 mm search cap) and deepS/h3p035 lost their trial-2 route; a pure
    short-stub-first sort restored it but pushed the src-order winners of p007/g1c04/h4p032 from
    positions 5/4/11 to 42/12/49 (2-3x slower, h4p032 to trial 1). Measured at equal load
    (runs/src15-fix/diag2), the interleave routes deepS trial 2 and keeps those three winners at
    their src positions. ``stub_of(port)`` is the port's stub in mm (to where its copper enters
    the pad)."""
    if target_stub_delay is None:
        return sorted(candidates, key=lambda az: stub_of(az[1]) > target_stub_cap + 1e-9)
    estimate, limit_ps = target_stub_delay
    cached = {}

    def stub_key(az):
        if id(az[1]) not in cached:
            mm = stub_of(az[1])
            ps = estimate(mm)
            cached[id(az[1])] = (mm > target_stub_cap + 1e-9 or ps > limit_ps + 1e-9, ps)
        return cached[id(az[1])]

    within = [az for az in candidates if not stub_key(az)[0]]
    over = [az for az in candidates if stub_key(az)[0]]
    short_first = sorted(within, key=lambda az: stub_key(az)[1])
    ordered = []
    seen = set()
    for pair in zip(within, short_first):
        for az in pair:
            if id(az) not in seen:
                seen.add(id(az))
                ordered.append(az)
    return ordered + over


def pair_layer_bridge(
    b,
    pair,
    terminals,
    rules,
    oracle,
    bounds,
    pitch,
    offsets,
    reuse_source=None,
    reference_validator=None,
    prior_reference=(),
    prior_vias=(),
    solution_index=0,
    max_expansions=15000,
    timing_target_mm=0,
    head_prior_mm=0.0,
    target_stub_cap=None,
    target_stub_length=None,
    target_stub_delay=None,
):
    """Matched through-via pairs and a checked B.Cu trunk, no single-leg jump.

    The combined surface and bridge-plane fanout obeys the original planar
    uncoupled budget. Equal barrel lengths enter endpoint timing. Impedance and
    transition qualification remain explicitly pending the actual stackup.

    PNR_PAIR_PER_RUN_UNCOUPLED=1: the head (source fanout + B.Cu lead, plus
    ``head_prior_mm`` = uncoupled run already arriving at the source junction)
    and the tail (B.Cu lead + target fanout) are separate continuous runs, each
    bounded by max_uncoupled_mm; otherwise both B.Cu leads share
    cap - max(source, target fanout). That shared value stays solve_pair's
    max_uncoupled (lane-shape scale) either way. ``target_stub_cap`` (PNR_PAIR_STUB_MAX_MM,
    only when the target is an intermediate terminal) moves target ports whose
    surface fanout exceeds the cap behind the others, since a later via-start
    leg would leave that fanout as the terminal's stub; ``target_stub_length``
    (port -> mm, default its longest fanout) is that stub as the final check
    measures it (to where the copper enters the pad).
    ``target_stub_delay`` (PNR_BUS_CLASSES delay limit: (estimate, limit_ps), estimate =
    planar mm -> ps on the leg layer): the src order interleaved with short-stub-first
    by that estimate, ports over the limit (or over target_stub_cap) last
    (order_bridge_candidates). Under a delay limit target_stub_cap is only the search
    cap (limit / fastest layer), which no port exceeds, so without this order the
    src13 short-stub-first steering was lost (src15 review: deepS/h3p035 lost their
    trial-2 route).
    PNR_PAIR_RUN_BARREL=1: each via hop inside an end's run adds the barrel to it.
    Reference trimming: the legacy shared budget at both ends unless
    PNR_PAIR_REF_TRIM_PER_END=1 (then each end's own budget).
    """
    import pcbnew as k
    from pnr.route.detail.coupled import geometry_ok
    from pnr.route.detail.regional import segment_distance

    p, n = pair["p"], pair["n"]
    width, gap = pair["width_mm"], pair["gap_mm"]
    diameter, drill = pair_via_geometry(rules)
    clearance = max(net_policy(net, rules)["clearance_mm"] for net in (p, n))
    hole_gap = rules.get("fab", {}).get(
        "hole_to_hole_mm", rules.get("fab", {}).get("hole_clearance_mm", 0.2)
    )
    via_spacing = max(diameter + clearance + 0.002, drill + hole_gap + 0.002)
    cap = pair.get("max_uncoupled_mm", 2)
    ports = lambda index: pair_bridge_ports(pair, terminals, rules, oracle, bounds, index)
    starts, ends = ([reuse_source] if reuse_source else ports(0)), ports(1)
    candidates = sorted(
        ((a, z) for a in starts for z in ends),
        key=lambda az: (
            abs(az[0].get("shift_mm", 0)) + abs(az[1].get("shift_mm", 0)),
            sum(az[0]["lengths"].values())
            + sum(az[1]["lengths"].values())
            + sum(math.dist(az[0]["sites"][net], az[1]["sites"][net]) for net in (p, n)),
        ),
    )
    if target_stub_cap is not None:
        stub_of = target_stub_length or (lambda port: max(port["lengths"].values()))
        candidates = order_bridge_candidates(
            candidates, target_stub_cap, stub_of, target_stub_delay
        )
    per_run = per_run_uncoupled_enabled()
    per_end_reference = ref_trim_per_end_enabled()
    # PNR_PAIR_RUN_BARREL: the source via hop (none when the source reuses vias the
    # trunk already arrives at on this layer) and the target via hop join the runs.
    barrel = run_barrel_mm(rules["electrical_fab"]["board_thickness_mm"]) if per_run else 0.0
    head_barrel = lambda a: 0.0 if a.get("reuse") else barrel
    bridge_failures = Counter()
    examples = []
    successful = 0

    def legacy_budget(a, z):
        return cap - max(
            max(a["lengths"].values()) + a.get("entry_budget_mm", 0), max(z["lengths"].values())
        )

    def end_budgets(a, z):
        return cap - (
            max(a["lengths"].values())
            + a.get("entry_budget_mm", 0)
            + head_prior_mm
            + head_barrel(a)
        ), cap - (max(z["lengths"].values()) + barrel)

    def jobs():
        # Legacy: every candidate once with the shared budget. PNR_PAIR_PER_RUN_UNCOUPLED:
        # pass 1 = exactly the legacy search (grid ports, shared budget at both
        # ends, legacy effort); pass 2 (only if pass 1 found nothing) = all
        # candidates, exact-run ports included, with the full per-end budgets.
        # The search budgets only bound fanout/lead GEOMETRY (an upper bound of the
        # uncoupled length); every route is accepted only if the exact measurement
        # of its continuous runs (leg_uncoupled_ok, with the arriving run) passes,
        # so A never narrows the legacy search (src13 review fix: pass 1 used
        # min(shared, per-end), which excluded compliant routes whose lead-in is
        # partly coupled). Larger fanout budgets widen (and slow) solve_pair's
        # fanout search, so they are spent only where the shared budget cannot route.
        if not per_run:
            for a, z in candidates[:64]:
                yield a, z, None
            return
        done = set()
        for a, z in [
            (a, z)
            for a, z in candidates
            if not a.get("exact_min_run") and not z.get("exact_min_run")
        ][:64]:
            shared = legacy_budget(a, z)
            if shared <= 0:
                continue
            done.add((id(a), id(z), round(shared, 9), round(shared, 9)))
            yield a, z, (shared, shared, 1)
        for a, z in candidates[:64]:
            head_rem, tail_rem = end_budgets(a, z)
            if (id(a), id(z), round(head_rem, 9), round(tail_rem, 9)) not in done:
                yield a, z, (head_rem, tail_rem, 2)

    trace = src13_trace_enabled()  # count the silent candidate skips too (diagnostics only)
    budget_skipped = set()
    for a, z, budget in jobs():
        vias = [(net, group["sites"][net]) for group in (a, z) for net in (p, n)]
        if any(
            math.dist(v, q) + 1e-6 < (via_spacing if net != other else drill + hole_gap + 0.002)
            for i, (net, v) in enumerate(vias)
            for other, q in vias[i + 1 :]
        ):
            if trace:
                bridge_failures["via_pair_spacing"] += 1
            continue

        def clear(net, x, y, w):
            return oracle.clear(net, k.B_Cu, x, y, w, pair=pair) and all(
                other == net
                or segment_distance(x, y, pt, pt) >= (diameter + w) / 2 + clearance + 0.001
                for other, pt in vias
            )

        candidate_reference = (
            pair_reference_validator(
                b, pair, rules, list(prior_vias) + vias, base_center=reference_validator.center
            )
            if reference_validator
            else None
        )
        if candidate_reference and any(
            not candidate_reference(paths, 0) for paths in prior_reference
        ):
            if trace:
                bridge_failures["prior_reference_aperture"] += 1
            continue
        if per_run:
            head_prior = (
                max(a["lengths"].values())
                + a.get("entry_budget_mm", 0)
                + head_prior_mm
                + head_barrel(a)
            )
            tail_after = max(z["lengths"].values()) + barrel
            search_head, search_tail, budget_pass = budget
            # Each end's own run budget (exact arriving run, via port fanouts are
            # never coupled): nothing left means no compliant bridge here.
            head_rem, tail_rem = end_budgets(a, z)
            if head_rem <= 1e-6 or tail_rem <= 1e-6:
                if (id(a), id(z)) not in budget_skipped:
                    bridge_failures["uncoupled_budget"] += 1
                    budget_skipped.add((id(a), id(z)))
                continue
            # solve_pair's lane-shape scale stays the legacy shared value (its
            # straight leads grow with it); the search budgets bound the fanouts.
            remaining = legacy_budget(a, z)
            if remaining <= 0:
                remaining = min(head_rem, tail_rem)
            # Reference trim: the legacy shared value at both ends (as src12b)
            # unless PNR_PAIR_REF_TRIM_PER_END.
            trim = (head_rem, tail_rem) if per_end_reference else (remaining, remaining)
        else:
            remaining = cap - max(
                max(a["lengths"].values()) + a.get("entry_budget_mm", 0), max(z["lengths"].values())
            )
            if remaining <= 0:
                continue
        totals = {
            net: offsets.get(net, 0)
            + a["lengths"][net]
            + z["lengths"][net]
            + (1 if a.get("reuse") else 2) * rules["electrical_fab"]["board_thickness_mm"]
            for net in (p, n)
        }
        tuning_offsets = dict(totals)
        tuning_offsets[n] += timing_target_mm
        if per_run:

            def accept(
                paths,
                trim=trim,
                head_prior=head_prior,
                tail_after=tail_after,
                reference=candidate_reference,
            ):
                return (reference is None or reference(paths, *trim)) and leg_uncoupled_ok(
                    pair, paths, head_prior, tail_after
                )

            rr = solve_pair(
                p,
                n,
                {net: (a["sites"][net], z["sites"][net]) for net in (p, n)},
                bounds,
                clear,
                lambda x, y, w: oracle.clear(p, k.B_Cu, x, y, w, ignore_nets=(p, n))
                and (
                    candidate_reference is None
                    or candidate_reference.center(tuple(x), tuple(y), w + gap)
                ),
                width,
                gap,
                pair["skew_mm"],
                pitch=pitch,
                max_expansions=max_expansions,
                max_uncoupled=remaining,
                max_uncoupled_head=search_head,
                max_uncoupled_tail=search_tail,
                max_tuning_length=cap,
                offsets=tuning_offsets,
                accept_paths=accept,
            )
        else:
            rr = solve_pair(
                p,
                n,
                {net: (a["sites"][net], z["sites"][net]) for net in (p, n)},
                bounds,
                clear,
                lambda x, y, w: oracle.clear(p, k.B_Cu, x, y, w, ignore_nets=(p, n))
                and (
                    candidate_reference is None
                    or candidate_reference.center(tuple(x), tuple(y), w + gap)
                ),
                width,
                gap,
                pair["skew_mm"],
                pitch=pitch,
                max_expansions=max_expansions,
                max_uncoupled=remaining,
                max_tuning_length=cap,
                offsets=tuning_offsets,
                accept_paths=(
                    (lambda paths: candidate_reference(paths, remaining))
                    if candidate_reference
                    else None
                ),
            )
        if rr["status"] != "routed":
            bridge_failures.update(rr.get("failures", {}))
            if len(examples) < 8:
                examples.append(dict(source=a, target=z, remaining=remaining, result=rr))
            continue
        if successful < solution_index:
            successful += 1
            continue
        rr["lengths"][n] -= timing_target_mm
        rr["intermediate_timing_target_mm"] = timing_target_mm
        tracks = []
        for net in (p, n):
            for group in (a, z):
                tracks.extend(
                    (net, k.F_Cu, x, y, width)
                    for x, y in zip(group["paths"][net], group["paths"][net][1:])
                )
            tracks.extend(
                (net, k.B_Cu, x, y, width) for x, y in zip(rr["paths"][net], rr["paths"][net][1:])
            )
        from pnr.route.detail.coupled import trim_path

        if per_run:
            rr["reference_paths"] = {
                net: trim_path(path, *trim) for net, path in rr["paths"].items()
            }
            rr["uncoupled_budget_mm"] = dict(
                head=head_rem,
                tail=tail_rem,
                head_prior=head_prior,
                tail_after=tail_after,
                arriving=head_prior_mm,
                search_pass=budget_pass,
                search=[search_head, search_tail],
                reference_trim=list(trim),
            )
        else:
            rr["reference_paths"] = {
                net: trim_path(path, remaining, remaining) for net, path in rr["paths"].items()
            }
        rr.update(
            pair_tracks=tracks,
            pair_vias=[
                (net, group["sites"][net])
                for group in (a, z)
                if not group.get("reuse")
                for net in (p, n)
            ],
            bridge_target=z,
            selected_port_rank=successful,
            layer="B.Cu",
            via_diameter_mm=diameter,
            via_drill_mm=drill,
            fanout_lengths=[a["lengths"], z["lengths"]],
        )
        return rr
    return dict(
        status="pair_no_matched_layer_bridge",
        source_ports=len(starts),
        target_ports=len(ends),
        attempts=min(len(candidates), 64),
        failures=dict(bridge_failures),
        examples=examples,
    )


def leg_uncoupled_ok(pair, paths, head_prior, tail_after):
    """PNR_PAIR_PER_RUN_UNCOUPLED: re-measure one leg's continuous uncoupled runs.

    paths: net -> centerline of this leg (one layer). The run leaving the leg's
    source continues the head_prior mm already uncoupled there (arriving run and/or
    source fanout); the run entering its target continues into tail_after mm
    (target fanout). Every continuous run must stay within max_uncoupled_mm.
    """
    from pnr.route.detail.coupled import uncoupled_runs

    cap = pair.get("max_uncoupled_mm", 2)
    min_coupled = run_min_coupled_mm()
    measured = uncoupled_runs(
        {net: [(0, a, z) for a, z in zip(path, path[1:])] for net, path in paths.items()},
        pair["width_mm"],
        pair["gap_mm"],
        min_coupled=min_coupled,
    )
    for net, metric in measured.items():
        runs = metric["runs"]
        for index, run in enumerate(runs):
            total = run["length_mm"]
            if index == 0 and metric["leading_mm"] > 0:
                total += head_prior
            if index == len(runs) - 1 and metric["trailing_mm"] > 0:
                total += tail_after
            if total > cap + 1e-6:
                return False
        # No uncoupled stretch at all: the arriving and the departing runs only
        # join through a leg shorter than the coupled length that separates runs.
        span = sum(math.dist(a, z) for a, z in zip(paths[net], paths[net][1:]))
        if not runs and span < min_coupled and head_prior + span + tail_after > cap + 1e-6:
            return False
    return True


def route_uncoupled_runs(pair, tracks, vias, origins, ends, thickness, exempt=(), breaks=()):
    """Continuous uncoupled runs along each net's exact endpoint path (src13).

    tracks/vias as in _pair_plan_order; origins: net -> xy (F.Cu); ends: net ->
    (xy, layer). exempt: tracks judged by their own limit (separately budgeted
    connector branch); they end a run and are not counted. breaks: F.Cu points
    (intermediate terminal pads, PNR_PAIR_RUN_BREAK_AT_PADS) where a run ends.
    None if either endpoint graph is not a valid tree.
    """
    import pcbnew as k
    from pnr.route.detail.coupled import path_steps, uncoupled_runs
    from pnr.track_graph import on_segment

    heights = {k.F_Cu: 0, k.B_Cu: thickness}
    steps = {}
    for net in (pair["p"], pair["n"]):
        metric = path_metrics(
            [(la, a, z) for nn, la, a, z, w in tracks if nn == net],
            [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
            (tuple(origins[net]), k.F_Cu),
            (tuple(ends[net][0]), ends[net][1]),
            layer_heights=heights,
        )
        if not metric.get("valid"):
            return None
        steps[net] = path_steps(metric["path"])
    nm = lambda q: tuple(round(v * 1e6) for v in q)
    segments = [(la, nm(a), nm(z)) for nn, la, a, z, w in exempt]

    def exempted(la, a, b):
        a, b = nm(a), nm(b)
        return any(
            la == el and on_segment(a, ea, eb) and on_segment(b, ea, eb) for el, ea, eb in segments
        )

    stops = {(k.F_Cu, *nm(q)) for q in breaks}
    return uncoupled_runs(
        steps,
        pair["width_mm"],
        pair["gap_mm"],
        exempt=exempted if segments else None,
        breaks=(lambda la, q: (la, *nm(q)) in stops) if stops else None,
        min_coupled=run_min_coupled_mm(),
        barrel=run_barrel_mm(thickness),
    )


def run_report(runs):
    """JSON form of route_uncoupled_runs: per net max and every run (mm, layer ids)."""
    if runs is None:
        return None

    def point(q):
        return None if q is None else [q[0], round(q[1], 6), round(q[2], 6)]

    return {
        net: dict(
            max_mm=round(m["max_mm"], 6),
            leading_mm=round(m["leading_mm"], 6),
            trailing_mm=round(m["trailing_mm"], 6),
            runs=[
                dict(
                    length_mm=round(r["length_mm"], 6), start=point(r["start"]), end=point(r["end"])
                )
                for r in m["runs"]
            ],
        )
        for net, m in runs.items()
    }


def pair_stub_metrics(pair, bylabel, tracks, vias, thickness, barrel=None, delay=None):
    """Copper length from the trunk to each intermediate terminal pad (src13).

    For every terminal_chain node between the first and the last: per net,
    on_path (in line) and stub_mm = exact graph length from the endpoint path
    (declared origin -> receiver) to the pad, a via barrel counting ``barrel``
    mm (default: the board thickness; 0 under PNR_PAIR_STUB_PLANAR=1). The
    endpoint path itself is always found with the real barrel lengths.
    """
    import pcbnew as k
    from pnr.route.detail.coupled import branch_lengths

    chain = pair["terminal_chain"]
    result = {}
    for node in chain[1:-1]:
        values = {}
        found_by_net = {}
        for net, key in ((pair["p"], "p"), (pair["n"], "n")):
            origin, end, pad = (
                xy(bylabel[label].GetPosition())
                for label in (chain[0][key], chain[-1][key], node[key])
            )
            net_tracks = [(la, a, z) for nn, la, a, z, w in tracks if nn == net]
            # The pad's own copper layers among those pair copper uses (F.Cu / B.Cu; a
            # through-hole pad is on both). src15 review fix: the reading assumed F.Cu, so a
            # bottom-side terminal was mismeasured or unmeasurable. F.Cu-only pads: unchanged.
            pad_obj = bylabel[node[key]]
            pad_layers = [la for la in (k.F_Cu, k.B_Cu) if pad_obj.IsOnLayer(la)]
            if not pad_layers:
                values[net] = dict(valid=False, reason="terminal pad on no outer copper layer")
                continue
            # Start at the pad centre and at every track vertex inside the pad's
            # copper: the stub is the shortest copper from where it leaves the pad.
            starts = [(pad, la) for la in pad_layers] + [
                (q, la)
                for la in pad_layers
                for q in sorted(
                    {
                        q
                        for l2, a, z in net_tracks
                        if l2 == la
                        for q in (tuple(a), tuple(z))
                        if q != tuple(pad) and pad_contains(pad_obj, q, la)
                    }
                )
            ]
            found = branch_lengths(
                net_tracks,
                [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
                (origin, k.F_Cu),
                (end, k.F_Cu),
                starts,
                layer_heights={k.F_Cu: 0, k.B_Cu: thickness},
                barrel=barrel,
            )
            if delay is not None:
                found_by_net[net] = (starts, found)
            if found is None:
                values[net] = dict(valid=False)
                continue
            centres = found[: len(pad_layers)]
            f = min(
                (g for g in centres if g["stub_mm"] is not None),
                key=lambda g: g["stub_mm"],
                default=centres[0],
            )
            if f["stub_mm"] is None:
                values[net] = dict(on_path=f["on_path"], stub_mm=None)
                continue
            # The stub ends where its copper enters the pad (decision 1: "to the
            # D2 pad"); the track inside the pad's own copper is pad, not stub.
            if any(g["on_path"] for g in found):
                stub = 0.0
            else:
                stub = min(
                    g["stub_mm"]
                    - pad_copper_length(pad_obj, None, s[1], leading=g.get("path") or [])
                    for s, g in zip(starts, found)
                    if g["stub_mm"] is not None
                )
            values[net] = dict(
                on_path=f["on_path"] or stub == 0.0,
                stub_mm=round(max(0.0, stub), 6),
                stub_centre_mm=round(f["stub_mm"], 6),
            )
            if pad_layers != [k.F_Cu]:
                values[net]["pad_layers"] = [
                    {k.F_Cu: "F.Cu", k.B_Cu: "B.Cu"}[la] for la in pad_layers
                ]
        if delay is not None:
            # PNR_BUS_CLASSES delay limit: the same branches, read as copper delay to
            # the pad centre with the via barrel included.
            for net, key in ((pair["p"], "p"), (pair["n"], "n")):
                values.setdefault(net, {}).update(
                    stub_delay_metrics(
                        pair, bylabel[node[key]], found_by_net.get(net), delay, thickness
                    )
                )
        result[node["p"] + "/" + node["n"]] = values
    return result


def stub_delay_metrics(pair, pad, found, delay, thickness):
    """stub_delay_ps (pad-centre reading, barrel included) of one terminal pad.

    ``found``: (starts, branch_lengths results) of pair_stub_metrics for the pad; starts
    are (xy, layer) on the pad's own copper layers, and each measured start is charged its
    straight distance to the pad centre on that layer (0 for the centre itself). In line
    (any start on the endpoint path): 0 ps."""
    import pcbnew as k
    from pnr.si.bus_classes import path_delay

    if not found or found[1] is None:
        return dict(stub_delay_ps=None, stub_delay_limit_ps=delay["max_ps"])
    starts, results = found
    names = {k.F_Cu: "F.Cu", k.B_Cu: "B.Cu"}
    centre = xy(pad.GetPosition())
    barrel_ps = float(delay["barrel_mm"]) * float(delay["barrel_ps_per_mm"])
    best = None
    if any(g["on_path"] for g in results):
        return dict(
            stub_delay_ps=0.0,
            stub_delay_limit_ps=delay["max_ps"],
            stub_delay_reading=delay.get("reading"),
        )
    for (q, start_layer), g in zip(starts, results):
        if g["stub_mm"] is None or not g.get("path"):
            continue
        ps, parts = path_delay(
            g["path"],
            lambda la: names.get(la, str(la)),
            delay["td_ps_per_mm"],
            barrel_ps,
            lead=(start_layer, math.dist(q, centre)),
        )
        if best is None or ps < best[0]:
            best = (ps, parts)
    if best is None:
        return dict(stub_delay_ps=None, stub_delay_limit_ps=delay["max_ps"])
    return dict(
        stub_delay_ps=round(best[0], 3),
        stub_delay_limit_ps=delay["max_ps"],
        stub_delay_path=best[1],
        stub_delay_reading=delay.get("reading"),
    )


def stub_over_delay(metrics, limit_ps):
    return [
        name
        for name, values in metrics.items()
        for v in values.values()
        if v.get("stub_delay_ps") is None or v["stub_delay_ps"] > limit_ps + 1e-6
    ]


def pad_contains(pad, point, layer):
    """True if ``point`` (mm) lies in the pad's copper on ``layer``."""
    try:
        return bool(pad.GetEffectiveShape(layer).Collide(vec(point), 0))
    except Exception:
        return bool(pad.HitTest(vec(point)))


def pad_copper_length(pad, points, layer, leading=None):
    """Length of the polyline ``points`` (mm, starting inside the pad) that runs
    inside the pad's copper on ``layer`` before it first leaves it (0 if it does
    not start inside). ``leading``: the same path as graph nodes (layer, x_nm,
    y_nm); only its leading run on ``layer`` is used (a via hop ends the walk)."""
    if leading is not None:
        points = []
        for la, x, y in leading:
            if la != layer:
                break
            points.append((x / 1e6, y / 1e6))
    total = 0.0
    for a, z in zip(points, points[1:]):
        span = math.dist(a, z)
        if span < 1e-12:
            continue
        if not pad_contains(pad, a, layer):
            break
        if pad_contains(pad, z, layer):
            total += span
            continue
        lo, hi = 0.0, 1.0
        for _ in range(40):
            middle = (lo + hi) / 2
            if pad_contains(
                pad, (a[0] + middle * (z[0] - a[0]), a[1] + middle * (z[1] - a[1])), layer
            ):
                lo = middle
            else:
                hi = middle
        total += lo * span
        break
    return total


def port_stub_mm(port, pads, layer):
    """Stub a bridge port's surface fanout would leave (to where it enters the pad)."""
    return max(
        port["lengths"][net]
        - (
            pad_copper_length(pads[net], port["paths"][net], layer)
            if port["paths"].get(net)
            else 0.0
        )
        for net in pads
    )


def stub_over_cap(metrics, cap):
    return [
        name
        for name, values in metrics.items()
        for v in values.values()
        if v.get("stub_mm") is None or v["stub_mm"] > cap + 1e-6
    ]


def duplicate_endpoint_metrics(pair, bylabel, tracks, vias, thickness):
    """Measure every declared connector contact, including actual branch joins."""
    import pcbnew as k

    origins = [pair["terminal_chain"][0]]
    origins.extend(group["source"] for group in pair.get("auxiliary_pairs", []))
    origins.extend(group["target"] for group in pair.get("auxiliary_pairs", []))
    result = {}
    for origin in origins:
        name = origin["p"] + "/" + origin["n"]
        if name in result:
            continue
        values = {}
        for net, key in ((pair["p"], "p"), (pair["n"], "n")):
            start = bylabel[origin[key]]
            end = bylabel[pair["terminal_chain"][-1][key]]
            values[net] = path_metrics(
                [(la, a, z) for nn, la, a, z, w in tracks if nn == net],
                [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
                (xy(start.GetPosition()), k.F_Cu),
                (xy(end.GetPosition()), k.F_Cu),
                layer_heights={k.F_Cu: 0, k.B_Cu: thickness},
            )
        result[name] = values
    return result


def connector_origins_qualified(pair, metrics):
    return bool(metrics) and all(
        all(m.get("valid", False) for m in values.values())
        and abs(values[pair["p"]]["length_mm"] - values[pair["n"]]["length_mm"])
        <= pair["skew_mm"] + 1e-6
        for values in metrics.values()
    )


def surface_leg_graph_failure(pair, tracks, vias, paths, layer, origins, ends, thickness):
    """None if planned pair copper plus one surface leg is a valid endpoint tree.

    Same exact graph as the final endpoint check (pnr.route.detail.coupled.
    path_metrics): tracks are (net,layer,a,z,width), vias (net,point), paths the
    leg centerlines per net on `layer`; origins/ends are the declared chain origin
    and this leg's end per net. Otherwise the first failing metric plus its net.
    """
    import pcbnew as k

    for net in (pair["p"], pair["n"]):
        leg = [(layer, a, z) for a, z in zip(paths[net], paths[net][1:])]
        metric = path_metrics(
            [(la, a, z) for nn, la, a, z, w in tracks if nn == net] + leg,
            [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
            (tuple(origins[net]), k.F_Cu),
            (tuple(ends[net]), k.F_Cu),
            layer_heights={k.F_Cu: 0, k.B_Cu: thickness},
        )
        if not metric.get("valid"):
            return dict(net=net, reason=metric.get("reason"), connected=metric.get("connected"))
    return None


def _pair_plan_order(
    b, pair, rules, oracle, bounds, pitch, auxiliary_order, topology=None, limits=None
):
    import pcbnew as k

    topology = topology or {}
    hand = topology.get("bridge_hand", 0)
    takeoff = topology.get("takeoff", "auxiliary_source")
    p, n = pair["p"], pair["n"]
    pads = [pad for f in b.GetFootprints() for pad in f.Pads() if pad.GetNetname() in (p, n)]
    groups = defaultdict(lambda: defaultdict(list))
    for pad in pads:
        groups[pad.GetParentFootprint().GetReference()][pad.GetNetname()].append(pad)
    # Native topology is explicit: each package must expose both polarities.
    if any(set(g) != {p, n} for g in groups.values()):
        return dict(status="pair_terminal_topology_unsupported")
    # Source annotations supply an ordered pair path through connector/ESD/load.
    endpoints = pair.get("terminal_chain", [])
    if len(endpoints) < 2:
        return dict(status="missing_pair_terminal_chain")
    paths = {p: [], n: []}
    tracks = []
    metrics = []
    vias = []
    la = k.F_Cu
    reference_validator = pair_reference_validator(b, pair, rules)
    if not reference_validator.available:
        return dict(status="pair_reference_plane_discontinuity")
    accumulated = {p: 0, n: 0}
    # Duplicate USB-C contacts are explicitly bounded local branches, not a
    # second independently routed long pair. Each branch still needs a native
    # path and an endpoint timing check; crossovers may use checked vias.
    from pnr.route.detail.layered import route_layers

    auxiliary = []
    branch_paths = []
    joint_source_port = None
    diameter, drill = pair_via_geometry(rules)
    bylabel = {pad.GetParentFootprint().GetReference() + "." + pad.GetNumber(): pad for pad in pads}
    for group in pair.get("auxiliary_pairs", []):
        lengths = {}
        group_paths = {}
        for key in auxiliary_order:
            net = pair[key]
            first, last = bylabel[group["source"][key]], bylabel[group["target"][key]]
            a, z = xy(first.GetPosition()), xy(last.GetPosition())
            region = [
                min(a[0], z[0]) - 2,
                min(a[1], z[1]) - 2,
                max(a[0], z[0]) + 2,
                max(a[1], z[1]) + 2,
            ]
            # Mirrored embeddings constrain the short duplicate-contact bridge
            # to either side of its own chord. This is rigid-transform invariant
            # and keeps literal net/pad assignments; it never swaps polarity.
            side = bridge_half_plane(a, z, hand)
            clear_branch = (
                lambda i, x, y: side(x)
                and side(y)
                and oracle.clear(net, (k.F_Cu, k.B_Cu)[i], x, y, pair["width_mm"], pair=pair)
            )
            if topology.get("bridge_depth_mm"):
                from types import SimpleNamespace

                dx, dy = z[0] - a[0], z[1] - a[1]
                span = math.hypot(dx, dy)
                depth = topology["bridge_depth_mm"]
                mid = (
                    (a[0] + z[0]) / 2 - hand * dy / span * depth,
                    (a[1] + z[1]) / 2 + hand * dx / span * depth,
                )
                left = route_layers(
                    [a],
                    [mid],
                    region,
                    clear_branch,
                    lambda pt: False,
                    pitch=0.05,
                    layers=2,
                    max_expansions=5000,
                    max_vias=0,
                    deadline=oracle.deadline,
                )
                right = route_layers(
                    [mid],
                    [z],
                    region,
                    clear_branch,
                    lambda pt: False,
                    pitch=0.05,
                    layers=2,
                    max_expansions=5000,
                    max_vias=0,
                    deadline=oracle.deadline,
                )
                rr = SimpleNamespace(
                    status="routed" if left.status == right.status == "routed" else "no_channel",
                    path=left.path[:-1] + right.path,
                )
            else:
                rr = route_layers(
                    [a],
                    [z],
                    region,
                    clear_branch,
                    lambda pt: side(pt) and oracle.via(net, pt, diameter, drill),
                    pitch=0.1,
                    layers=2,
                    max_expansions=5000,
                    max_vias=2,
                    deadline=oracle.deadline,
                )

            if rr.status != "routed":
                return dict(status="pair_auxiliary_" + rr.status)
            total = sum(
                (
                    math.dist(a[:2], z[:2])
                    if a[2] == z[2]
                    else rules["electrical_fab"]["board_thickness_mm"]
                )
                for a, z in zip(rr.path, rr.path[1:])
            )
            if total > group["max_length_mm"]:
                return dict(status="pair_auxiliary_length_limit", length_mm=total)
            lengths[net] = total
            group_paths[net] = rr.path
            for a, z in zip(rr.path, rr.path[1:]):
                if a[2] == z[2]:
                    layer = (k.F_Cu, k.B_Cu)[a[2]]
                    tracks.append((net, layer, a[:2], z[:2], pair["width_mm"]))
                    oracle.reserve_track(net, layer, a[:2], z[:2], pair["width_mm"])
                else:
                    vias.append((net, a[:2]))
                    oracle.reserve_via(net, a[:2], diameter, drill)
        if abs(lengths[p] - lengths[n]) > pair["skew_mm"]:
            return dict(status="pair_auxiliary_skew", lengths=lengths)
        auxiliary.append(lengths)
        branch_paths.append(group_paths)
    auxiliary_tracks = list(tracks)
    routing_endpoints = [dict(t) for t in endpoints]
    for group, branch_lengths in zip(pair.get("auxiliary_pairs", []), auxiliary):
        if group["target"] == routing_endpoints[0]:
            keys = {
                "auxiliary_source": ("p", "n"),
                "declared_p_source_n": ("n",),
                "source_p_declared_n": ("p",),
            }.get(takeoff, ())
            for key in keys:
                routing_endpoints[0][key] = group["source"][key]
                accumulated[pair[key]] += branch_lengths[pair[key]]
    if takeoff == "bridge_join_via":
        from pnr.pair_joint import branch_join_port

        groups = list(zip(pair.get("auxiliary_pairs", []), branch_paths))
        found = next((paths for group, paths in groups if group["target"] == endpoints[0]), None)
        if found is None:
            return dict(status="pair_joint_missing_connector_group")
        joint = branch_join_port(
            pair,
            found,
            oracle,
            rules,
            topology.get("join_fraction", 0.5),
            budget_scope=topology.get("auxiliary_budget_scope", "combined"),
        )
        if joint["status"] != "routed":
            return joint
        joint_source_port = joint["port"]
        accumulated = joint["declared_offsets"]
        join_paths = found
    post_bridge = post_bridge_surface_enabled()
    post_bridge_legs = []
    per_run = per_run_uncoupled_enabled()
    stub_cap = stub_max_mm()
    cap = pair.get("max_uncoupled_mm", 2)
    thickness = rules["electrical_fab"]["board_thickness_mm"]
    stub_delay = None
    if os.environ.get("PNR_BUS_CLASSES") == "1":
        # PNR_BUS_CLASSES: the pair's class/annotation stub limit (PNR_PAIR_STUB_MAX_MM
        # is the explicit override). A delay limit searches with an mm cap no stub
        # within it can exceed; the final check is the delay (pnr.si.bus_classes).
        from pnr.si.bus_classes import router_stub_rule

        stub_cap, stub_delay, _ = router_stub_rule(pair, stub_cap)
    stub_ps = limit_ps = None
    if stub_delay is not None:
        # Estimated delay of a planar fanout on the leg layer plus barrels, the quantity the
        # final check measures (it reads to the pad centre, so the estimate to the pad entry
        # never exceeds it): orders target ports short-stub-first and prunes the via-start /
        # via-reuse legs whose stub alone would break the delay limit (review fix 2026-09-30).
        td_stub = stub_delay["td_ps_per_mm"]
        td_la = float(td_stub.get(b.GetLayerName(la), max(td_stub.values())))
        barrel_ps = float(stub_delay["barrel_mm"]) * float(stub_delay["barrel_ps_per_mm"])
        limit_ps = float(stub_delay["max_ps"])
        stub_ps = lambda planar_mm, barrels=0: planar_mm * td_la + barrels * barrel_ps
    # pair_plan's in-line hunt (PNR_PAIR_EARLY_EXIT + PNR_PAIR_PREFER_INLINE after a
    # stub route): no stub (cap 0) and no more vias than the stub route has.
    limits = limits or {}
    if "stub_cap" in limits:
        stub_cap = limits["stub_cap"] if stub_cap is None else min(stub_cap, limits["stub_cap"])
    max_vias = limits.get("max_vias")
    barrel = run_barrel_mm(thickness) if per_run else 0.0  # PNR_PAIR_RUN_BARREL
    trace = src13_trace_enabled()  # where a timed-out search was (pair_plan reports it)
    # PNR_PAIR_RUN_BREAK_AT_PADS: intermediate terminal pads end a continuous run.
    breaks = (
        [xy(bylabel[node[key]].GetPosition()) for node in endpoints[1:-1] for key in ("p", "n")]
        if per_run and run_break_at_pads_enabled()
        else []
    )
    origins = {
        net: xy(bylabel[endpoints[0][key]].GetPosition()) for net, key in ((p, "p"), (n, "n"))
    }
    # PNR_PAIR_PER_RUN_UNCOUPLED measures the declared origin's endpoint path in
    # full: nothing on it is exempt. The duplicate contact's own leg (auxiliary
    # source -> join) is never on that path and keeps its authored max_length_mm;
    # the declared contact's leg to the join via is part of the first continuous
    # run (review fix: every auxiliary track was exempt before, that leg included).
    exempt_tracks = []

    def arriving(points, layer):
        # PNR_PAIR_PER_RUN_UNCOUPLED: uncoupled run already present at a junction.
        runs = route_uncoupled_runs(
            pair,
            tracks,
            vias,
            origins,
            {net: (points[net], layer) for net in (p, n)},
            thickness,
            exempt_tracks,
            breaks,
        )
        return cap if runs is None else max(runs[net]["trailing_mm"] for net in (p, n))

    stub_skips = []
    for stage, (first, last) in enumerate(zip(routing_endpoints, routing_endpoints[1:])):
        if stage:
            actual = {}
            for net, key in ((p, "p"), (n, "n")):
                origin, finish = bylabel[endpoints[0][key]], bylabel[first[key]]
                metric = path_metrics(
                    [(la, a, z) for nn, la, a, z, w in tracks if nn == net],
                    [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
                    (xy(origin.GetPosition()), k.F_Cu),
                    (xy(finish.GetPosition()), k.F_Cu),
                    layer_heights={
                        k.F_Cu: 0,
                        k.B_Cu: rules["electrical_fab"]["board_thickness_mm"],
                    },
                )
                if not metric.get("valid"):
                    return dict(status="pair_partial_endpoint_graph_invalid", partial_metric=metric)
                actual[net] = metric["length_mm"]
            accumulated = actual
        terminals = {}
        for net, key in ((p, "p"), (n, "n")):

            def find(label):
                ref, num = label.rsplit(".", 1)
                found = [
                    pad
                    for pad in pads
                    if pad.GetParentFootprint().GetReference() == ref and pad.GetNumber() == num
                ]
                if len(found) != 1:
                    raise ValueError("ambiguous pair endpoint " + label)
                return found[0]

            a, z = find(first[key]), find(last[key])
            if not a.IsOnLayer(la) or not z.IsOnLayer(la):
                return dict(status="pair_requires_layer_transition")
            terminals[net] = (xy(a.GetPosition()), xy(z.GetPosition()))

        def clear(net, a, z, width):
            return oracle.clear(net, la, a, z, width, pair=pair)

        def envelope(a, z, width):
            return oracle.clear(
                p, la, a, z, width, ignore_nets=(p, n)
            ) and reference_validator.center(tuple(a), tuple(z), width + pair["gap_mm"])

        def surface(leg_terminals, leg_offsets, prior=0.0):
            if not per_run:
                return solve_pair(
                    p,
                    n,
                    leg_terminals,
                    bounds,
                    clear,
                    envelope,
                    pair["width_mm"],
                    pair["gap_mm"],
                    pair["skew_mm"] if stage == len(endpoints) - 2 else 1e9,
                    pitch=pitch,
                    max_expansions=30000,
                    max_attempts=128,
                    max_uncoupled=pair.get("max_uncoupled_mm", 2),
                    offsets=leg_offsets,
                    accept_paths=lambda paths: reference_validator(
                        paths, pair.get("max_uncoupled_mm", 2)
                    ),
                )
            # PNR_PAIR_PER_RUN_UNCOUPLED: the source fanout continues the run of
            # `prior` mm already uncoupled at the start junction.
            head_cap = cap - prior
            if head_cap <= 1e-6:
                return dict(status="pair_uncoupled_budget", arriving_mm=prior)
            # The leg is judged against the plane as the post-fill check will
            # see it: with the clearance apertures of the vias already planned
            # (a bridge's vias next to the pad), as pair_layer_bridge judges its
            # own candidates. This rejects only legs the post-fill check would.
            validator = (
                pair_reference_validator(
                    b, pair, rules, list(vias), base_center=reference_validator.center
                )
                if vias
                else reference_validator
            )

            def leg_envelope(a, z, width):
                return oracle.clear(p, la, a, z, width, ignore_nets=(p, n)) and validator.center(
                    tuple(a), tuple(z), width + pair["gap_mm"]
                )

            # Reference trim: legacy (cap at both ends) unless PNR_PAIR_REF_TRIM_PER_END.
            ref_trim = (head_cap, cap) if ref_trim_per_end_enabled() else (cap, cap)
            # The fanout search keeps the legacy geometric bound (cap at both ends):
            # a fanout is only partly uncoupled, so head_cap would exclude
            # compliant legs (review fix: g1c04's in-line leg). The exact
            # measurement with the arriving run decides (leg_uncoupled_ok).
            leg = solve_pair(
                p,
                n,
                leg_terminals,
                bounds,
                clear,
                leg_envelope,
                pair["width_mm"],
                pair["gap_mm"],
                pair["skew_mm"] if stage == len(endpoints) - 2 else 1e9,
                pitch=pitch,
                max_expansions=30000,
                max_attempts=128,
                max_uncoupled=cap,
                max_uncoupled_head=cap,
                max_uncoupled_tail=cap,
                offsets=leg_offsets,
                accept_paths=lambda paths: validator(paths, *ref_trim)
                and leg_uncoupled_ok(pair, paths, prior, 0.0),
            )
            if leg["status"] == "routed":
                leg["uncoupled_budget_mm"] = dict(head=head_cap, tail=cap, arriving=prior)
                leg["reference_trim_mm"] = list(ref_trim)
            return leg

        # The run arriving at this stage's source pad (none at a run-breaking pad).
        pad_prior = (
            arriving({net: terminals[net][0] for net in (p, n)}, la)
            if per_run and stage and not breaks
            else 0.0
        )
        if (
            per_run
            and stage == 0
            and joint_source_port
            and joint_source_port.get("auxiliary_budget_scope") == "separate"
        ):
            # The declared contact's own leg to the join via arrives at the join
            # (its measured trailing uncoupled run; the whole leg if unmeasurable)
            # and continues into the stage-0 bridge head.
            join_layer = {net: (k.F_Cu, k.B_Cu)[join_paths[net][0][2]] for net in (p, n)}
            runs = route_uncoupled_runs(
                pair,
                tracks,
                vias,
                origins,
                {net: (joint_source_port["sites"][net], join_layer[net]) for net in (p, n)},
                thickness,
                exempt_tracks,
                breaks,
            )
            pad_prior = (
                max(runs[net]["trailing_mm"] for net in (p, n))
                if runs is not None
                else max(accumulated.values())
            )
        stage_pads = {net: bylabel[first[key]] for net, key in ((p, "p"), (n, "n"))}
        if trace:
            oracle.progress = dict(stage=stage, step="surface")

        def leg_graph_failure(leg):
            # PNR_PAIR_POST_BRIDGE_SURFACE: the leg must leave the planned copper
            # a valid endpoint tree (the final check would reject a loop anyway).
            return surface_leg_graph_failure(
                pair,
                tracks,
                vias,
                leg["paths"],
                la,
                {
                    net: xy(bylabel[endpoints[0][key]].GetPosition())
                    for net, key in ((p, "p"), (n, "n"))
                },
                {net: terminals[net][1] for net in (p, n)},
                rules["electrical_fab"]["board_thickness_mm"],
            )

        result = (
            dict(status="joint_source_via_requested")
            if (stage == 0 and joint_source_port)
            else surface(terminals, accumulated, pad_prior)
        )
        if post_bridge and result["status"] == "routed":
            broken = leg_graph_failure(result)
            if broken:
                post_bridge_legs.append(
                    dict(
                        stage=stage,
                        start="pad",
                        status="pair_surface_leg_cycle",
                        net=broken["net"],
                        reason=broken.get("reason"),
                    )
                )
                result = dict(
                    status="pair_surface_leg_cycle", net=broken["net"], reason=broken.get("reason")
                )
            elif metrics and metrics[-1].get("bridge_target"):
                post_bridge_legs.append(dict(stage=stage, start="pad", status="routed"))
        if (
            post_bridge
            and result["status"] != "routed"
            and metrics
            and metrics[-1].get("bridge_target")
        ):
            # Start the surface leg at the previous bridge's via pair; its
            # via-to-pad fanout stays as the intermediate device's stub (the
            # topology of the bridge-to-bridge reuse below, without new vias).
            old = metrics[-1]["bridge_target"]
            via_terminals = {net: (tuple(old["sites"][net]), terminals[net][1]) for net in (p, n)}
            via_offsets = {net: accumulated[net] - old["lengths"][net] for net in (p, n)}
            # The stub this leg would leave: the via-to-pad fanout up to where it
            # enters the pad (as pair_stub_metrics measures it).
            fanout_stub = port_stub_mm(old, stage_pads, la) if stub_cap is not None else None
            if stub_cap is not None and (
                fanout_stub > stub_cap + 1e-9
                or (stub_ps is not None and stub_ps(fanout_stub) > limit_ps + 1e-9)
            ):
                # PNR_PAIR_STUB_MAX_MM: this leg would leave the via-to-pad
                # fanout as the intermediate terminal's stub (PNR_BUS_CLASSES: its delay).
                alternative = dict(status="pair_stub_limit", stub_mm=fanout_stub)
                stub_skips.append(
                    dict(
                        stage=stage,
                        start="bridge_vias",
                        stub_mm=round(fanout_stub, 6),
                        **(
                            {"stub_ps": round(stub_ps(fanout_stub), 3)}
                            if stub_ps is not None
                            else {}
                        )
                    )
                )
            else:
                if trace:
                    oracle.progress = dict(stage=stage, step="via_start")
                # PNR_PAIR_RUN_BARREL: the hop from the B.Cu trunk up to the leg.
                alternative = surface(
                    via_terminals,
                    via_offsets,
                    arriving(old["sites"], k.B_Cu) + barrel if per_run else 0.0,
                )
            record = dict(
                stage=stage,
                start="bridge_vias",
                status=alternative["status"],
                sites={net: via_terminals[net][0] for net in (p, n)},
            )
            if alternative["status"] == "routed":
                broken = leg_graph_failure(alternative)
                if broken:
                    record.update(
                        status="pair_surface_leg_cycle",
                        net=broken["net"],
                        reason=broken.get("reason"),
                    )
                    alternative = dict(status="pair_surface_leg_cycle")
            else:
                record["failures"] = alternative.get("failures", {})
            post_bridge_legs.append(record)
            if alternative["status"] == "routed":
                alternative["post_bridge_start"] = dict(
                    sites={net: via_terminals[net][0] for net in (p, n)},
                    stub_lengths=dict(old["lengths"]),
                )
                result = alternative
            else:
                result = dict(result, post_bridge_via_start=record)
        if result["status"] != "routed":
            surface_failure = result
            reuse = joint_source_port if stage == 0 else None
            bridge_offsets = accumulated
            bridge_prior = pad_prior
            if metrics and metrics[-1].get("bridge_target"):
                old = metrics[-1]["bridge_target"]
                reuse = dict(
                    sites=old["sites"],
                    paths={net: [] for net in (p, n)},
                    lengths={net: 0 for net in (p, n)},
                    reuse=True,
                )
                # The prior surface leg becomes an ESD stub, not a round trip
                # in the connector-to-receiver timing path.
                bridge_offsets = {
                    net: accumulated[net]
                    - old["lengths"][net]
                    - rules["electrical_fab"]["board_thickness_mm"]
                    for net in (p, n)
                }
                if per_run:
                    bridge_prior = arriving(old["sites"], k.B_Cu)
                reuse_fanout = port_stub_mm(old, stage_pads, la) if stub_cap is not None else None
                if stub_cap is not None and (
                    stub_barrel_mm(thickness) + reuse_fanout > stub_cap + 1e-9
                    or (stub_ps is not None and stub_ps(reuse_fanout, 1) > limit_ps + 1e-9)
                ):
                    # PNR_PAIR_STUB_MAX_MM: reusing the vias leaves barrel +
                    # fanout as the terminal's stub (PNR_BUS_CLASSES: its delay). Bridge
                    # from the terminal pad itself instead (terminal in line).
                    stub_skips.append(
                        dict(
                            stage=stage,
                            start="via_reuse",
                            stub_mm=round(stub_barrel_mm(thickness) + reuse_fanout, 6),
                            **(
                                {"stub_ps": round(stub_ps(reuse_fanout, 1), 3)}
                                if stub_ps is not None
                                else {}
                            )
                        )
                    )
                    reuse = None
                    bridge_offsets = accumulated
                    bridge_prior = pad_prior
            extra_bridge = {}
            if per_run:
                extra_bridge["head_prior_mm"] = bridge_prior
            if stub_cap is not None and stage < len(routing_endpoints) - 2:
                extra_bridge["target_stub_cap"] = stub_cap
                target_pads = {net: bylabel[last[key]] for net, key in ((p, "p"), (n, "n"))}
                extra_bridge["target_stub_length"] = (
                    lambda port, target_pads=target_pads: port_stub_mm(port, target_pads, la)
                )
                if stub_ps is not None:
                    extra_bridge["target_stub_delay"] = (stub_ps, limit_ps)
            if (
                max_vias is not None
                and len(vias) + (2 if (reuse and reuse.get("reuse")) else 4) > max_vias
            ):
                # In-line hunt: this bridge would need more vias than the stub route.
                return dict(
                    status="pair_via_limit",
                    failed_stage=stage,
                    max_vias=max_vias,
                    planned_vias=len(vias),
                    surface_failure=surface_failure,
                    **({"stub_skips": stub_skips} if stub_cap is not None else {})
                )
            if trace:
                oracle.progress = dict(
                    stage=stage,
                    step="bridge",
                    reuse=bool(reuse and reuse.get("reuse")),
                    joint=bool(stage == 0 and joint_source_port),
                )
            result = pair_layer_bridge(
                b,
                pair,
                terminals,
                rules,
                oracle,
                bounds,
                pitch,
                bridge_offsets,
                reuse_source=reuse,
                reference_validator=reference_validator,
                prior_reference=[m["reference_paths"] for m in metrics],
                prior_vias=vias,
                solution_index=topology.get("target_port_rank", 0) if stage == 0 else 0,
                max_expansions=60000 if stage and takeoff == "bridge_join_via" else 15000,
                timing_target_mm=topology.get("prefix_timing_target_mm", 0) if stage == 0 else 0,
                **extra_bridge
            )
            if result["status"] != "routed":
                return dict(
                    result,
                    failed_stage=stage,
                    terminals=terminals,
                    surface_failure=surface_failure,
                    planned_tracks=tracks,
                    joint_source_port=joint_source_port,
                    **({"post_bridge_legs": post_bridge_legs} if post_bridge else {}),
                    **({"stub_skips": stub_skips} if stub_cap is not None else {})
                )
        if result.get("pair_tracks"):
            metrics.append(result)
            accumulated = dict(result["lengths"])
            tracks.extend(result["pair_tracks"])
            vias.extend(result["pair_vias"])
            for net, layer, x, y, w in result["pair_tracks"]:
                oracle.reserve_track(net, layer, x, y, w)
            for net, point in result["pair_vias"]:
                oracle.reserve_via(net, point, result["via_diameter_mm"], result["via_drill_mm"])
            continue
        from pnr.route.detail.coupled import trim_path

        cap = pair.get("max_uncoupled_mm", 2)
        if per_run and ref_trim_per_end_enabled():
            result["reference_paths"] = {
                net: trim_path(
                    path,
                    result["uncoupled_budget_mm"]["head"],
                    result["uncoupled_budget_mm"]["tail"],
                )
                for net, path in result["paths"].items()
            }
        else:
            result["reference_paths"] = {
                net: trim_path(path, cap, cap) for net, path in result["paths"].items()
            }
        metrics.append(result)
        accumulated = dict(result["lengths"])
        for net, path in result["paths"].items():
            paths[net] += path
            for a, z in zip(path, path[1:]):
                tracks.append((net, la, a, z, pair["width_mm"]))
                oracle.reserve_track(net, la, a, z, pair["width_mm"])
    used = {x[key] for x in endpoints for key in ("p", "n")}
    used |= {
        t[side][key]
        for t in pair.get("auxiliary_pairs", [])
        for side in ("source", "target")
        for key in ("p", "n")
    }
    if any(label not in used for label in bylabel):
        return dict(status="pair_unassigned_terminals")
    # Exact polygon containment over the coupled trunk. Fanouts have the
    # explicit bounded uncoupled allowance; impedance needs a real fab stackup.
    reference = b.GetLayerID(pair.get("reference_layer", "In1.Cu"))
    plane_nets = {
        n
        for c in rules.get("net_classes", [])
        if c.get("plane_layer") == pair.get("reference_layer", "In1.Cu")
        for n in c["nets"]
    }
    fill = k.SHAPE_POLY_SET()
    for zone in b.Zones():
        if (
            not zone.GetIsRuleArea()
            and zone.IsOnLayer(reference)
            and zone.GetNetname() in plane_nets
        ):
            fill.BooleanAdd(zone.GetFilledPolysList(reference))
    for segment in metrics:
        # Inspect the actual tuned copper, not just the untuned centerline.
        # Only the explicitly bounded terminal fanouts may leave the reference.
        for path in segment["reference_paths"].values():
            for a, z in zip(path, path[1:]):
                probe = k.PCB_TRACK(b)
                probe.SetLayer(k.F_Cu)
                probe.SetStart(vec(a))
                probe.SetEnd(vec(z))
                probe.SetWidth(round((pair["width_mm"] + pair["gap_mm"]) * 1e6))
                copper = k.SHAPE_POLY_SET()
                probe.TransformShapeToPolygon(copper, k.F_Cu, 0, 1000, k.ERROR_OUTSIDE)
                copper.BooleanSubtract(fill)
                if not copper.IsEmpty():
                    return dict(status="pair_reference_plane_discontinuity")
    # Endpoint graph lengths exclude ESD side branches and duplicate copper.
    endpoint_metrics = {}
    for net, key in ((p, "p"), (n, "n")):
        first, last = bylabel[endpoints[0][key]], bylabel[endpoints[-1][key]]
        endpoint_metrics[net] = path_metrics(
            [(la, a, z) for nn, la, a, z, w in tracks if nn == net],
            [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in vias if nn == net],
            (xy(first.GetPosition()), k.F_Cu),
            (xy(last.GetPosition()), k.F_Cu),
            layer_heights={k.F_Cu: 0, k.B_Cu: rules["electrical_fab"]["board_thickness_mm"]},
        )
    trace = {"post_bridge_legs": post_bridge_legs} if post_bridge else {}
    if stub_cap is not None:
        trace["stub_skips"] = stub_skips
    if not all(v.get("valid") for v in endpoint_metrics.values()):
        return dict(
            status="pair_endpoint_graph_invalid", endpoint_metrics=endpoint_metrics, **trace
        )
    if (
        abs(endpoint_metrics[p]["length_mm"] - endpoint_metrics[n]["length_mm"])
        > pair["skew_mm"] + 1e-6
    ):
        return dict(status="pair_endpoint_skew", endpoint_metrics=endpoint_metrics, **trace)
    duplicate_metrics = duplicate_endpoint_metrics(
        pair, bylabel, tracks, vias, rules["electrical_fab"]["board_thickness_mm"]
    )
    if not connector_origins_qualified(pair, duplicate_metrics):
        return dict(
            status="pair_duplicate_endpoint_skew",
            endpoint_metrics=endpoint_metrics,
            connector_endpoint_metrics=duplicate_metrics,
            **trace
        )
    if per_run:
        # PNR_PAIR_PER_RUN_UNCOUPLED: every continuous uncoupled run of the
        # final endpoint path (connector origin -> receiver) within the cap.
        runs = route_uncoupled_runs(
            pair,
            tracks,
            vias,
            origins,
            {
                net: (xy(bylabel[endpoints[-1][key]].GetPosition()), k.F_Cu)
                for net, key in ((p, "p"), (n, "n"))
            },
            thickness,
            exempt_tracks,
            breaks,
        )
        trace["uncoupled_run_metrics"] = run_report(runs)
        if runs is None or any(m["max_mm"] > cap + 1e-6 for m in runs.values()):
            return dict(
                status="pair_uncoupled_run_limit",
                endpoint_metrics=endpoint_metrics,
                connector_endpoint_metrics=duplicate_metrics,
                **trace
            )
    if stub_cap is not None and stub_delay is not None:
        # PNR_BUS_CLASSES delay limit (barrel included, pad centre); the in-line hunt's
        # mm cap (limits['stub_cap'] = 0) still applies on top.
        trace["stub_metrics"] = pair_stub_metrics(
            pair, bylabel, tracks, vias, thickness, barrel=thickness, delay=stub_delay
        )
        over = stub_over_delay(trace["stub_metrics"], float(stub_delay["max_ps"]))
        if "stub_cap" in limits:
            over = over or stub_over_cap(trace["stub_metrics"], limits["stub_cap"])
        if over:
            return dict(
                status="pair_stub_limit",
                endpoint_metrics=endpoint_metrics,
                connector_endpoint_metrics=duplicate_metrics,
                **trace
            )
    elif stub_cap is not None:
        trace["stub_metrics"] = pair_stub_metrics(
            pair, bylabel, tracks, vias, thickness, barrel=stub_barrel_mm(thickness)
        )
        if stub_over_cap(trace["stub_metrics"], stub_cap):
            return dict(
                status="pair_stub_limit",
                endpoint_metrics=endpoint_metrics,
                connector_endpoint_metrics=duplicate_metrics,
                **trace
            )
    return dict(
        status="routed",
        pair_tracks=tracks,
        pair_vias=vias,
        segments=metrics,
        auxiliary=auxiliary,
        topology=topology,
        joint_source_port=joint_source_port,
        endpoint_metrics=endpoint_metrics,
        connector_endpoint_metrics=duplicate_metrics,
        mode="pair",
        via_diameter_mm=diameter,
        via_drill_mm=drill,
        impedance_qualified=False,
        **trace
    )


def screen_pair_placements(board_path, rules, pair, proposals, bounds):
    """Order legal placement proposals by exact paired escape availability.

    This is a routing heuristic, never an acceptance or placement-DRC result.
    Keep zero-via-port candidates last: a surface-only route can still work.
    """
    import pcbnew as k

    result = []
    for proposal in proposals:
        b = k.LoadBoard(str(board_path))
        removed = [t for t in b.GetTracks() if t.GetNetname() in (pair["p"], pair["n"])]
        if any(t.IsLocked() for t in removed):
            raise ValueError("pair copper locked")
        # Delete, not Remove: b and removed are rebound per proposal, which freed
        # each board before its Removed items.
        for t in removed:
            b.Delete(t)
        b.BuildConnectivity()
        try:
            move_pair_support(b, pair, proposal)
        except ValueError:
            continue
        labels = {
            f.GetReference() + "." + p.GetNumber(): xy(p.GetPosition())
            for f in b.GetFootprints()
            for p in f.Pads()
        }
        chain = pair["terminal_chain"]
        index = next(
            i
            for i, t in enumerate(chain)
            if any(v.rsplit(".", 1)[0] == proposal["ref"] for v in t.values())
        )
        source = chain[index - 1]
        if index == 1:
            for aux in pair.get("auxiliary_pairs", []):
                if aux["target"] == source:
                    source = aux["source"]
        terminals = {
            pair[key]: (labels[source[key]], labels[chain[index][key]]) for key in ("p", "n")
        }
        ports = pair_bridge_ports(pair, terminals, rules, Oracle(b, rules), bounds, 1)
        result.append(dict(proposal, paired_via_ports=len(ports)))
    return sorted(result, key=lambda p: (not bool(p["paired_via_ports"]), p["score"]))


if __name__ == "__main__":
    from pnr.profile import run

    run("native-electrical", main)
