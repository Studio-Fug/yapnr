"""Width and space repair of a binary design on its pixel grid, before export (design §7.6).

The length-scale constraints (Zhou et al.) act on the skeletons of the filtered field. With a
filter radius of two pixels, the field is not smooth on the pixel scale and the constraints can
read as met while the binary design keeps one-pixel holes or one-pixel nubs (the divider had
four). `repair` removes them mechanically:

- **width:** a morphological opening of the copper with a k × k square (k = the minimum width
  in pixels): copper that no all-copper k × k square covers is removed;
- **space:** the same opening of the void with the minimum space;
- **corners:** a 2 × 2 block whose copper touches only diagonally ([[1, 0], [0, 1]]) gets its
  two void pixels filled (a corner contact is a zero-width neck and a zero-width gap at once);

with the fixed pixels (port pads, fixed regions) set back after each pass, and outside the
window the exterior ring's values (the feeds are copper, the rest void), until nothing changes.

- **conflicts:** a fixed point of the round is not yet a fixed point of each pass. A copper
  pixel can be too narrow and, removed, leave too narrow a gap: a one-pixel bridge between two
  blocks offset diagonally by a pixel (the opening removes it, the space pass puts it back, and
  the round ends where it began; round 1's Wilkinson footprint kept two such 0.3 mm necks).
  Such a pixel is widened instead: the k × k square through it with the fewest void pixels
  (none of them fixed void or outside the window) becomes copper, which keeps the connection
  the design made, and the rounds continue.

- **diagonal necks and gaps** (a two-pixel rule): the copper is exported pixel for pixel
  (`contour`), and a union of k × k squares can still narrow to √2 pixels where two of its void
  pixels face each other diagonally across one copper pixel ([[0, 1, ·], [1, 1, 1], [·, 1, 0]])
  or to one pixel where they face each other across a pixel edge (void pixels offset by
  (2, 1)); a gap of void narrows the same way between copper pixels. A neck is widened (the
  k × k square through the facing void pixel with the smaller x, or through the other one when
  that one is fixed void or outside the window, that needs the fewest new copper pixels, as
  for conflicts); a gap is closed (the k × k square through each of its void pixels that needs
  the fewest new copper pixels), as the space opening closes one-pixel gaps and as the dilated
  design of the robust optimization sees them. Ties go to the square nearest the window's
  centre line in y, so a mirror-symmetric design stays symmetric. Round 2's divider, antenna
  and three-channel bank had two to four √2 necks (0.42 mm at 0.3 mm pitch) and the antenna
  two one-pixel gaps between its feed pad and the islands beside it; the chamfered polygons of
  the earlier export hid them from the polygon check (design §24).

The result is exported and then re-simulated by the validator, which compares it with the
optimizer's binary design.
"""

from __future__ import annotations

import math

import numpy as np


def _opening(mask: np.ndarray, outside: np.ndarray, k: int, r: int) -> np.ndarray:
    """Pixels of the window covered by a k × k square that is all True in the extended mask
    (the window inside `outside`, a (ni + 2r, nj + 2r) array, r ≥ k − 1)."""
    ni, nj = mask.shape
    ext = outside.astype(bool).copy()
    ext[r : r + ni, r : r + nj] = mask
    n0, n1 = ext.shape
    full = np.ones((n0 - k + 1, n1 - k + 1), dtype=bool)
    for a in range(k):
        for b in range(k):
            full &= ext[a : a + n0 - k + 1, b : b + n1 - k + 1]
    cover = np.zeros_like(ext)
    for a in range(k):
        for b in range(k):
            cover[a : a + n0 - k + 1, b : b + n1 - k + 1] |= full
    return cover[r : r + ni, r : r + nj] & mask


def _bridge_corners(mask: np.ndarray) -> np.ndarray:
    """Fill the void pixels of every 2 × 2 block whose copper touches only at a corner."""
    m = mask.copy()
    a, b = m[:-1, :-1], m[1:, 1:]
    c, d = m[1:, :-1], m[:-1, 1:]
    diag = a & b & ~c & ~d
    anti = c & d & ~a & ~b
    fill = np.zeros_like(m)
    fill[1:, :-1] |= diag  # c
    fill[:-1, 1:] |= diag  # d
    fill[:-1, :-1] |= anti  # a
    fill[1:, 1:] |= anti  # b
    return m | fill


