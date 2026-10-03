"""Dense search fields for the packed and native maze kernels.

The reference A\\* (:func:`pnr.route.detail.maze._astar`) asks the grid, cell by
cell, whether a net may enter a cell or drop a via there, and prices every cell
from a window of the occupancy, history and rip-up dictionaries. A *field* holds
the same answers for one net and one pricing state as flat arrays over the
``[layer, j, i]`` grid, built once per routed net with numpy and shared by every
search of that net's tree:

* ``ok``: the cell is passable for the net (in bounds, not an obstacle, not a
  foreign pad) and outside the search's blocked footprint;
* ``price`` / ``via_price``: the reference cell price, ``(1 + history) * (1 +
  present * occupancy) + soft`` over the same windows and in the same arithmetic
  order, so every double is identical;
* ``col``: a through-via may drop in this column (every layer clear and
  via-passable, and, unless the column lands in an own-net plated pad, no raw
  blocked cell within the via keep-out);
* ``plated``: the column lands in an own-net plated pad (no new drill);
* ``hole``: the drill-spacing verdict against source drills and escape vias.

Drill spacing against the vias of the search's own path stays path-dependent and
is evaluated by the kernels with an offset stencil; every other predicate is a
lookup. Nothing here is a clearance shortcut: the predicates are the grid's, and
the parity tests compare whole searches and whole routes with the reference.

The incremental tables (:class:`DenseCounts`, :class:`DenseOwners`) keep the
router's occupancy, history and committed-owner dictionaries mirrored in arrays
as they change, so a field never re-reads a dictionary.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Optional

import numpy as np

from .cost_field import neighbourhood_max
from .grid import Cell, RouteGrid

# Offsets whose distance sits this close to a drill threshold make the stencil
# position-dependent at the last bit; such a grid falls back to the reference.
_STENCIL_MARGIN = 1e-9

# What the fields reproduce, as it was when they were last checked against it:
# the source of the grid predicates and of the reference search (whitespace
# folded; :func:`model_sources`), and the grid attributes the fields know. A grid
# whose predicates, search or state stray from this is routed by the reference
# kernel (correct, slower; one warning per process), and
# ``tests/test_dense_maze.py`` (GridModelGuardTest) fails until the fields and the
# kernels are ported to the change, checked for parity, and this updated.
MODELLED_SOURCES = {
    "RouteGrid.in_bounds": "82de6984f75cd003",
    "RouteGrid.passable": "0e9a97df280cc3c4",
    "RouteGrid.via_passable": "24c4f5a0141c8844",
    "RouteGrid.plated_transition": "c1d3b5a6d337614d",
    "RouteGrid.hole_site_clear": "4c3d077575fe0373",
    "maze._astar_reference": "e90464d2042c802a",
}
MODELLED_ATTRIBUTES = frozenset(
    (
        "_pth_keepouts _smd_index _wide_seen _wide_specs _wide_tables access blocked "
        "clearance component_pth_min_drill drilled_pads escape_segments escape_vias "
        "height hole_clearance in_pad layers net_clearances net_widths nlayers "
        "npth_hole_gap nx ny pad_net pad_rectangles pitch plated_ports "
        "protected_escape_access pth_hole_gap routing_track_halos routing_via_keepout "
        "smd_pads smd_via_blocked source_drill_plated source_drills track_width "
        "via_blocked via_drill_radius via_halo via_hole_gap via_radius via_spacing "
        "via_to_smd_pad wide_pad_net width"
    ).split()
)
_PREDICATES = ("passable", "via_passable", "plated_transition", "hole_site_clear")
_STALE: Optional[list] = None
_WARNED: set = set()


def model_sources() -> Dict[str, Optional[str]]:
    """Fingerprints of the code the fields reproduce (None: no source to read)."""
    import hashlib
    import inspect

    from . import maze

    out = {}
    for name in MODELLED_SOURCES:
        owner, attr = name.split(".")
        obj = getattr(RouteGrid if owner == "RouteGrid" else maze, attr)
        try:
            text = " ".join(inspect.getsource(obj).split())
        except (OSError, TypeError):
            out[name] = None
            continue
        out[name] = hashlib.sha256(text.encode()).hexdigest()[:16]
    return out


def _stale_sources() -> list:
    global _STALE
    if _STALE is None:
        _STALE = sorted(
            name
            for name, key in model_sources().items()
            if key is not None and key != MODELLED_SOURCES[name]
        )
    return _STALE


def _neutral(value) -> bool:
    if value is None or value is False:
        return True
    return isinstance(value, (dict, list, tuple, set, frozenset, str)) and not value


def unmodelled(grid) -> Optional[str]:
    """Why the fields cannot stand in for ``grid``'s predicates, or None."""
    if not isinstance(grid, RouteGrid):
        return "not a RouteGrid"
    stock = RouteGrid.__dict__
    for name in _PREDICATES:
        if getattr(type(grid), name) is not stock[name] or name in vars(grid):
            return "RouteGrid.%s is overridden" % name
    stale = _stale_sources()
    if stale:
        return "changed since the dense fields were checked against it: " + ", ".join(stale)
    extra = sorted(
        k for k, v in vars(grid).items() if k not in MODELLED_ATTRIBUTES and not _neutral(v)
    )
    if extra:
        return "grid state the dense fields do not model: " + ", ".join(extra)
    return None


