"""The ``yapnr-planar-v1`` document: validation, rings, port geometry and the geometry hash.

A document is plain JSON, in millimetres:

- ``stack.dielectrics``: slabs ``{name, z0, z1, eps_r, tan_d}``, optionally limited to an
  ``outline`` ring (a finite board); a later slab wins where two overlap.
- ``stack.layers``: conductor layers ``{name, z, t, sigma, rough_k, model}``. ``model`` is how a
  solver represents the copper: ``pec``; ``sheet`` (zero thickness at ``z``, surface impedance of
  conductivity ``sigma / rough_k**2`` and thickness ``t``); or ``solid`` (extruded from ``z`` to
  ``z + t``, only for a layer with nothing but air above it). ``rough_k`` is the Hammerstad
  roughness factor at the design frequency, so ``sigma / rough_k**2`` keeps the loss of the rough
  surface (the openEMS models of the radar60 work use the same reduction).
- ``conductors``: ``{layer, net, polygons: [{outer, holes}]}``. A ring is a list of points
  ``[x, y]``; an item ``{"mid": [x, y]}`` between two points makes that edge a circular arc
  through ``mid``, so arcs are exact rather than polygonized.
- ``vias``: barrels ``{at, drill, from, to, net}`` between two layers (or ``zmin``).
- ``ports``: ``{name, kind, net, layer, ref, at, dir, width, z0, excite, face}``. ``at`` is the
  reference plane on the line's centre and ``dir`` the direction the line runs from there into
  the model (``+x``, ``-x``, ``+y``, ``-y``). A ``wave`` port is a face on the domain wall behind
  ``at`` (the solver de-embeds back to ``at``); a ``lumped`` port is a rectangle across the
  dielectric under the line at ``at``. ``ref`` is the layer (or ``zmin``) the line refers to.
- ``domain``: ``box`` ``[x0, x1, y0, y1, z0, z1]`` and ``boundaries`` per face (``xmin`` ...
  ``zmax``): ``abc1``/``abc2`` (first/second-order absorbing), ``pec`` or ``pmc``.
- ``mesh``: sizing hints for the mesher (all optional); ``provenance``: source and generator;
  ``features``: named regions of interest (for example the GND sliver of the radar60 TX1 feed),
  carried for probes and plots.

Stdlib only: the cleaner and the adapters (``clean``, ``adapters``) need shapely, the mesher
gmsh.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = "yapnr-planar-v1"
FACES = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
BOUNDARY_KINDS = ("abc1", "abc2", "pec", "pmc")
LAYER_MODELS = ("pec", "sheet", "solid")
PORT_KINDS = ("wave", "lumped")
DIRS = {"+x": (0, 1), "-x": (0, -1), "+y": (1, 1), "-y": (1, -1)}
# A wave port's face: half its width and its height above the signal layer (mm). 8 strip widths
# by about 7 substrate heights for the 0.2 mm lines on 0.1 mm cores; the walls of the face act
# as PEC in the port's mode solve, so the face has to be several line widths wide [D].
FACE_DEFAULT = {"half_width": 0.8, "height": 0.7}
TOL = 1e-6

Point = Tuple[float, float]


class PlanarError(ValueError):
    """The document is not a valid yapnr-planar-v1 model."""


# --- rings -------------------------------------------------------------------------------------


def ring_edges(ring: Sequence[Any]) -> List[Tuple[str, Point, Optional[Point], Point]]:
    """The closed ring as edges: ``("line", a, None, b)`` or ``("arc", a, mid, b)``."""
    pts: List[Point] = []
    mids: Dict[int, Point] = {}
    for item in ring:
        if isinstance(item, dict):
            if not pts or len(pts) - 1 in mids:
                raise PlanarError("an arc 'mid' must follow a point")
            mids[len(pts) - 1] = (float(item["mid"][0]), float(item["mid"][1]))
        else:
            pts.append((float(item[0]), float(item[1])))
    if len(pts) < 2 or (len(pts) < 3 and not mids):
        raise PlanarError("a ring needs at least three points (or two and an arc)")
    edges = []
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        if i in mids:
            edges.append(("arc", a, mids[i], b))
        else:
            edges.append(("line", a, None, b))
    return edges


def circle_through(a: Point, m: Point, b: Point) -> Tuple[float, float, float]:
    """Centre and radius of the circle through three points."""
    ax, ay = a
    mx, my = m
    bx, by = b
    d = 2.0 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-15:
        raise PlanarError(f"arc points are collinear: {a} {m} {b}")
    ux = (
        (ax * ax + ay * ay) * (my - by)
        + (mx * mx + my * my) * (by - ay)
        + (bx * bx + by * by) * (ay - my)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (bx - mx)
        + (mx * mx + my * my) * (ax - bx)
        + (bx * bx + by * by) * (mx - ax)
    ) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def arc_points(a: Point, m: Point, b: Point, chord: float = 0.005) -> List[Point]:
    """Points along the arc a -> m -> b (a included, b excluded), at most ``chord`` apart."""
    cx, cy, r = circle_through(a, m, b)
    t0 = math.atan2(a[1] - cy, a[0] - cx)
    tm = math.atan2(m[1] - cy, m[0] - cx)
    t1 = math.atan2(b[1] - cy, b[0] - cx)

    # the sweep that passes through m
    ccw = (t1 - t0) % (2 * math.pi)
    mid = (tm - t0) % (2 * math.pi)
    sweep = ccw if mid <= ccw else ccw - 2 * math.pi
    n = max(2, int(math.ceil(abs(sweep) * r / chord)))
    return [
        (cx + r * math.cos(t0 + sweep * k / n), cy + r * math.sin(t0 + sweep * k / n))
        for k in range(n)
    ]


def ring_points(ring: Sequence[Any], chord: float = 0.005) -> List[Point]:
    """The ring as a polygon (arcs sampled at ``chord``), without the closing point."""
    out: List[Point] = []
    for kind, a, m, b in ring_edges(ring):
        if kind == "arc":
            out.extend(arc_points(a, m, b, chord))
        else:
            out.append(a)
    return out


def ring_area(points: Sequence[Point]) -> float:
    """Signed shoelace area (counter-clockwise positive)."""
    s = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        s += x0 * y1 - x1 * y0
    return 0.5 * s


def ring_bounds(ring: Sequence[Any]) -> Tuple[float, float, float, float]:
    pts = ring_points(ring, chord=0.01)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# --- lookups -----------------------------------------------------------------------------------


def layer(doc: Dict[str, Any], name: str) -> Dict[str, Any]:
    for item in doc["stack"]["layers"]:
        if item["name"] == name:
            return item
    raise PlanarError(f"unknown layer {name!r}")


def z_of(doc: Dict[str, Any], name: str) -> float:
    """The z of a layer, or of the domain floor for ``zmin``."""
    if name == "zmin":
        return float(doc["domain"]["box"][4])
    return float(layer(doc, name)["z"])


def sigma_eff(lay: Dict[str, Any]) -> float:
    """Conductivity with the Hammerstad roughness folded in: sigma / K^2."""
    return float(lay["sigma"]) / float(lay.get("rough_k", 1.0)) ** 2


# --- validation --------------------------------------------------------------------------------


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate(doc: Dict[str, Any]) -> List[str]:
    """Every finding; an empty list means the document is valid."""
    errs: List[str] = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    if doc.get("units", "mm") != "mm":
        errs.append("units must be 'mm'")
    dom = doc.get("domain") or {}
    box = dom.get("box")
    if not (isinstance(box, list) and len(box) == 6 and all(_num(v) for v in box)):
        errs.append("domain.box must be [x0, x1, y0, y1, z0, z1]")
        return errs
    x0, x1, y0, y1, z0, z1 = box
    if not (x0 < x1 and y0 < y1 and z0 < z1):
        errs.append("domain.box must have x0 < x1, y0 < y1 and z0 < z1")
    bnd = dom.get("boundaries") or {}
    for face in FACES:
        if bnd.get(face) not in BOUNDARY_KINDS:
            errs.append(f"domain.boundaries.{face} must be one of {BOUNDARY_KINDS}")
    for face in bnd:
        if face not in FACES:
            errs.append(f"domain.boundaries: unknown face {face!r}")
    stack = doc.get("stack") or {}
    names = set()
    for d in stack.get("dielectrics", []):
        n = d.get("name")
        if not n or n in names or n == "air":
            errs.append(f"dielectric name {n!r} missing, repeated or reserved")
        names.add(n)
        if not (_num(d.get("z0")) and _num(d.get("z1")) and d["z0"] < d["z1"]):
            errs.append(f"dielectric {n}: needs z0 < z1")
        elif d["z0"] < z0 - TOL or d["z1"] > z1 + TOL:
            errs.append(f"dielectric {n}: outside the domain in z")
        if not (_num(d.get("eps_r")) and d["eps_r"] >= 1.0):
            errs.append(f"dielectric {n}: eps_r >= 1 required")
        if not (_num(d.get("tan_d", 0.0)) and d.get("tan_d", 0.0) >= 0):
            errs.append(f"dielectric {n}: tan_d >= 0 required")
        if d.get("outline") is not None:
            try:
                ring_edges(d["outline"])
            except (PlanarError, KeyError, TypeError, IndexError) as exc:
                errs.append(f"dielectric {n}: bad outline ({exc})")
    lnames = set()
    for lay in stack.get("layers", []):
        n = lay.get("name")
        if not n or n in lnames or n == "zmin":
            errs.append(f"layer name {n!r} missing, repeated or reserved")
        lnames.add(n)
        if lay.get("model") not in LAYER_MODELS:
            errs.append(f"layer {n}: model must be one of {LAYER_MODELS}")
        if not _num(lay.get("z")) or not (z0 - TOL <= lay["z"] <= z1 + TOL):
            errs.append(f"layer {n}: z missing or outside the domain")
        if not (_num(lay.get("t", 0.0)) and lay.get("t", 0.0) >= 0):
            errs.append(f"layer {n}: t >= 0 required")
        if lay.get("model") in ("sheet", "solid"):
            if not (_num(lay.get("sigma")) and lay["sigma"] > 0):
                errs.append(f"layer {n}: sigma > 0 required for a {lay['model']} layer")
            if not (_num(lay.get("rough_k", 1.0)) and lay.get("rough_k", 1.0) >= 1.0):
                errs.append(f"layer {n}: rough_k >= 1 required")
        if lay.get("model") == "solid":
            if not lay.get("t"):
                errs.append(f"layer {n}: a solid layer needs t > 0")
            for d in stack.get("dielectrics", []):
                if (
                    _num(lay.get("z"))
                    and d["z0"] < lay["z"] + lay.get("t", 0) - TOL
                    and d["z1"] > lay["z"] + TOL
                ):
                    errs.append(f"layer {n}: solid copper would overlap dielectric {d['name']}")
    for k, c in enumerate(doc.get("conductors", [])):
        where = f"conductors[{k}] ({c.get('layer')}/{c.get('net')})"
        if c.get("layer") not in lnames:
            errs.append(f"{where}: unknown layer")
        if not c.get("net"):
            errs.append(f"{where}: net missing")
        for p in c.get("polygons", []):
            try:
                for ring in [p["outer"]] + list(p.get("holes", [])):
                    edges = ring_edges(ring)
                    for _, a, _, b in edges:
                        for q in (a, b):
                            if not (x0 - TOL <= q[0] <= x1 + TOL and y0 - TOL <= q[1] <= y1 + TOL):
                                raise PlanarError(f"point {q} outside the domain")
            except (PlanarError, KeyError, TypeError, IndexError) as exc:
                errs.append(f"{where}: bad polygon ({exc})")
                break
    for k, v in enumerate(doc.get("vias", [])):
        where = f"vias[{k}]"
        if not (isinstance(v.get("at"), list) and len(v["at"]) == 2):
            errs.append(f"{where}: at [x, y] required")
            continue
        if not (_num(v.get("drill")) and v["drill"] > 0):
            errs.append(f"{where}: drill > 0 required")
        for key in ("from", "to"):
            if v.get(key) != "zmin" and v.get(key) not in lnames:
                errs.append(f"{where}: unknown layer {key}={v.get(key)!r}")
        if not v.get("net"):
            errs.append(f"{where}: net missing")
        r = 0.5 * float(v.get("drill") or 0)
        if not (
            x0 + r - TOL <= v["at"][0] <= x1 - r + TOL
            and y0 + r - TOL <= v["at"][1] <= y1 - r + TOL
        ):
            errs.append(f"{where}: barrel at {v['at']} crosses or lies outside the domain wall")
        if v.get("from") in lnames | {"zmin"} and v.get("to") in lnames | {"zmin"}:
            if z_of(doc, v["from"]) >= z_of(doc, v["to"]):
                errs.append(f"{where}: 'from' must be below 'to'")
    pnames = set()
    for k, p in enumerate(doc.get("ports", [])):
        n = p.get("name")
        where = f"ports[{k}] ({n})"
        if not n or n in pnames:
            errs.append(f"{where}: name missing or repeated")
        pnames.add(n)
        if p.get("kind") not in PORT_KINDS:
            errs.append(f"{where}: kind must be one of {PORT_KINDS}")
        if p.get("dir") not in DIRS:
            errs.append(f"{where}: dir must be one of {tuple(DIRS)}")
        if p.get("layer") not in lnames:
            errs.append(f"{where}: unknown signal layer")
        if p.get("ref") != "zmin" and p.get("ref") not in lnames:
            errs.append(f"{where}: unknown reference layer")
        if not (_num(p.get("width")) and p["width"] > 0):
            errs.append(f"{where}: width > 0 required")
        at = p.get("at")
        if not (isinstance(at, list) and len(at) == 2 and all(_num(v) for v in at)):
            errs.append(f"{where}: at [x, y] required")
        elif not (x0 - TOL <= at[0] <= x1 + TOL and y0 - TOL <= at[1] <= y1 + TOL):
            errs.append(f"{where}: at outside the domain")
        if p.get("kind") == "wave" and p.get("dir") in DIRS:
            wall = wall_of(p["dir"])
            if bnd.get(wall) == "pec":
                errs.append(f"{where}: wave port on the {wall} wall, which is PEC")
    if not errs:
        try:
            port_geometry(doc)
        except PlanarError as exc:
            errs.append(str(exc))
    return errs


def check(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The document, or PlanarError listing every finding."""
    errs = validate(doc)
    if errs:
        raise PlanarError("invalid yapnr-planar-v1 model:\n  " + "\n  ".join(errs))
    return doc


