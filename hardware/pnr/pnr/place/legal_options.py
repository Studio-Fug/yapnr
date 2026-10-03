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
"""

from __future__ import annotations

from typing import Optional, Tuple

from .geometry import courtyard_rect

Box = Tuple[float, float, float, float]


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
    return out


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