def supports(grid) -> bool:
    """Fields model the stock :class:`RouteGrid` predicates, the reference search
    and the grid state they were checked against (:data:`MODELLED_SOURCES`,
    :data:`MODELLED_ATTRIBUTES`) only; otherwise the reference kernel routes, and
    the reason is written to stderr once."""
    reason = unmodelled(grid)
    if reason is None:
        return True
    if isinstance(grid, RouteGrid) and reason not in _WARNED:
        import sys

        _WARNED.add(reason)
        sys.stderr.write("dense maze fields: reference kernel instead (%s)\n" % reason)
    return False


_STENCILS: Dict[tuple, object] = {}


def drill_stencil(grid):
    """``(radius, mask)``: column offsets at which two new grid-centre drills
    violate the via spacing (``hole_site_clear``'s site test), or None when an
    offset sits on the threshold to the last bit (the grid then routes with the
    reference kernel). Depends on the pitch and the via spacing only."""
    key = (grid.pitch, grid.via_spacing)
    cached = _STENCILS.get(key)
    if cached is None:
        spacing = grid.via_spacing - 1e-7
        radius = max(0, int(math.ceil(grid.via_spacing / grid.pitch)) + 1)
        mask = np.zeros((2 * radius + 1, 2 * radius + 1), dtype=np.uint8)
        exact = True
        origin = grid.center_of(radius, radius)
        for dj in range(-radius, radius + 1):
            for di in range(-radius, radius + 1):
                d = math.dist(origin, grid.center_of(radius + di, radius + dj))
                if d < 1e-7:
                    continue
                if abs(d - spacing) < _STENCIL_MARGIN or abs(d - 1e-7) < _STENCIL_MARGIN:
                    exact = False
                if d < spacing:
                    mask[dj + radius, di + radius] = 1
        if mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any():
            exact = False
        cached = _STENCILS[key] = (radius, mask) if exact else False
    return cached or None


def _inside(grid, layer, i, j):
    return 0 <= layer < grid.nlayers and 0 <= i < grid.nx and 0 <= j < grid.ny


class DenseCounts(defaultdict):
    """``defaultdict`` keyed by grid :class:`Cell` whose values are mirrored into
    ``array`` (``[layer, j, i]``, float64). ``exact`` turns False if a key falls
    outside the grid, and the array is then not used."""

    def __init__(self, factory, grid):
        super().__init__(factory)
        self.array = np.zeros((grid.nlayers, grid.ny, grid.nx), dtype=np.float64)
        self.exact = True

    def __setitem__(self, cell, value):
        dict.__setitem__(self, cell, value)
        layer, i, j = cell.layer, cell.i, cell.j
        array = self.array
        if 0 <= layer < array.shape[0] and 0 <= j < array.shape[1] and 0 <= i < array.shape[2]:
            array[layer, j, i] = value
        else:
            self.exact = False

    def __delitem__(self, cell):
        dict.__delitem__(self, cell)
        layer, i, j = cell.layer, cell.i, cell.j
        array = self.array
        if 0 <= layer < array.shape[0] and 0 <= j < array.shape[1] and 0 <= i < array.shape[2]:
            array[layer, j, i] = 0.0

    def __reduce__(self):
        # Workers and copies see an ordinary dictionary.
        return (defaultdict, (self.default_factory, dict(self)))


