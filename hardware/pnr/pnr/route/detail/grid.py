"""Routing grid + obstacle + pin-access model (detailed router, R2).

The geometric substrate the maze router (R4) works on. A uniform grid per signal
layer where each cell is either free, a static **obstacle**, or a **pad cell**
owned by one net (routable only by that net — its access point). The grid **pitch**
is chosen ≥ track-width + clearance (and ≥ via + clearance), so any two routes in
non-adjacent cells are automatically DRC-legal — *DRC-by-construction*, the way
grid routers guarantee clean spacing.

Pure numpy + stdlib on the placed :class:`pnr.graph.BoardGraph` — no pcbnew. Frame:
mm, origin bottom-left (see :mod:`pnr.graph`); layer 0 is the top (F.Cu) signal
layer, the last index the bottom (B.Cu). Inner layers are planes (not routed here).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from pnr.graph import SIDE_BOTTOM, BoardGraph

from ...place.geometry import Rect, pad_rects

# Default signal layers (outer copper); inner layers carry the power/ground planes.
DEFAULT_SIGNAL_LAYERS = ("F.Cu", "B.Cu")


@dataclass(frozen=True)
class Cell:
    """A grid node: layer index + column/row."""

    layer: int
    i: int
    j: int


class RouteGrid:
    """Per-layer occupancy grid built from a placed board."""

    def __init__(
        self,
        width: float,
        height: float,
        pitch: float,
        layers: Tuple[str, ...] = DEFAULT_SIGNAL_LAYERS,
        clearance: float = 0.15,
        track_width: float = 0.15,
        via_radius: float = 0.225,
    ):
        self.width = float(width)
        self.height = float(height)
        self.pitch = float(pitch)
        self.layers = tuple(layers)
        self.clearance = float(clearance)
        self.track_width = float(track_width)
        self.via_radius = float(via_radius)
        self.nlayers = len(layers)
        self.nx = max(1, int(np.ceil(width / pitch)))
        self.ny = max(1, int(np.ceil(height / pitch)))
        # Static obstacles (True = never routable) per layer.
        self.blocked = np.zeros((self.nlayers, self.ny, self.nx), dtype=bool)
        self.via_blocked = np.zeros_like(self.blocked)
        # Pad cells: (layer, i, j) -> net name (routable only by that net; the net's
        # connection point). This is the pad body + its *track* clearance halo
        # (clearance + ½track) — a track may run this close to the pad.
        self.pad_net: Dict[Tuple[int, int, int], str] = {}
        # A wider *via* clearance halo (clearance + via radius) around each pad,
        # (layer, i, j) -> net: a via (much fatter than a track) must stay this far
        # from the pad, but a TRACK may enter it. Keeping the two separate is what
        # frees the pad-dense top layer for tracks instead of over-reserving it at
        # via width (which forced routing onto the back layer).
        self.via_halo: Dict[Tuple[int, int, int], str] = {}
        # Access cell per (net, pad_key) recorded during build.
        self.access: Dict[Tuple[str, str], Cell] = {}
        self.pad_rectangles = []
        self.escape_segments = []
        self.escape_vias = []
        self.net_widths = {}
        # net -> pad clearance table at that net's own width, for nets too wide
        # for the pad track halo (reserve_wide_pad_clearance); others: absent.
        self.wide_pad_net: Dict[str, Dict[Tuple[int, int, int], str]] = {}
        self.via_spacing = 2 * self.via_radius
        self.via_drill_radius = self.via_radius  # conservative until fab rules supply the drill
        self.hole_clearance = 0.2
        self.source_drills = []  # centre, conservative radius; slots use enclosing circle
        # Fab-profile hole kinds (None = one hole_clearance for every drill, the
        # pre-profile rule): plated flag per source drill, aligned by index.
        self.source_drill_plated = []
        self.pth_hole_gap = None  # via drill to component PTH drill
        self.npth_hole_gap = None  # via drill to NPTH drill
        self.via_hole_gap = None  # via drill to via-class drill (None: hole_clearance)
        # Plated pad drills under this are a footprint's vias (5A Component PTH
        # hole 0.30 or more): via rules, not PTH ones. None: every plated pad is PTH.
        self.component_pth_min_drill = None
        self.drilled_pads = []  # (layers, net, centre, drill radius, plated)
        self.plated_ports = []  # net, exact centre, conservative in-land radius
        # Surface pads (layer, net, Rect, land corner) and the fab profile's
        # via-to-SMD-pad rule (restrict_smd_vias: 5A via copper 0.127 from any SMD
        # pad, 5B filled in-pad class inside an own-net pad). The corner is the
        # exact land's corner radius (graph Pad.land_corner), None when the Rect
        # only bounds the copper (no in-pad via there). None rule: the pre-profile
        # grid, where a via may enter or graze its own net's pads.
        self.smd_pads = []
        self.in_pad = None
        self.via_to_smd_pad = None
        self.smd_via_blocked = None  # (ny, nx) bool, cell-centre verdicts
        self._smd_index = None

    def plated_transition(self, net, i, j):
        """Exact source PTH centre when this column fits its existing copper land.

        Only positively identified plated pads participate. The inscribed circle
        is conservative for native round/rectangular/oval lands. Route writeback
        bonds each used layer to this centre and emits no duplicate drill.
        """
        import math

        point = self.center_of(i, j)
        width = self.net_widths.get(net, self.track_width)
        for owner, centre, radius in self.plated_ports:
            if owner == net and math.dist(point, centre) + width / 2 <= radius - 1e-7:
                return centre
        return None

    def hole_site_clear(self, point, sites=()):
        """Same-net copper may merge; two distinct drills still need spacing."""
        import math

        if self.pth_hole_gap is None:
            if any(
                math.dist(point, p) < radius + self.via_drill_radius + self.hole_clearance - 1e-7
                for p, radius in self.source_drills
            ):
                return False
        elif any(
            math.dist(point, p) < radius + self.via_drill_radius + self._source_hole_gap(k) - 1e-7
            for k, (p, radius) in enumerate(self.source_drills)
        ):
            return False
        return all(
            math.dist(point, p) < 1e-7 or math.dist(point, p) >= self.via_spacing - 1e-7
            for p in [xy for _, xy in self.escape_vias] + list(sites)
        )

    def _via_class_drill(self, radius: float) -> bool:
        """A plated pad drill of this radius is a footprint's via (not a component PTH)."""
        return (
            self.component_pth_min_drill is not None
            and 2 * radius < self.component_pth_min_drill - 1e-9
        )

    def _source_hole_gap(self, index):
        """Profile drill gap from a new via to source drill ``index``."""
        plated = self.source_drill_plated[index] if index < len(self.source_drill_plated) else None
        if plated and self._via_class_drill(self.source_drills[index][1]):
            return self.via_hole_gap if self.via_hole_gap is not None else self.hole_clearance
        npth = self.npth_hole_gap if self.npth_hole_gap is not None else self.pth_hole_gap
        return (
            self.pth_hole_gap
            if plated
            else npth if plated is False else max(self.pth_hole_gap, npth)
        )

    def mark_pth_hole_keepouts(
        self, pth_hole_clearance: float, via_hole_clearance: Optional[float] = None
    ) -> None:
        """Fab profile: foreign tracks keep ``pth_hole_clearance`` from a
        component PTH drill wall even where its copper land is narrow (the pad
        track halo only guarantees ``clearance`` from the land). A footprint's
        via-class drill keeps ``via_hole_clearance`` instead (none if None)."""
        for layers, net, (cx, cy), radius, plated in self.drilled_pads:
            if not plated:
                continue
            gap = via_hole_clearance if self._via_class_drill(radius) else pth_hole_clearance
            if gap is None:
                continue
            hole = Rect(cx, cy, 2 * radius, 2 * radius)
            for la in layers:

                def reserve(L, i, j, owner_net=net):
                    key = (L, i, j)
                    owner = self.pad_net.get(key)
                    self.pad_net[key] = (
                        owner_net if owner is None or owner == owner_net else "\0conflict"
                    )

                self._mark_rect(la, hole, gap + 0.5 * self.track_width, reserve)

    def block_edge_inset_split(self, track_inset: float, via_inset: float) -> None:
        """Fab profile edge rules: copper-to-edge for tracks, and for vias the
        larger of copper-to-edge and hole-to-edge (pad cells left alone, as in
        :meth:`block_edge_inset`)."""
        for j in range(self.ny):
            for i in range(self.nx):
                d = min(
                    (i + 0.5) * self.pitch,
                    (j + 0.5) * self.pitch,
                    self.width - (i + 0.5) * self.pitch,
                    self.height - (j + 0.5) * self.pitch,
                )
                if d >= max(track_inset, via_inset) - 1e-9:
                    continue
                for la in range(self.nlayers):
                    if (la, i, j) in self.pad_net:
                        continue
                    if d < track_inset - 1e-9:
                        self.blocked[la, j, i] = True
                    if d < via_inset - 1e-9:
                        self.via_blocked[la, j, i] = True

    # -- coordinate mapping --------------------------------------------------

    def cell_of(self, x: float, y: float) -> Tuple[int, int]:
        i = min(self.nx - 1, max(0, int(x / self.pitch)))
        j = min(self.ny - 1, max(0, int(y / self.pitch)))
        return i, j

    def center_of(self, i: int, j: int) -> Tuple[float, float]:
        return ((i + 0.5) * self.pitch, (j + 0.5) * self.pitch)

    def in_bounds(self, i: int, j: int) -> bool:
        return 0 <= i < self.nx and 0 <= j < self.ny

    def side_layer(self, side: str) -> int:
        """Signal-layer index for a component side (top→0, bottom→last)."""
        return self.nlayers - 1 if side == SIDE_BOTTOM else 0

    # -- occupancy queries ---------------------------------------------------

    def passable(self, layer: int, i: int, j: int, net: Optional[str] = None) -> bool:
        """True if net ``net`` may occupy cell (layer, i, j): in bounds, not a
        static obstacle, and either free of pads or a pad of its own net (for a
        net in :attr:`wide_pad_net`, free of the pads' halos at its width too)."""
        if not self.in_bounds(i, j):
            return False
        if self.blocked[layer, j, i]:
            return False
        owner = self.pad_net.get((layer, i, j))
        if owner is not None and owner != net:
            return False
        wide = self.wide_pad_net.get(net)
        if wide is None:
            return True
        owner = wide.get((layer, i, j))
        return owner is None or owner == net

    def via_passable(
        self,
        layer: int,
        i: int,
        j: int,
        net: Optional[str] = None,
        point: Optional[Tuple[float, float]] = None,
    ) -> bool:
        """True if net ``net`` may drop a **via** at cell (layer, i, j): passable for
        a track *and* clear of every other net's wider via-halo (a via is fatter than
        a track, so it needs more room from foreign pads). Under a fab profile the
        via-to-SMD-pad rule applies at the cell centre, or at ``point`` (an exact
        off-grid via site such as a pad centre) when given."""
        if not self.in_bounds(i, j) or self.via_blocked[layer, j, i]:
            return False
        if self.smd_via_blocked is not None:
            if point is not None:
                if not self.smd_via_ok(point, net):
                    return False
            elif self.smd_via_blocked[j, i]:
                return False
        owner = self.pad_net.get((layer, i, j))
        if owner is not None and owner != net:
            return False
        vh = self.via_halo.get((layer, i, j))
        return vh is None or vh == net

    def smd_via_ok(self, point: Tuple[float, float], net: Optional[str] = None) -> bool:
        """Fab profile via-to-SMD-pad rule for a via centred at ``point`` (mm).

        Outside every surface pad the default via keeps ``via_to_smd_pad`` from each
        one (5A).
        A via whose centre lies in pads (all of one net, ``net`` when given) must be
        a 5B filled in-pad via there: :func:`pnr.fab_profile.in_pad_fit` on the pad
        rectangle with its land corner. Only exact lands qualify: a Rect that only
        bounds a custom, chamfered, rotated or offset land (corner None) can hold
        points with no copper or with too little hole margin, so it admits no via.
        Emission re-checks the native land and sizes it (pnr.via_in_pad.in_pad_size).
        """
        from pnr.fab_profile import in_pad_fit

        keep = self.via_radius + self.via_to_smd_pad + 0.001
        index = self._smd_index
        if index is None or index[0] != keep:
            buckets: Dict[Tuple[int, int], List[int]] = {}
            for k, (_, _, r, _) in enumerate(self.smd_pads):
                for bx in range(math.floor(r.left - keep), math.floor(r.right + keep) + 1):
                    for by in range(math.floor(r.bottom - keep), math.floor(r.top + keep) + 1):
                        buckets.setdefault((bx, by), []).append(k)
            index = self._smd_index = (keep, buckets)
        inside = []
        for k in index[1].get((math.floor(point[0]), math.floor(point[1])), ()):
            _, owner, r, corner = self.smd_pads[k]
            dx = max(r.left - point[0], 0.0, point[0] - r.right)
            dy = max(r.bottom - point[1], 0.0, point[1] - r.top)
            if dx == 0.0 and dy == 0.0:
                inside.append((owner, r, corner))
            elif math.hypot(dx, dy) < keep - 1e-9:
                return False
        if not inside:
            return True
        ip = self.in_pad
        owners = {owner for owner, _, _ in inside}
        if (
            ip is None
            or len(owners) != 1
            or not next(iter(owners))
            or (net is not None and owners != {net})
        ):
            return False
        if any(corner is None for _, _, corner in inside):
            return False
        return any(
            all(
                in_pad_fit(ip, (r.w, r.h), (point[0] - r.cx, point[1] - r.cy), d, ip.drill, corner)
                for _, r, corner in inside
            )
            for d in ip.diameters
        )

    def restrict_smd_vias(self, in_pad, via_to_smd_pad: float) -> None:
        """Apply the fab profile's via-to-SMD-pad rule (:meth:`smd_via_ok`) to every
        cell near a surface pad; the grid is unchanged elsewhere."""
        self.in_pad = in_pad
        self.via_to_smd_pad = float(via_to_smd_pad)
        self.smd_via_blocked = np.zeros((self.ny, self.nx), dtype=bool)
        reach = self.via_radius + self.via_to_smd_pad + self.pitch
        seen = set()
        for _, _, r, _ in self.smd_pads:
            i0 = max(0, int((r.left - reach) / self.pitch))
            i1 = min(self.nx - 1, int((r.right + reach) / self.pitch))
            j0 = max(0, int((r.bottom - reach) / self.pitch))
            j1 = min(self.ny - 1, int((r.top + reach) / self.pitch))
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    if (i, j) in seen:
                        continue
                    seen.add((i, j))
                    if not self.smd_via_ok(self.center_of(i, j)):
                        self.smd_via_blocked[j, i] = True

    # -- construction --------------------------------------------------------

    def _mark_rect(self, layer: int, r: Rect, grow: float, setter) -> None:
        """Apply ``setter(layer, i, j)`` over cells touched by ``r`` grown by ``grow``."""
        i0 = max(0, int((r.left - grow) / self.pitch))
        i1 = min(self.nx - 1, int((r.right + grow) / self.pitch))
        j0 = max(0, int((r.bottom - grow) / self.pitch))
        j1 = min(self.ny - 1, int((r.top + grow) / self.pitch))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                setter(layer, i, j)

    def add_pad(self, layer: int, net: str, r: Rect) -> None:
        """Record a pad with a **two-tier clearance halo** so the pad-dense layer
        stays routable by tracks:

        * body cells → own-net (hard-set; pad copper wins over another pad's halo);
        * a **track halo** (``clearance + ½track``) → own-net (``setdefault``): only
          this net's tracks/vias may come this close — a foreign track here would
          short the pad;
        * a wider **via halo** (``clearance + via_radius``) → recorded separately in
          ``via_halo``: a foreign *via* (fatter than a track) must stay outside this,
          but a foreign *track* may enter it.

        Reserving the whole via-width around every pad (the old behaviour) walled off
        the top layer for tracks and pushed routing to the back; the track halo is the
        tighter reservation a track actually needs. The pad's centre is its access.
        """

        self.pad_rectangles.append((layer, net, r))

        def reserve(table, la, i, j):
            key = (la, i, j)
            owner = table.get(key)
            # An overlap belongs to neither net. A later pad must never erase a
            # foreign pad's clearance halo (or vice versa).
            table[key] = net if owner is None or owner == net else "\0conflict"

        self._mark_rect(
            layer,
            r,
            self.clearance + self.via_radius,
            lambda la, i, j: reserve(self.via_halo, la, i, j),
        )
        self._mark_rect(
            layer,
            r,
            self.clearance + 0.5 * self.track_width,
            lambda la, i, j: reserve(self.pad_net, la, i, j),
        )

    def reserve_wide_pad_clearance(self) -> None:
        """Give every net too wide for the pad track halo its own pad halo.

        A route edge runs between two cell centres (a 45° step also needs its two
        corner cells), so it stays inside cells that miss every foreign pad's track
        halo rectangle (``clearance + ½track``, all cells it touches): at least
        ``clearance + ½track + ½pitch`` from the pad. That clears a track up to
        ``track + pitch`` wide. A wider net (``net_widths``) gets a table like
        :attr:`pad_net` grown by ``clearance + ½width - ½pitch`` instead, which
        :meth:`passable` also reads for it. The cells a pad's rectangle touches
        belong to the pad's net in that table whatever halo covers them, so a
        wide net still lands on each of its pads (from the side away from a close
        neighbour). Nets of the same width share one table; a board without such
        a net gets none.
        """
        base = self.clearance + 0.5 * self.track_width
        tables: Dict[float, Dict[Tuple[int, int, int], str]] = {}
        for net, width in sorted(self.net_widths.items()):
            grow = self.clearance + 0.5 * width - 0.5 * self.pitch
            if grow <= base + 1e-9:
                continue
            table = tables.get(width)
            if table is None:
                table = tables[width] = {}
                body: Dict[Tuple[int, int, int], str] = {}
                for layer, owner, r in self.pad_rectangles:
                    for mark, distance in ((table, grow), (body, 0.0)):

                        def reserve(la, i, j, owner=owner, mark=mark):
                            key = (la, i, j)
                            old = mark.get(key)
                            mark[key] = owner if old is None or old == owner else "\0conflict"

                        self._mark_rect(layer, r, distance, reserve)
                table.update(body)
            self.wide_pad_net[net] = table

    def block_region(
        self,
        r: Rect,
        layers: Optional[List[int]] = None,
        grow: float = 0.0,
        block_vias: bool = True,
    ) -> None:
        """Block a rectangular region (e.g. a keep-out, or no-net copper) on the
        given layers (all by default), grown by ``grow`` — never routable."""
        lays = range(self.nlayers) if layers is None else layers
        for la in lays:
            self._mark_rect(la, r, grow, lambda L, i, j: self.blocked.__setitem__((L, j, i), True))
            if block_vias:
                self._mark_rect(
                    la, r, grow, lambda L, i, j: self.via_blocked.__setitem__((L, j, i), True)
                )

    def block_edge_inset(self, inset: float) -> None:
        """Block every routing cell whose centre is within ``inset`` of the board
        outline (the grid bounds) on all layers, so routed copper keeps its edge
        clearance from the board edge. Pad cells are left alone — a footprint placed
        at the edge (an overhanging edge connector) still needs its access, and its
        edge clearance is the footprint's concern, not the router's."""
        # The final cell can straddle the outline when size/pitch is not an
        # integer. Mirroring a near-edge cell count from nx/ny under-reserves
        # the far edge (fresh119: y=54.775 on a 55 mm board, 0.35 mm pitch).
        # Compare physical centres to the actual outline on all four sides.
        border = [
            (i, j)
            for j in range(self.ny)
            for i in range(self.nx)
            if min(
                (i + 0.5) * self.pitch,
                (j + 0.5) * self.pitch,
                self.width - (i + 0.5) * self.pitch,
                self.height - (j + 0.5) * self.pitch,
            )
            < inset - 1e-9
        ]
        for la in range(self.nlayers):
            for i, j in border:
                if (la, i, j) not in self.pad_net:
                    self.blocked[la, j, i] = True
                    self.via_blocked[la, j, i] = True

    @classmethod
    def from_graph(
        cls,
        graph: BoardGraph,
        width: float,
        height: float,
        *,
        pitch: float = 0.25,
        clearance: float = 0.15,
        track_width: float = 0.15,
        via_radius: float = 0.225,
        layers: Tuple[str, ...] = DEFAULT_SIGNAL_LAYERS,
    ) -> "RouteGrid":
        """Build the grid from a placed graph: pads become access points +
        own-net cells; the outline is the grid bounds."""
        g = cls(
            width,
            height,
            pitch,
            layers=layers,
            clearance=clearance,
            track_width=track_width,
            via_radius=via_radius,
        )
        for comp in graph.components:
            side = g.side_layer(comp.side)
            # pad_rects is exact only at quarter-turn part rotations.
            quarter = abs(comp.rot / 90.0 - round(comp.rot / 90.0)) < 1e-6
            for (name, net, r), pad in zip(pad_rects(comp), comp.pads):
                land = pad.land_corner if quarter else None
                # A through-hole pad occupies (and must be cleared on) *every* signal
                # layer; an SMD pad only the component's side.
                pad_layers = tuple(range(g.nlayers)) if pad.through_hole else (side,)
                if max(pad.drill_size) > 0:
                    # Circular envelope is conservative for oval/rotated slots;
                    # source drills are obstacles even for copper on the same net.
                    g.source_drills.append(((r.cx, r.cy), max(pad.drill_size) / 2))
                    g.source_drill_plated.append(pad.plated)
                    g.drilled_pads.append(
                        (pad_layers, net, (r.cx, r.cy), max(pad.drill_size) / 2, pad.plated)
                    )
                    if pad.plated is True and net and pad.plated_land_radius > 0:
                        g.plated_ports.append((net, (r.cx, r.cy), pad.plated_land_radius))
                if not net:
                    # NC/mounting pads remain foreign copper on every actual pad
                    # layer. Preserve their exact rectangles and separate track/
                    # via halos, just like assigned pads. Encoding only a via-
                    # expanded absolute mask double-inflates nearby pin escapes
                    # and wrongly seals ordinary SOT-23 pin rows. No routable net
                    # has the empty name, and no access point is created here.
                    for la in pad_layers:
                        g.add_pad(la, "", r)
                    if not pad.through_hole and r.w > 0 and r.h > 0:
                        g.smd_pads.append((side, "", r, land))
                    continue
                for la in pad_layers:
                    g.add_pad(la, net, r)
                if not pad.through_hole and r.w > 0 and r.h > 0:
                    g.smd_pads.append((side, net, r, land))
                # Access cell on the component's side (where a same-side track meets
                # it); a through-hole pad is reachable from either side via its via.
                g.access[(net, comp.ref + "." + name)] = Cell(side, *g.cell_of(r.cx, r.cy))
        # Keep routed copper its edge clearance away from the board outline.
        g.block_edge_inset(clearance + g.via_radius)
        return g

    # -- net access points ---------------------------------------------------

    def net_access(self, graph: BoardGraph, net_name: str) -> List[Cell]:
        """The access cells (one per pad) for ``net_name`` on this grid."""
        try:
            net = graph.net(net_name)
        except KeyError:
            return []
        cells: List[Cell] = []
        for ref, pad in net.pins:
            comp = None
            try:
                comp = graph.component(ref)
            except KeyError:
                continue
            la = self.side_layer(comp.side)
            # Absolute pad centre.
            for name, pnet, r in pad_rects(comp):
                if name == pad and pnet == net_name:
                    cells.append(Cell(la, *self.cell_of(r.cx, r.cy)))
                    break
        return cells