def repair(
    binary: np.ndarray,
    fixed: np.ndarray,
    fixed_value: np.ndarray,
    ring: np.ndarray,
    ring_width: int,
    min_width_px: int,
    min_space_px: int,
    max_rounds: int = 20,
) -> tuple[np.ndarray, int]:
    """The repaired binary design and the number of rounds it took (see the module doc).

    `ring` is the material grid's exterior ring ((ni + 2r, nj + 2r), r = `ring_width`)."""
    b = np.asarray(binary) > 0.5
    fixed = np.asarray(fixed, dtype=bool)
    fv = np.asarray(fixed_value) > 0.5
    kw, ks = max(1, int(min_width_px)), max(1, int(min_space_px))
    r = max(kw, ks)
    ni, nj = b.shape
    # The exterior: the ring's values, padded with void if the ring is narrower than r.
    ring = np.asarray(ring) > 0.5
    rw = int(ring_width)
    outside = np.zeros((ni + 2 * r, nj + 2 * r), dtype=bool)
    lo = max(0, rw - r)
    src = ring[lo : ring.shape[0] - lo, lo : ring.shape[1] - lo]
    off = r - (rw - lo)
    outside[off : off + src.shape[0], off : off + src.shape[1]] = src
    rounds = 0
    for rounds in range(1, max_rounds + 1):
        prev = b.copy()
        if kw > 1:
            b = _opening(b, outside, kw, r)
            b = np.where(fixed, fv, b)
        if ks > 1:
            b = ~_opening(~b, ~outside, ks, r)
            b = np.where(fixed, fv, b)
        b = _bridge_corners(b)
        b = np.where(fixed, fv, b)
        if np.array_equal(b, prev):
            if kw == 1:
                break
            # The space pass undid the opening (module doc, "conflicts"): widen the copper.
            bad = b & ~_opening(b, outside, kw, r) & ~fixed
            if bad.any():
                b = _widen(b, bad, outside, kw, r, fixed & ~fv)
                if not np.array_equal(b, prev):
                    continue
            # Diagonal necks and gaps that the polygon check flags (module doc).
            if kw == 2 or ks == 2:
                b = _repair_necks(b, outside, r, fixed, fv, kw, ks)
            b = np.where(fixed, fv, b)
            if np.array_equal(b, prev):
                break  # nothing left, or nothing can change (fixed or the exterior in the way)
    return b.astype(np.float64), rounds