class DenseOwners(dict):
    """``cell -> net`` dictionary mirrored into an owner-id array (-1: free)."""

    def __init__(self, grid):
        super().__init__()
        self.array = np.full((grid.nlayers, grid.ny, grid.nx), -1, dtype=np.int32)
        self.ids: Dict[str, int] = {}
        self.exact = True

    def __setitem__(self, cell, net):
        dict.__setitem__(self, cell, net)
        layer, i, j = cell.layer, cell.i, cell.j
        array = self.array
        if 0 <= layer < array.shape[0] and 0 <= j < array.shape[1] and 0 <= i < array.shape[2]:
            ids = self.ids
            index = ids.get(net)
            if index is None:
                index = ids[net] = len(ids)
            array[layer, j, i] = index
        else:
            self.exact = False

    def __delitem__(self, cell):
        dict.__delitem__(self, cell)
        layer, i, j = cell.layer, cell.i, cell.j
        array = self.array
        if 0 <= layer < array.shape[0] and 0 <= j < array.shape[1] and 0 <= i < array.shape[2]:
            array[layer, j, i] = -1

    def __reduce__(self):
        return (dict, (dict(self),))


class SoftOwners:
    """The rip-up pass's crossing price: ``penalty`` on every committed cell
    owned by a net other than ``net``. Equivalent to the dictionary
    ``{c: penalty for c, o in owners.items() if o != net}`` (:meth:`as_dict`),
    without building it."""

    def __init__(self, owners: DenseOwners, net: str, penalty: float):
        self.owners = owners
        self.net = net
        self.penalty = penalty

    def as_dict(self):
        net, penalty = self.net, self.penalty
        return {c: penalty for c, o in self.owners.items() if o != net}

    def array(self, grid):
        owners = self.owners
        if not owners.exact:
            return _array_from(self.as_dict(), grid)
        own = owners.array
        foreign = own >= 0
        index = owners.ids.get(self.net)
        if index is not None:
            foreign &= own != index
        return np.where(foreign, self.penalty, 0.0)


def plain_soft(soft):
    """A soft-price argument as the reference kernel's dictionary."""
    return soft.as_dict() if isinstance(soft, SoftOwners) else soft


def _array_from(mapping, grid):
    out = np.zeros((grid.nlayers, grid.ny, grid.nx), dtype=np.float64)
    if mapping:
        for c, value in mapping.items():
            if _inside(grid, c.layer, c.i, c.j):
                out[c.layer, c.j, c.i] = value
    return out


def _values(mapping, grid):
    """Array of a (possibly mirrored) occupancy/history/soft mapping, or None
    when it holds nothing (all prices then read zero)."""
    if mapping is None:
        return None
    if isinstance(mapping, SoftOwners):
        return mapping.array(grid)
    if isinstance(mapping, DenseCounts) and mapping.exact:
        return mapping.array if mapping else None
    if not mapping:
        return None
    return _array_from(mapping, grid)