# --- ports -------------------------------------------------------------------------------------


def wall_of(direction: str) -> str:
    """The domain face behind a port whose line runs in ``direction`` into the model."""
    axis, sign = DIRS[direction]
    return ("x", "y")[axis] + ("min" if sign > 0 else "max")


def port_geometry(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Face rectangle, de-embedding offset and voltage path of every port, by name.

    A wave port's face lies on the wall behind ``at``: laterally ``face.half_width`` either side
    of the line, cut back at the domain edge and halfway to a neighbouring port on the same wall;
    vertically from the reference layer to ``face.height`` above the signal layer. A lumped
    port's face is the ``width`` x (signal - reference) rectangle across the dielectric at ``at``.
    Coordinates are mm; ``rect`` lists the four corners in order.
    """
    x0, x1, y0, y1, z0, z1 = (float(v) for v in doc["domain"]["box"])
    lim = ((x0, x1), (y0, y1))
    out: Dict[str, Dict[str, Any]] = {}
    walls: Dict[str, List[Dict[str, Any]]] = {}
    for p in doc.get("ports", []):
        axis, sign = DIRS[p["dir"]]
        lat = 1 - axis
        z_sig = z_of(doc, p["layer"])
        z_ref = z_of(doc, p["ref"])
        if z_ref >= z_sig:
            raise PlanarError(f"port {p['name']}: the reference layer must lie below the signal")
        c = float(p["at"][lat])
        g: Dict[str, Any] = dict(kind=p["kind"], axis="xy"[axis], z_sig=z_sig, z_ref=z_ref)
        if p["kind"] == "wave":
            wall = wall_of(p["dir"])
            plane = lim[axis][0] if sign > 0 else lim[axis][1]
            face = dict(FACE_DEFAULT, **(p.get("face") or {}))
            g.update(
                wall=wall,
                plane=plane,
                offset=abs(float(p["at"][axis]) - plane),
                lat=[
                    max(lim[lat][0], c - face["half_width"]),
                    min(lim[lat][1], c + face["half_width"]),
                ],
                z=[z_ref, min(z1, z_sig + face["height"])],
                centre=c,
            )
            walls.setdefault(wall, []).append(dict(name=p["name"], g=g))
        else:
            plane = float(p["at"][axis])
            w = float(p["width"])
            g.update(
                plane=plane, offset=0.0, lat=[c - w / 2, c + w / 2], z=[z_ref, z_sig], centre=c
            )
        out[p["name"]] = g
    for wall, items in walls.items():
        items.sort(key=lambda it: it["g"]["centre"])
        for a, b in zip(items, items[1:]):
            mid = 0.5 * (a["g"]["centre"] + b["g"]["centre"])
            a["g"]["lat"][1] = min(a["g"]["lat"][1], mid)
            b["g"]["lat"][0] = max(b["g"]["lat"][0], mid)
    for p in doc.get("ports", []):
        g = out[p["name"]]
        lo, hi = g["lat"]
        w = float(p["width"])
        if (
            not (lo < g["centre"] - w / 2 - TOL and hi > g["centre"] + w / 2 + TOL)
            and g["kind"] == "wave"
        ):
            raise PlanarError(f"port {p['name']}: the face does not clear the {w} mm line")
        if g["kind"] == "lumped":
            lo, hi = g["lat"]
        pl = g["plane"]
        za, zb = g["z"]
        if g["axis"] == "x":
            g["rect"] = [[pl, lo, za], [pl, hi, za], [pl, hi, zb], [pl, lo, zb]]
            vp = [[pl, g["centre"], g["z_sig"]], [pl, g["centre"], g["z_ref"]]]
        else:
            g["rect"] = [[lo, pl, za], [hi, pl, za], [hi, pl, zb], [lo, pl, zb]]
            vp = [[g["centre"], pl, g["z_sig"]], [g["centre"], pl, g["z_ref"]]]
        g["voltage_path"] = vp  # signal -> ground, as Palace's VoltagePath and lumped Direction
        g["half_width_actual"] = [g["centre"] - lo, hi - g["centre"]]
    return out


# --- identity ----------------------------------------------------------------------------------

GEOMETRY_KEYS = ("stack", "conductors", "vias", "ports", "domain")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def geometry_hash(doc: Dict[str, Any]) -> str:
    """sha256 of the parts a solver sees (not the mesh hints, provenance or features)."""
    return hashlib.sha256(canonical({k: doc.get(k) for k in GEOMETRY_KEYS}).encode()).hexdigest()


def new(
    name: str, box: Sequence[float], boundaries: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """An empty document over ``box`` (all faces first-order absorbing unless given)."""
    bnd = {f: "abc1" for f in FACES}
    bnd.update(boundaries or {})
    return dict(
        schema=SCHEMA,
        name=name,
        units="mm",
        stack=dict(dielectrics=[], layers=[]),
        conductors=[],
        vias=[],
        ports=[],
        domain=dict(box=[float(v) for v in box], boundaries=bnd),
        mesh={},
        features=[],
        provenance={},
    )


def with_layer_model(
    doc: Dict[str, Any], model: str, layers: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """A copy with the copper model of ``layers`` (default: every sheet or solid layer) changed."""
    if model not in LAYER_MODELS:
        raise PlanarError(f"unknown copper model {model!r}")
    out = copy.deepcopy(doc)
    names = set(layers) if layers is not None else None
    for lay in out["stack"]["layers"]:
        if (names is None and lay["model"] != "pec") or (
            names is not None and lay["name"] in names
        ):
            lay["model"] = model
    return out