def _necks(mask: np.ndarray, outside: np.ndarray, r: int, frozen: np.ndarray) -> list:
    """Candidate √2 and one-pixel necks of the copper and gaps of the void (two-pixel rules):
    [(kind, centre (x, y) in window pixel units, window pixels)]. A neck's pixels are its two
    facing void pixels that may become copper (not `frozen`, fixed void or outside the window),
    the one with the smaller x first: alternatives, one of which `_repair_necks` widens. A gap's
    pixels are its void pixels in the window, all of which it closes. `outside` as in `_opening`
    (r ≥ 2); the patterns are searched over the window and the exterior around it."""
    ni, nj = mask.shape
    ext = outside.astype(bool).copy()
    ext[r : r + ni, r : r + nj] = mask
    n0, n1 = ext.shape
    can = np.zeros_like(ext)  # pixels that may become copper
    can[r : r + ni, r : r + nj] = ~mask & ~frozen[r : r + ni, r : r + nj]
    out = []

    def at(v, di, dj):
        # Anchors (i, j): i in [0, n0 − 2), j in [2, n1 − 2), so every offset used (0..2 along
        # x, −2..2 along y) stays in the array.
        return v[di : di + n0 - 2, 2 + dj : 2 + dj + n1 - 4]

    for cop in (True, False):
        v = ext if cop else ~ext  # the narrow material: copper (a neck) or void (a gap)
        kind = "width" if cop else "space"
        for sy in (1, -1):
            # √2: A = (0, 0), B = (2, 2sy) of the other material across M = (1, sy), with this
            # material on both sides of the diagonal through M.
            cond = (
                ~at(v, 0, 0)
                & ~at(v, 2, 2 * sy)
                & at(v, 1, sy)
                & (at(v, 0, sy) | at(v, 1, 2 * sy))
                & (at(v, 1, 0) | at(v, 2, sy))
            )
            centre = (1.5, sy + 0.5)
            shapes = [((0, 0), (2, 2 * sy), [(1, sy)])]
            # One pixel: A = (0, 0), B = (2, sy) across (1, 0) and (1, sy) (centre on their
            # shared edge); A = (0, 0), B = (1, 2sy) across (0, sy) and (1, sy).
            cond2 = ~at(v, 0, 0) & ~at(v, 2, sy) & at(v, 1, 0) & at(v, 1, sy)
            cond3 = ~at(v, 0, 0) & ~at(v, 1, 2 * sy) & at(v, 0, sy) & at(v, 1, sy)
            shapes += [
                ((0, 0), (2, sy), [(1, 0), (1, sy)]),
                ((0, 0), (1, 2 * sy), [(0, sy), (1, sy)]),
            ]
            centres = [centre, (1.5, 0.5 + 0.5 * sy), (1.0, sy + 0.5)]
            for c, ctr, (a_, b_, mid) in zip((cond, cond2, cond3), centres, shapes):
                if cop:

                    def pick(i, j, a_=a_, b_=b_):
                        # Both facing void pixels that may become copper, A (smaller x) first.
                        pa, pb = (i + a_[0], j + a_[1]), (i + b_[0], j + b_[1])
                        return [q for q in (pa, pb) if can[q]]

                else:

                    def pick(i, j, mid=mid):
                        return [(i + m0, j + m1) for m0, m1 in mid]

                for x, y, pix in _cands(c, ctr, pick, r, ni, nj):
                    out.append((kind, (x, y), pix))
    return out


def _cands(cond, centre, pick, r, ni, nj):
    """(x, y, window pixels) of every anchor where `cond` holds (see `_necks`)."""
    res = []
    for i, j in np.argwhere(cond):
        ai, aj = int(i), int(j) + 2  # extended indices of the anchor pixel
        pix = [(p - r, q - r) for p, q in pick(ai, aj) if r <= p < r + ni and r <= q < r + nj]
        if pix:
            res.append((ai - r + centre[0], aj - r + centre[1], pix))
    return res


def _repair_necks(b, outside, r, fixed, fv, kw, ks) -> np.ndarray:
    """Widen the necks and close the gaps of `_necks` that the polygon width and space check
    of the pixel-exact copper flags (a candidate counts when its centre lies in a flagged
    residue's box grown by half a pixel): the check's opening tolerates a √2 neck between
    blocks thick enough that the disks from both sides overlap in it."""
    from yapnr.rf.export.contour import islands
    from yapnr.rf.export.drc import check_width_space
    from yapnr.rf.export.raster import label

    ni, nj = b.shape
    frozen = np.ones_like(outside)  # fixed void and the exterior stay void
    frozen[r : r + ni, r : r + nj] = fixed & ~fv
    cand = _necks(b, outside, r, frozen)
    if not cand:
        return b
    polys = [isl.polygon for isl in islands(b)]
    viol = check_width_space(polys, (0.0, ni, 0.0, nj), 1.0, kw, ks).violations
    boxes = {"width": [], "space": []}
    for v in viol:
        if v.box_mm is not None:
            x0, x1, y0, y1 = v.box_mm
            boxes[v.kind].append((x0 - 0.5, x1 + 0.5, y0 - 0.5, y1 + 0.5))
    ext = outside.astype(bool).copy()
    ext[r : r + ni, r : r + nj] = b
    lab, _ = label(ext)
    keep_void = np.ones_like(ext)
    keep_void[r : r + ni, r : r + nj] = fixed & ~fv
    neck = np.zeros_like(ext)
    gap = np.zeros_like(b)
    for kind, (x, y), pix in cand:
        if not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, x1, y0, y1 in boxes[kind]):
            continue
        if kind == "space":
            for p, q in pix:
                gap[p, q] = True
            continue
        if kw != 2:
            continue
        # The copper component the neck belongs to: the copper pixels next to its centre.
        ci, cj = int(np.floor(x)) + r, int(np.floor(y)) + r
        own = {int(lab[a, c]) for a in (ci - 1, ci) for c in (cj - 1, cj) if lab[a, c] > 0}
        for p, q in pix:  # A first, then B
            sq = _square(ext, p + r, q + r, kw, keep_void, nj, r)
            if sq is None:
                continue
            i0, j0 = sq
            new = ~ext[i0 : i0 + kw, j0 : j0 + kw]
            if _near_other(lab, own, i0, j0, kw, new, ks):
                continue  # widening here would narrow a gap to other copper
            neck[i0 : i0 + kw, j0 : j0 + kw] = True
            break
    out = b | neck[r : r + ni, r : r + nj]
    if ks == 2 and gap.any():
        out = _widen(out, gap & ~fixed, outside, kw, r, fixed & ~fv)
    return out