class GridStatic:
    """Net-independent and per-net static predicates of one grid, valid while the
    grid is not modified (one :func:`pnr.route.detail.maze.route` call)."""

    def __init__(self, grid: RouteGrid):
        self.grid = grid
        shape = (grid.nlayers, grid.ny, grid.nx)
        self.names: Dict[str, int] = {}
        self.pad = self._owners(grid.pad_net, shape)
        self.halo = self._owners(grid.via_halo, shape)
        self.blocked = np.asarray(grid.blocked, dtype=bool)
        via = ~np.asarray(grid.via_blocked, dtype=bool)
        if grid.smd_via_blocked is not None:
            via &= ~np.asarray(grid.smd_via_blocked, dtype=bool)[None, :, :]
        self.via_free = via
        self._nets: Dict[str, tuple] = {}
        self._wide: Dict[int, np.ndarray] = {}
        self._hole = None

    def _owners(self, table, shape):
        out = np.full(shape, -1, dtype=np.int32)
        names = self.names
        for (layer, i, j), owner in table.items():
            if 0 <= layer < shape[0] and 0 <= j < shape[1] and 0 <= i < shape[2]:
                index = names.get(owner)
                if index is None:
                    index = names[owner] = len(names)
                out[layer, j, i] = index
        return out

    def net(self, net):
        """``(passable, via_passable, plated)`` arrays for ``net``."""
        cached = self._nets.get(net)
        if cached is None:
            # RouteGrid.passable: a wide net also keeps its own-width pad halos
            # (one table per width, shared by the nets of that width).
            table = self.grid.wide_pad_net.get(net)
            wide = None
            if table is not None:
                wide = self._wide.get(id(table))
                if wide is None:
                    wide = self._wide[id(table)] = self._owners(table, self.pad.shape)
            index = self.names.get(net, -2)
            own_pad = (self.pad == -1) | (self.pad == index)
            passable = ~self.blocked & own_pad
            if wide is not None:
                passable &= (wide == -1) | (wide == index)
            via = self.via_free & own_pad & ((self.halo == -1) | (self.halo == index))
            cached = self._nets[net] = (passable, via, self._plated(net))
        return cached

    def _plated(self, net):
        grid = self.grid
        out = np.zeros((grid.ny, grid.nx), dtype=bool)
        width = grid.net_widths.get(net, grid.track_width)
        for owner, centre, radius in grid.plated_ports:
            if owner != net:
                continue
            for i, j in _columns_near(grid, centre, radius + grid.pitch):
                if out[j, i]:
                    continue
                # grid.plated_transition's test, evaluated exactly per column.
                if math.dist(grid.center_of(i, j), centre) + width / 2 <= radius - 1e-7:
                    out[j, i] = True
        return out

    def hole(self):
        """Columns whose drill clears every source drill and escape via."""
        if self._hole is None:
            grid = self.grid
            out = np.ones((grid.ny, grid.nx), dtype=bool)
            gaps = [grid.hole_clearance]
            gaps += [g for g in (grid.pth_hole_gap, grid.npth_hole_gap, grid.via_hole_gap) if g]
            reach = max(gaps) + grid.via_drill_radius
            sites = [(p, r + reach) for p, r in grid.source_drills]
            sites += [(xy, grid.via_spacing) for _, xy in grid.escape_vias]
            seen = set()
            for point, distance in sites:
                for i, j in _columns_near(grid, point, distance + grid.pitch):
                    if (i, j) in seen:
                        continue
                    seen.add((i, j))
                    out[j, i] = grid.hole_site_clear(grid.center_of(i, j), ())
            self._hole = out
        return self._hole

    def stencil(self):
        """The grid's :func:`drill_stencil`."""
        return drill_stencil(self.grid)


def _columns_near(grid, point, distance):
    x, y = point
    i0 = max(0, int(math.floor((x - distance) / grid.pitch)) - 1)
    i1 = min(grid.nx - 1, int(math.ceil((x + distance) / grid.pitch)) + 1)
    j0 = max(0, int(math.floor((y - distance) / grid.pitch)) - 1)
    j1 = min(grid.ny - 1, int(math.ceil((y + distance) / grid.pitch)) + 1)
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            yield i, j


def _dilate(mask, radius):
    """Square dilation per layer, clipped at the grid edges."""
    if not radius or not mask.any():
        return mask.copy()
    return neighbourhood_max(mask.astype(np.uint8), radius).astype(bool)


class Field:
    """One net's search field (see the module docstring)."""

    __slots__ = (
        "grid",
        "net",
        "nx",
        "ny",
        "nlayers",
        "ok",
        "price",
        "via_price",
        "col",
        "plated",
        "hole",
        "stencil",
        "static",
        "corner",
        "diag",
        "_lists",
    )

    def tree_hole(self, drill_sites):
        """``hole`` with the spacing to ``drill_sites`` (the tree's new vias)
        applied exactly, as :meth:`RouteGrid.hole_site_clear` does."""
        if not drill_sites:
            return self.hole
        grid = self.grid
        out = self.hole.copy()
        spacing = grid.via_spacing - 1e-7
        for site in drill_sites:
            for i, j in _columns_near(grid, site, grid.via_spacing + grid.pitch):
                if out[j, i]:
                    d = math.dist(grid.center_of(i, j), site)
                    if not (d < 1e-7 or d >= spacing):
                        out[j, i] = False
        return out

    def lists(self):
        """Python lists of the field (the packed kernel's flat lookups)."""
        if self._lists is None:
            ok = self.ok.tolist()
            self._lists = (
                ok,
                self.price.tolist(),
                self.via_price.tolist(),
                self.col.tolist(),
                self.plated.tolist(),
                ok if self.corner is self.ok else self.corner.tolist(),
                None if self.diag is None else self.diag.tolist(),
            )
        return self._lists


