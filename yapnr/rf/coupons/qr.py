"""A small QR Code encoder (ISO/IEC 18004): byte mode, error correction level M, versions 1-6,
the mask chosen by the standard's penalty rules. It draws the tag stick's QR (Order 0 design §6)
without a new dependency; the unit test checks it against an independent encoder's module grid.

`matrix(data)` returns the module grid (True = dark) without the quiet zone.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

# (total codewords, EC codewords per block, [data codewords per block]) for level M
_BLOCKS_M = {
    1: (26, 10, [16]),
    2: (44, 16, [28]),
    3: (70, 26, [44]),
    4: (100, 18, [32, 32]),
    5: (134, 24, [43, 43]),
    6: (172, 16, [27, 27, 27, 27]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_LEVEL_BITS_M = 0b00

# --- GF(256) and Reed-Solomon (primitive polynomial 0x11d) --------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(n: int) -> List[int]:
    g = [1]
    for i in range(n):
        out = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            out[j] ^= c
            out[j + 1] ^= _mul(c, _EXP[i])
        g = out
    return g


def rs_ecc(data: Sequence[int], n: int) -> List[int]:
    """The n Reed-Solomon error-correction codewords of `data`."""
    g = _generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(g)):
                rem[i + j] ^= _mul(g[j], c)
    return rem[len(data) :]


# --- data codewords ---------------------------------------------------------------------------


def _codewords(data: bytes, version: int) -> List[int]:
    total, ec, blocks = _BLOCKS_M[version]
    cap = sum(blocks)
    bits: List[int] = []

    def put(v: int, n: int):
        bits.extend((v >> (n - 1 - k)) & 1 for k in range(n))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    cw = [int("".join(map(str, bits[k : k + 8])), 2) for k in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    k = 0
    while len(cw) < cap:
        cw.append(pad[k % 2])
        k += 1
    # split into blocks, add EC, interleave
    dblocks, eblocks, pos = [], [], 0
    for n in blocks:
        d = cw[pos : pos + n]
        pos += n
        dblocks.append(d)
        eblocks.append(rs_ecc(d, ec))
    out = []
    for i in range(max(blocks)):
        out += [b[i] for b in dblocks if i < len(b)]
    for i in range(ec):
        out += [b[i] for b in eblocks]
    assert len(out) == total
    return out


def capacity(version: int) -> int:
    """Bytes that fit in byte mode at level M."""
    return sum(_BLOCKS_M[version][2]) - 2


# --- the symbol -------------------------------------------------------------------------------


def _format_bits(mask: int) -> int:
    d = (_LEVEL_BITS_M << 3) | mask
    v = d << 10
    g = 0b10100110111
    for i in range(14, 9, -1):
        if v & (1 << i):
            v ^= g << (i - 10)
    return ((d << 10) | v) ^ 0b101010000010010


def _base(version: int):
    n = 17 + 4 * version
    m: List[List[Optional[bool]]] = [[None] * n for _ in range(n)]

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < n and 0 <= cc < n:
                    inside = 0 <= r <= 6 and 0 <= c <= 6
                    ring = inside and (r in (0, 6) or c in (0, 6))
                    core = 2 <= r <= 4 and 2 <= c <= 4
                    m[rr][cc] = bool(ring or core)

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    for i in range(8, n - 8):  # timing patterns
        m[6][i] = i % 2 == 0
        m[i][6] = i % 2 == 0
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            if m[r][c] is not None:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    m[n - 8][8] = True  # the dark module
    reserved = [[v is not None for v in row] for row in m]
    for i in range(9):  # format areas
        reserved[8][i] = reserved[i][8] = True
    for i in range(8):
        reserved[8][n - 1 - i] = reserved[n - 1 - i][8] = True
    return m, reserved


def _place(m, reserved, cw: List[int]):
    n = len(m)
    bits = [(b >> (7 - k)) & 1 for b in cw for k in range(8)]
    idx = 0
    up = True
    c = n - 1
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                if not reserved[r][cc]:
                    m[r][cc] = bool(bits[idx]) if idx < len(bits) else False
                    idx += 1
        up = not up
        c -= 2


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply(m, reserved, mask: int):
    n = len(m)
    out = [row[:] for row in m]
    f = _MASKS[mask]
    for r in range(n):
        for c in range(n):
            if not reserved[r][c] and f(r, c):
                out[r][c] = not out[r][c]
    bits = _format_bits(mask)
    fb = [(bits >> i) & 1 for i in range(15)]  # bit 0 is the least significant
    # around the top-left finder (bits 14..0 in the standard's order)
    coords_a = [
        (8, 0),
        (8, 1),
        (8, 2),
        (8, 3),
        (8, 4),
        (8, 5),
        (8, 7),
        (8, 8),
        (7, 8),
        (5, 8),
        (4, 8),
        (3, 8),
        (2, 8),
        (1, 8),
        (0, 8),
    ]
    coords_b = [
        (n - 1, 8),
        (n - 2, 8),
        (n - 3, 8),
        (n - 4, 8),
        (n - 5, 8),
        (n - 6, 8),
        (n - 7, 8),
        (8, n - 8),
        (8, n - 7),
        (8, n - 6),
        (8, n - 5),
        (8, n - 4),
        (8, n - 3),
        (8, n - 2),
        (8, n - 1),
    ]
    for k in range(15):
        bit = bool(fb[14 - k])
        r, c = coords_a[k]
        out[r][c] = bit
        r, c = coords_b[k]
        out[r][c] = bit
    return out


def _penalty(m) -> int:
    n = len(m)
    score = 0
    for lines in (m, [list(col) for col in zip(*m)]):
        for row in lines:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            s = "".join("1" if v else "0" for v in row)
            for pat in ("10111010000", "00001011101"):
                start = s.find(pat)
                while start != -1:
                    score += 40
                    start = s.find(pat, start + 1)
    for r in range(n - 1):
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(1 for v in row if v) for row in m)
    k = abs(dark * 20 - n * n * 10) // (n * n)
    score += 10 * k
    return score


def matrix(data, version: Optional[int] = None, mask: Optional[int] = None) -> List[List[bool]]:
    """The module grid of `data` (str as UTF-8, or bytes), level M, byte mode: the smallest
    version that holds it (or `version`), the mask with the lowest penalty (or `mask`)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if version is None:
        fits = [v for v in sorted(_BLOCKS_M) if capacity(v) >= len(data)]
        if not fits:
            raise ValueError(f"{len(data)} bytes do not fit version 6-M ({capacity(6)} bytes)")
        version = fits[0]
    if capacity(version) < len(data):
        raise ValueError(f"{len(data)} bytes do not fit version {version}-M")
    base, reserved = _base(version)
    _place(base, reserved, _codewords(data, version))
    masks = [mask] if mask is not None else range(8)
    best: Optional[Tuple[int, int, List[List[bool]]]] = None
    for k in masks:
        cand = _apply(base, reserved, k)
        p = _penalty(cand)
        if best is None or p < best[0]:
            best = (p, k, cand)
    return [[bool(v) for v in row] for row in best[2]]