def _square(ext, i, j, k, keep_void, nj, r):
    """(i0, j0) of the k × k square through extended pixel (i, j) that needs the fewest new
    copper pixels, none on `keep_void`; ties as in `_widen`; None if every square is blocked."""
    best = None
    n0, n1 = ext.shape
    for a in range(k):
        for c in range(k):
            i0, j0 = i - a, j - c
            if i0 < 0 or j0 < 0 or i0 + k > n0 or j0 + k > n1:
                continue
            need = ~ext[i0 : i0 + k, j0 : j0 + k]
            if (need & keep_void[i0 : i0 + k, j0 : j0 + k]).any():
                continue
            key = (int(need.sum()), abs(2 * (j0 - r) + k - nj), i0)
            if best is None or key < best[0]:
                best = (key, i0, j0)
    return None if best is None else best[1:]


def _near_other(lab, own, i0, j0, k, new, ks) -> bool:
    """Whether a new pixel of the square at (i0, j0) lies within `ks` pixels (Chebyshev) of
    copper of a component other than `own`: the widening would open a gap narrower than the
    minimum space, which the space pass closes, merging the components."""
    n0, n1 = lab.shape
    for a, c in np.argwhere(new):
        p, q = i0 + a, j0 + c
        win = lab[max(0, p - ks) : min(n0, p + ks + 1), max(0, q - ks) : min(n1, q + ks + 1)]
        others = set(np.unique(win[win > 0]).tolist()) - own
        if others:
            return True
    return False


def _widen(
    mask: np.ndarray,
    bad: np.ndarray,
    outside: np.ndarray,
    k: int,
    r: int,
    keep_void: np.ndarray,
) -> np.ndarray:
    """`mask` with, for every pixel of `bad`, the k × k square through it that needs the fewest
    new copper pixels made copper; squares that would need copper outside the window (where
    `outside` is void) or on `keep_void` pixels are not used. On ties the square nearest the
    window's centre line in y wins, then the one with the smallest x: the choice commutes with
    a mirror about that line, so a mirror-symmetric design stays symmetric."""
    ni, nj = mask.shape
    ext = outside.astype(bool).copy()
    ext[r : r + ni, r : r + nj] = mask
    frozen = np.ones_like(ext)  # pixels that may not become copper
    frozen[r : r + ni, r : r + nj] = keep_void
    out = ext.copy()
    for i, j in np.argwhere(bad):
        best = None
        for a in range(k):
            for c in range(k):
                i0, j0 = r + i - a, r + j - c
                sq = ext[i0 : i0 + k, j0 : j0 + k]
                need = ~sq
                if (need & frozen[i0 : i0 + k, j0 : j0 + k]).any():
                    continue
                cost = int(need.sum())
                # Distance of the square's centre from the centre line, in half pixels.
                off = abs(2 * (j0 - r) + k - nj)
                key = (cost, off, i0)
                if best is None or key < best[0]:
                    best = (key, i0, j0)
        if best is not None:
            _, i0, j0 = best
            out[i0 : i0 + k, j0 : j0 + k] = True
    return out[r : r + ni, r : r + nj]


def pixels_for(length_mm: float, pitch_mm: float) -> int:
    """The minimum length in whole pixels (rounded up, to 1e-6)."""
    if length_mm <= 0:
        return 1
    return int(math.ceil(length_mm / pitch_mm - 1e-6))