def build_field(
    grid,
    net,
    occ,
    history,
    pres_fac,
    blocked=None,
    soft=None,
    static: Optional[GridStatic] = None,
):
    """The search field of ``net`` for one pricing state, or None when the grid
    or the inputs fall outside the dense model (the caller then searches with
    the reference kernel)."""
    if static is None:
        # The cheap verdicts first: an unsupported grid builds no static tables.
        if not supports(grid) or drill_stencil(grid) is None:
            return None
        static = GridStatic(grid)
    stencil = static.stencil()
    if stencil is None:
        return None
    nl, ny, nx = grid.nlayers, grid.ny, grid.nx
    track_halo = getattr(grid, "routing_track_halos", {}).get(net, 0)
    via_halo = getattr(grid, "routing_via_keepout", 0)
    passable, via_ok, plated = static.net(net)

    # The search's blocked footprint: raw cells plus the net's track halo.
    raw = np.zeros((nl, ny, nx), dtype=bool)
    outside = []
    if blocked:
        mask = getattr(blocked, "dense_mask", None)
        if mask is not None:
            raw = mask()
        else:
            for c in blocked:
                layer, i, j = c.layer, c.i, c.j
                if not 0 <= layer < nl:
                    continue
                if 0 <= i < nx and 0 <= j < ny:
                    raw[layer, j, i] = True
                else:
                    outside.append(c)
    block = _dilate(raw, track_halo)
    if outside and track_halo:
        for c in outside:
            for di in range(-track_halo, track_halo + 1):
                for dj in range(-track_halo, track_halo + 1):
                    i, j = c.i + di, c.j + dj
                    if 0 <= i < nx and 0 <= j < ny:
                        block[c.layer, j, i] = True
    ok = passable & ~block

    # Through-via columns: every layer clear and via-passable; outside an own
    # plated pad also no raw blocked cell within the via keep-out.
    column = np.all(via_ok & ~block, axis=0)
    near = raw.any(axis=0)
    if via_halo:
        near = _dilate(near[None], via_halo)[0]
    for c in outside:
        for di in range(-via_halo, via_halo + 1):
            for dj in range(-via_halo, via_halo + 1):
                i, j = c.i - di, c.j - dj
                if 0 <= i < nx and 0 <= j < ny:
                    near[j, i] = True
    column &= plated | ~near

    # Prices, in the reference's arithmetic order.
    counts = _values(occ, grid)
    hist = _values(history, grid)
    penalty = _values(soft, grid)
    if counts is None and hist is None and penalty is None:
        price = np.ones(nl * ny * nx)
        via_price = np.ones(ny * nx)
    else:
        zero = None

        def window(values, radius, via):
            nonlocal zero
            if values is None:
                if zero is None:
                    zero = np.zeros((nl, ny, nx))
                values = zero
            if via:
                return neighbourhood_max(values.max(axis=0), radius)
            return neighbourhood_max(values, radius)

        def combine(via):
            radius = max(track_halo, via_halo) if via else track_halo
            o = np.maximum(window(counts, radius, via), 0.0)
            h = window(hist, radius, via)
            s = window(penalty, radius, via)
            return ((1.0 + h) * (1.0 + pres_fac * o) + s).reshape(-1)

        price = combine(False)
        via_price = combine(True)

    return _field(static, net, ok, price, via_price, column, plated, stencil)


