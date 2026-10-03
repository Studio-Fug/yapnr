"""Opt-in legalizer options: the ``legalize:`` constraint section.

Every option is off unless the constraint file declares it, and then the legalizer
(:func:`pnr.place.legalize.legalize`) calls exactly the code it always did.

``outline: exact``
    The raster the legalizer packs on is ``ceil(W / g) x ceil(H / g)`` cells, so when
    the outline is not a whole number of cells its last row or column reaches past the
    board edge (a 46.3 mm board on the 0.25 mm grid has rows up to 46.5 mm). A slot
    there keeps its courtyard ``clearance / 2`` inside the raster but up to
    ``g - clearance / 2`` outside the board, and the placement's own hard check
    (:func:`pnr.place.metrics.outside_outline`) then refuses the whole start. With
    ``exact`` the slot centre is bounded by :func:`outline_box`, derived from the same
    courtyard rectangle that check tests, and the legalizer asserts that check before it
    returns.

``order: scarcity``
    The legalizer places the parts of hard-group blocks (and aligns) first, choosing the
    next block by its free slots per area, and only then the other parts, so a part
    held only by a narrow hard region (a connector's one window) finds its slots taken
    by unrelated blocks. With ``scarcity`` every part held by a hard region or a hard
    edge band, and in no block, is a block of its own and competes by the same rank.

``lookahead: regions``
    The power-first look-ahead (``starves``: refuse a slot when a greedy trial pack of
    the unplaced hard-limited parts fails) without power-first: the parts that join the
    trial are those held by a hard group, region or edge band whose reach meets the slot
    and that keep at most :data:`SCARCE_SLOTS` free slots, each packed at its own slot
    target inside its edge box and region mask. Up to ``LOOK_AHEAD_TRIES`` slots are
    tried; when every one strands a part the nearest slot is kept, as without the
    look-ahead, and backtracking deals with the stranded part.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .geometry import courtyard_rect

Box = Tuple[float, float, float, float]

# ``lookahead: regions``: a held part is scarce, and joins the trial pack, when a slot
# leaves it at most this many free slots (bounds the look-ahead's cost).
SCARCE_SLOTS = 64


def options(constraints) -> dict:
    """The declared ``legalize:`` options of compiled ``constraints`` ({} when absent)."""
    return dict(getattr(constraints, "legalize", None) or {})


def legalize_kwargs(constraints) -> dict:
    """Keyword arguments of :func:`pnr.place.legalize.legalize` for the declared
    options; empty without a ``legalize:`` section (or with only default values), so
    every other design calls the legalizer exactly as before."""
    opts = options(constraints)
    out = {}
    if opts.get("outline") == "exact":
        out["outline"] = "exact"
    if opts.get("order") == "scarcity":
        out["order"] = "scarcity"
    if opts.get("lookahead") == "regions":
        out["lookahead"] = "regions"
    return out


def region_held(components, rules, bands) -> set:
    """Refs of ``components`` held by a hard region (``rules``, a
    :class:`pnr.place.regions.LegalizeRules` or None) or a hard edge band (``bands``)."""
    from pnr.constraints import Enforcement

    out = set()
    for comp in components:
        if comp.ref in (bands or {}):
            out.add(comp.ref)
        elif rules is not None and any(
            con.enforcement is Enforcement.HARD for con, _ in rules.region_of.get(comp.ref, ())
        ):
            out.add(comp.ref)
    return out


def reaches(comp, slot, limits, box, rules, width: float, height: float) -> bool:
    """Whether ``comp`` could still be placed over ``slot`` (x0, x1, y0, y1, mm): its
    centre bounds (``box``, a centre box or None; each hard group disc in ``limits``; the
    bounding box of its hard polygon regions) grown by a generous half slot (either turn,
    the largest spreading) meet it. A cheap filter: False only when no slot of ``comp``
    can overlap ``slot``, so taking it cannot change ``comp``'s free slots."""
    x0, x1, y0, y1 = 0.0, width, 0.0, height
    if box is not None:
        x0, x1, y0, y1 = max(x0, box[0]), min(x1, box[1]), max(y0, box[2]), min(y1, box[3])
    for ax, ay, radius in limits:
        x0, x1 = max(x0, ax - radius), min(x1, ax + radius)
        y0, y1 = max(y0, ay - radius), min(y1, ay + radius)
    if rules is not None and comp.ref in rules.region_of:
        bounds = rules.static_box(comp, bounds=True)
        if bounds is not None:
            x0, x1 = max(x0, bounds[0]), min(x1, bounds[1])
            y0, y1 = max(y0, bounds[2]), min(y1, bounds[3])
    grow = 0.65 * max(comp.courtyard) + 0.5
    return not (
        x1 + grow < slot[0] or slot[1] < x0 - grow or y1 + grow < slot[2] or slot[3] < y0 - grow
    )


def outline_box(comp, width: float, height: float) -> Box:
    """Centre box (x_lo, x_hi, y_lo, y_hi) of the poses at which ``comp``'s courtyard
    (:func:`pnr.place.geometry.courtyard_rect` at its rotation and side, with any offset
    from the origin it has) lies inside ``[0, width] x [0, height]``: the hard outline
    test, as a bound on the pose."""
    r = courtyard_rect(comp)
    x, y = comp.pos
    return (x - r.left, width - (r.right - x), y - r.bottom, height - (r.top - y))


def intersect(a: Optional[Box], b: Box) -> Box:
    """The intersection of two centre boxes (``a`` None: ``b``)."""
    if a is None:
        return b
    return (max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3]))


def mark_outside(occ, g: float, width: float, height: float) -> None:
    """Mark the raster cells of ``occ`` (rows: y, columns: x, ``g`` mm) that reach past
    the ``width x height`` outline occupied, so no block packed on it leaves the board
    (for the packers that bound no slot centre: :func:`pnr.place.matched.refine_matched`)."""
    ny, nx = occ.shape
    rows = [r for r in range(ny) if (r + 1) * g > height + 1e-9]
    cols = [c for c in range(nx) if (c + 1) * g > width + 1e-9]
    if rows:
        occ[rows[0] :, :] = True
    if cols:
        occ[:, cols[0] :] = True


def outline_offenders(graph, width: float, height: float, refs) -> list:
    """The parts among ``refs`` whose courtyard leaves the outline, by the placement's
    hard check itself (:func:`pnr.place.metrics.outside_outline`)."""
    from .metrics import outside_outline

    keep = set(refs)
    return [ref for ref in outside_outline(graph, width, height) if ref in keep]
