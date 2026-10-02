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
            if not bad.any():
                break
            b = _widen(b, bad, outside, kw, r, fixed & ~fv)
            if np.array_equal(b, prev):
                break  # nothing can be widened (fixed void or the exterior in the way)
    return b.astype(np.float64), rounds


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
    `outside` is void) or on `keep_void` pixels are not used (first such square on ties)."""
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
                if best is None or cost < best[0]:
                    best = (cost, i0, j0)
        if best is not None:
            _, i0, j0 = best
            out[i0 : i0 + k, j0 : j0 + k] = True
    return out[r : r + ni, r : r + nj]


def pixels_for(length_mm: float, pitch_mm: float) -> int:
    """The minimum length in whole pixels (rounded up, to 1e-6)."""
    if length_mm <= 0:
        return 1
    return int(math.ceil(length_mm / pitch_mm - 1e-6))