def _field(static, net, ok, price, via_price, column, plated, stencil, corner=None, diag=None):
    """A field. A 45° step needs both corner cells ``corner`` (by default the
    same as ``ok``) and, when ``diag`` is given, its 2x2 block (indexed by the
    block's lower-left cell) ``diag``."""
    grid = static.grid
    field = Field()
    field.grid = grid
    field.net = net
    field.nx, field.ny, field.nlayers = grid.nx, grid.ny, grid.nlayers
    field.ok = np.ascontiguousarray(ok.reshape(-1))
    field.price = np.ascontiguousarray(price, dtype=np.float64).reshape(-1)
    field.via_price = np.ascontiguousarray(via_price, dtype=np.float64).reshape(-1)
    field.col = np.ascontiguousarray(column.reshape(-1))
    field.plated = np.ascontiguousarray(plated.reshape(-1))
    field.hole = static.hole()
    field.stencil = stencil
    field.static = static
    field.corner = field.ok if corner is None else np.ascontiguousarray(corner.reshape(-1))
    field.diag = None if diag is None else np.ascontiguousarray(diag.reshape(-1))
    field._lists = None
    return field


def build_exact_field(
    static: GridStatic,
    net,
    *,
    track_count=None,
    via_count=None,
    history=None,
    pres_fac=0.0,
    track_block=None,
    via_block=None,
    diag_block=None,
    track_soft=None,
    via_soft=None,
):
    """A search field under the exact separation model (:mod:`.exact_route`).

    The inputs are already exact for this net's core kinds, so nothing is
    dilated here: ``track_count``/``track_block``/``track_soft`` (``[L, ny,
    nx]``) say how many other nets' separation zones, whether any committed
    zone, and at what crossing price, cover a cell for this net's track cores;
    ``via_count`` (``[L, ny, nx]``) and ``via_block``/``via_soft`` (``[ny,
    nx]``, every layer folded) do the same for a through-via core, and
    ``diag_block`` (``[L, ny, nx]`` by a 2x2 block's lower-left cell) for the
    centre of a 45° step. A 45° step's corner cells only need to be passable
    (pads, obstacles): the separation is judged at its centre. ``history`` is
    the negotiated history per cell. Prices keep the reference form
    ``(1 + history) * (1 + present * count) + soft``; a via is priced over
    every layer of its column. Pads, plated pads, drills and escape vias are
    the grid's, as for :func:`build_field`. None when the grid is outside the
    dense model.
    """
    stencil = static.stencil()
    if stencil is None:
        return None
    passable, via_ok, plated = static.net(net)
    if track_block is None:
        ok = passable
        column = np.all(via_ok, axis=0)
    else:
        ok = passable & ~track_block
        column = np.all(via_ok & ~track_block, axis=0)
    if via_block is not None:
        column &= plated | ~via_block
    grid = static.grid
    shape = (grid.nlayers, grid.ny, grid.nx)
    hist = np.zeros(shape) if history is None else history
    count = 0.0 if track_count is None else track_count
    price = (1.0 + hist) * (1.0 + pres_fac * count)
    if track_soft is not None:
        price = price + track_soft
    via_hist = hist.max(axis=0)
    via_occ = 0.0 if via_count is None else via_count.max(axis=0)
    via_price = (1.0 + via_hist) * (1.0 + pres_fac * via_occ)
    if via_soft is not None:
        via_price = via_price + via_soft
    diag = None if diag_block is None else ~diag_block
    return _field(
        static, net, ok, price, via_price, column, plated, stencil, corner=passable, diag=diag
    )


class DenseSession:
    """Static predicates shared by every field of one routing call (``static``
    None: the grid is outside the dense model, the reference kernel routes)."""

    def __init__(self, grid):
        self.grid = grid
        self.static = (
            GridStatic(grid) if supports(grid) and drill_stencil(grid) is not None else None
        )

    def field(self, net, occ, history, pres_fac, blocked=None, soft=None):
        if self.static is None:
            return None
        return build_field(
            self.grid, net, occ, history, pres_fac, blocked, soft, static=self.static
        )


def keys_of(field, cells):
    """Flat keys of in-grid ``cells`` that are ``ok`` for the field."""
    nx, ny, nl = field.nx, field.ny, field.nlayers
    plane = nx * ny
    ok = field.ok
    out = []
    for c in cells:
        if 0 <= c.layer < nl and 0 <= c.i < nx and 0 <= c.j < ny:
            key = c.layer * plane + c.j * nx + c.i
            if ok[key]:
                out.append(key)
    return out


def cell_of_key(field, key):
    la, ij = divmod(key, field.nx * field.ny)
    j, i = divmod(ij, field.nx)
    return Cell(la, i, j)
