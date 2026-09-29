"""Pure-Python 2-D geometry for the make-room model (KiCad Python 3.9: no numpy)."""
import math


def sub(a, b): return (a[0] - b[0], a[1] - b[1])
def add(a, b): return (a[0] + b[0], a[1] + b[1])
def mul(a, s): return (a[0] * s, a[1] * s)
def dot(a, b): return a[0] * b[0] + a[1] * b[1]
def cross(a, b): return a[0] * b[1] - a[1] * b[0]


def closest_on_seg(p, a, z):
    """``(t, point)``: parameter in [0, 1] and closest point of segment a-z to p."""
    d = sub(z, a)
    length = dot(d, d)
    if length < 1e-18:
        return 0.0, a
    t = max(0.0, min(1.0, dot(sub(p, a), d) / length))
    return t, add(a, mul(d, t))


def seg_intersect(a, b, c, d):
    """Proper crossing of segments a-b and c-d (touching is not a crossing)."""
    def orient(p, q, r): return cross(sub(q, p), sub(r, p))
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def seg_seg(a, b, c, d):
    """``(distance, t on ab, s on cd, point on ab, point on cd)`` of the closest
    points of two segments (distance 0 at a crossing)."""
    if seg_intersect(a, b, c, d):
        r, s = sub(b, a), sub(d, c)
        den = cross(r, s)
        t = cross(sub(c, a), s) / den
        u = cross(sub(c, a), r) / den
        p = add(a, mul(r, t))
        return 0.0, t, u, p, p
    best = None
    for p, pa, pb, flip in ((a, c, d, False), (b, c, d, False), (c, a, b, True), (d, a, b, True)):
        t, q = closest_on_seg(p, pa, pb)
        dist = math.dist(p, q)
        if best is None or dist < best[0]:
            if not flip:
                best = (dist, 0.0 if p is a else 1.0, t, p, q)
            else:
                best = (dist, t, 0.0 if p is c else 1.0, q, p)
    return best


def point_in_poly(p, ring):
    """Even-odd containment of point ``p`` in the closed ring (list of points)."""
    x, y = p
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                inside = not inside
    return inside


def poly_edges(rings):
    for ring in rings:
        n = len(ring)
        for i in range(n):
            yield ring[i], ring[(i + 1) % n]


def crosses_polyline(path, a, z):
    """True when segment a-z properly crosses any segment of ``path``."""
    return any(seg_intersect(a, z, p, q) for p, q in zip(path, path[1:]))
