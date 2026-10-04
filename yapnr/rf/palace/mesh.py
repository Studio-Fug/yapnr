"""Gmsh OCC builder: a yapnr-planar-v1 document becomes a tetrahedral mesh for Palace.

Geometry, in the document's millimetres:

- the domain box is the air; dielectric slabs (boxes, or an ``outline`` extruded for a finite
  board) override it, later slabs over earlier ones;
- ``sheet`` and ``pec`` copper are zero-thickness surfaces at the layer's z, with exact arcs;
  ``solid`` copper is extruded by t and removed from the domain (its surface is the conductor);
- via barrels are cylinders removed from the domain (their surface is PEC, as openEMS's posts);
- wave-port faces are rectangles on the walls, lumped-port faces rectangles across the dielectric.

One ``occ.fragment`` makes everything conforming. Physical groups (Palace's attributes):

- volumes: ``air`` and ``diel:<name>``;
- surfaces: ``cond:<layer>:<net>`` (sheets, or the surface of solid copper), ``via:<net>``,
  ``port:<name>``, and ``wall:<face>`` for the parts of each outer face that are not ports.

Sizes: a distance field from every copper and via edge gives ``edge_h`` at the edges, growing by
``grade`` per mm, capped per region at wavelength / ``lam_frac`` at ``f_max`` (in each
dielectric, and in air), at ``slab_cap`` x the thickness of a slab (thin cores stay well shaped)
and at ``port_h`` on port faces. ``mesh.json`` records the attribute map, element counts, an
estimate of Palace's unknowns per element order, and element quality.

gmsh (GPL-2.0-or-later) is imported lazily: the mesher runs in the FEA environment and in the
Palace task image, never under Bazel.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from yapnr.rf.planar import model

C0 = 299792458.0
DEFAULTS: Dict[str, Any] = dict(
    edge_h=0.04,  # mm, at copper and via edges: about 2.5 elements across a 0.1 mm core there
    grade=0.35,  # size growth per mm of distance from the nearest copper edge
    lam_frac=6.0,  # bulk size: local wavelength / lam_frac at f_max (p = 2 elements)
    slab_cap=1.5,  # a slab's elements at most slab_cap x its thickness
    port_h=0.1,  # mm, largest element on a port face (the 2D mode solve lives there)
    f_max_ghz=70.0,
    algorithm3d=1,  # gmsh Delaunay (deterministic); 10 = HXT (parallel)
    threads=1,
    binary=True,
    # Netgen pass: lifts the worst tetrahedra (gamma 0.02 -> 0.16 on patch-w). gmsh 4.15.2's Netgen
    # optimizer crashes (SIGSEGV) on a volume with an embedded face, so a model with a lumped port
    # (a face inside the dielectric) is never passed to it.
    optimize_netgen=True,
)
TOL = 1e-6


def _gmsh():
    try:
        import gmsh
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("the Palace mesher needs gmsh (pip install gmsh)") from exc
    return gmsh


class _Builder:
    def __init__(self, gmsh, doc: Dict[str, Any]):
        self.gmsh = gmsh
        self.occ = gmsh.model.occ
        self.doc = doc
        self.inputs: List[Tuple[Tuple[int, int], str, Any]] = []

    def loop(self, ring, z):
        occ = self.occ
        edges = model.ring_edges(ring)
        curves = []
        tags = []
        for _, a, _, _ in edges:
            tags.append(occ.addPoint(a[0], a[1], z))
        n = len(edges)
        for i, (kind, a, m, b) in enumerate(edges):
            pa, pb = tags[i], tags[(i + 1) % n]
            if kind == "line":
                curves.append(occ.addLine(pa, pb))
            else:
                pm = occ.addPoint(m[0], m[1], z)
                curves.append(occ.addCircleArc(pa, pm, pb, center=False))
                occ.remove([(0, pm)])
        return occ.addCurveLoop(curves)

    def surface(self, poly, z):
        loops = [self.loop(poly["outer"], z)] + [self.loop(h, z) for h in poly.get("holes", [])]
        return self.occ.addPlaneSurface(loops)

    def rect(self, corners):
        occ = self.occ
        pts = [occ.addPoint(*c) for c in corners]
        lines = [occ.addLine(pts[i], pts[(i + 1) % 4]) for i in range(4)]
        return occ.addPlaneSurface([occ.addCurveLoop(lines)])

    def add(self, dimtag, role, info):
        self.inputs.append((dimtag, role, info))


def _fragment(occ, dimtags: List[Tuple[int, int]], late: List[bool]) -> List[List[Tuple[int, int]]]:
    """Fragment in two passes, the ``late`` entities (port faces) into the conforming result of
    the others, and return the map from every input to its pieces. One pass with an internal
    lumped-port face that touches a window plane on its edge made OCC return overlapping,
    negative-volume solids (patch-finite); two passes do not."""
    first = [dt for dt, lt in zip(dimtags, late) if not lt]
    rest = [dt for dt, lt in zip(dimtags, late) if lt]
    out1, map1 = occ.fragment(first, [])
    if not rest:
        it = iter(map1)
        return [next(it) for _ in dimtags]
    # out1 lists the top-level results only; a sheet that became a face of a volume is in map1
    stage = []
    for dt in list(out1) + [x for m in map1 for x in m]:
        if tuple(dt) not in stage:
            stage.append(tuple(dt))
    out2, map2 = occ.fragment(stage + rest, [])
    pos = {dt: k for k, dt in enumerate(stage)}
    composed, k1, k2 = [], 0, len(stage)
    for lt in late:
        if lt:
            composed.append([tuple(x) for x in map2[k2]])
            k2 += 1
        else:
            pieces = []
            for dt in map1[k1]:
                for x in map2[pos[tuple(dt)]]:
                    if tuple(x) not in pieces:
                        pieces.append(tuple(x))
            composed.append(pieces)
            k1 += 1
    return composed


def _check_volumes(gmsh) -> None:
    bad = [(t, gmsh.model.occ.getMass(3, t)) for _, t in gmsh.model.getEntities(3)]
    bad = [(t, m) for t, m in bad if m <= 1e-12]
    if bad:
        raise ValueError(f"OCC fragment produced degenerate volumes {bad}: check the geometry")


def _bulk_sizes(doc: Dict[str, Any], o: Dict[str, Any]) -> Dict[str, float]:
    f = float(o["f_max_ghz"]) * 1e9
    lam0 = C0 / f * 1e3
    out = {"air": lam0 / o["lam_frac"]}
    for d in doc["stack"]["dielectrics"]:
        out[d["name"]] = min(
            lam0 / math.sqrt(d["eps_r"]) / o["lam_frac"], o["slab_cap"] * (d["z1"] - d["z0"])
        )
    return out


def build(
    doc: Dict[str, Any], out_dir: Optional[str] = None, name: str = "mesh", **opts
) -> Dict[str, Any]:
    """Mesh ``doc``; write ``<name>.msh`` (msh 2.2) and ``<name>.json`` into ``out_dir`` when
    given. Returns the mesh record (groups, counts, quality, options, geometry hash)."""
    model.check(doc)
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (doc.get("mesh") or {}).items() if k in DEFAULTS})
    o.update({k: v for k, v in opts.items() if v is not None})
    unknown = set(opts) - set(DEFAULTS)
    if unknown:
        raise TypeError(f"unknown mesh options {sorted(unknown)}")
    gmsh = _gmsh()
    t_start = time.time()
    if not gmsh.isInitialized():
        gmsh.initialize(readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.Verbosity", 2)
        gmsh.option.setNumber("General.NumThreads", int(o["threads"]))
        gmsh.model.add(doc.get("name", "planar"))
        rec = _build(gmsh, doc, o)
        rec["seconds"] = round(time.time() - t_start, 2)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
            gmsh.option.setNumber("Mesh.Binary", 1 if o["binary"] else 0)
            gmsh.option.setNumber("Mesh.SaveAll", 0)
            msh = os.path.join(out_dir, f"{name}.msh")
            gmsh.write(msh)
            rec["file"] = os.path.basename(msh)
            rec["bytes"] = os.path.getsize(msh)
            with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1)
                fh.write("\n")
        return rec
    finally:
        gmsh.finalize()


def _build(gmsh, doc: Dict[str, Any], o: Dict[str, Any]) -> Dict[str, Any]:
    occ = gmsh.model.occ
    b = _Builder(gmsh, doc)
    x0, x1, y0, y1, z0, z1 = (float(v) for v in doc["domain"]["box"])
    layers = {la["name"]: la for la in doc["stack"]["layers"]}
    b.add((3, occ.addBox(x0, y0, z0, x1 - x0, y1 - y0, z1 - z0)), "air", None)
    for k, d in enumerate(doc["stack"]["dielectrics"]):
        if d.get("outline"):
            s = b.surface(dict(outer=d["outline"]), d["z0"])
            ext = occ.extrude([(2, s)], 0, 0, d["z1"] - d["z0"])
            vols = [dt for dt in ext if dt[0] == 3]
            for dt in vols:
                b.add(dt, "diel", k)
        else:
            b.add((3, occ.addBox(x0, y0, d["z0"], x1 - x0, y1 - y0, d["z1"] - d["z0"])), "diel", k)
    for k, v in enumerate(doc.get("vias", [])):
        za, zb = model.z_of(doc, v["from"]), model.z_of(doc, v["to"])
        b.add(
            (3, occ.addCylinder(v["at"][0], v["at"][1], za, 0, 0, zb - za, 0.5 * v["drill"])),
            "via",
            k,
        )
    for c in doc["conductors"]:
        lay = layers[c["layer"]]
        for poly in c["polygons"]:
            if lay["model"] == "solid":
                s = b.surface(poly, lay["z"])
                ext = occ.extrude([(2, s)], 0, 0, lay["t"])
                for dt in ext:
                    if dt[0] == 3:
                        b.add(dt, "solid", (c["layer"], c["net"]))
            else:
                b.add((2, b.surface(poly, lay["z"])), "sheet", (c["layer"], c["net"]))
    ports = model.port_geometry(doc)
    for p in doc.get("ports", []):
        b.add((2, b.rect(ports[p["name"]]["rect"])), "port", p["name"])
    omap = _fragment(
        occ, [dt for dt, role, _ in b.inputs], [role == "port" for _, role, _ in b.inputs]
    )
    occ.synchronize()
    _check_volumes(gmsh)

    # volumes: the highest-priority input owns each fragment
    owner: Dict[int, Tuple[int, str, Any]] = {}
    n_diel = len(doc["stack"]["dielectrics"])
    for (dt, role, info), mapped in zip(b.inputs, omap):
        prio = {
            "air": 0,
            "diel": 1 + (info if role == "diel" else 0),
            "via": 2 + n_diel,
            "solid": 2 + n_diel,
        }.get(role)
        if prio is None:
            continue
        for dim, tag in mapped:
            if dim == 3 and (tag not in owner or owner[tag][0] < prio):
                owner[tag] = (prio, role, info)
    kept = sorted(t for t, (_, r, _) in owner.items() if r in ("air", "diel"))
    removed = sorted(t for t, (_, r, _) in owner.items() if r in ("via", "solid"))

    def faces_of(vol):
        return [
            abs(t)
            for d, t in gmsh.model.getBoundary([(3, vol)], combined=False, oriented=False)
            if d == 2
        ]

    kept_count: Dict[int, int] = {}
    for v in kept:
        for f in faces_of(v):
            kept_count[f] = kept_count.get(f, 0) + 1
        # a face inside a volume that does not split it (a lumped port across the core) is
        # embedded, not part of the boundary: it is internal, like a face between two volumes
        for d, f in gmsh.model.mesh.getEmbedded(3, v):
            if d == 2:
                kept_count[abs(f)] = kept_count.get(abs(f), 0) + 2
    removed_of: Dict[int, Tuple[str, Any]] = {}
    for v in removed:
        _, role, info = owner[v]
        for f in faces_of(v):
            removed_of.setdefault(f, (role, info))
    from_input: Dict[int, List[Tuple[str, Any]]] = {}
    for (dt, role, info), mapped in zip(b.inputs, omap):
        if role in ("sheet", "port"):
            for dim, tag in mapped:
                if dim == 2:
                    from_input.setdefault(tag, []).append((role, info))

    walls = {
        "xmin": (0, x0),
        "xmax": (0, x1),
        "ymin": (1, y0),
        "ymax": (1, y1),
        "zmin": (2, z0),
        "zmax": (2, z1),
    }
    scale = max(x1 - x0, y1 - y0, z1 - z0)

    def wall_of(f):
        bb = gmsh.model.getBoundingBox(2, f)
        for name, (ax, val) in walls.items():
            if abs(bb[ax] - val) < 1e-7 * scale and abs(bb[ax + 3] - val) < 1e-7 * scale:
                return name
        return None

    port_kind = {p["name"]: p["kind"] for p in doc.get("ports", [])}
    groups2: Dict[str, List[int]] = {}
    problems: List[str] = []
    for f, cnt in sorted(kept_count.items()):
        tags = from_input.get(f, [])
        port = next((info for role, info in tags if role == "port"), None)
        sheet = next((info for role, info in tags if role == "sheet"), None)
        if port is not None:
            groups2.setdefault(f"port:{port}", []).append(f)
        elif f in removed_of:
            role, info = removed_of[f]
            if role == "via":
                groups2.setdefault(f"via:{doc['vias'][info]['net']}", []).append(f)
            else:
                groups2.setdefault(f"cond:{info[0]}:{info[1]}", []).append(f)
        elif sheet is not None:
            groups2.setdefault(f"cond:{sheet[0]}:{sheet[1]}", []).append(f)
        elif cnt == 1:
            w = wall_of(f)
            if w is None:
                problems.append(f"exterior face {f} is neither a wall, a port nor a conductor")
            else:
                groups2.setdefault(f"wall:{w}", []).append(f)
    for name in list(groups2):
        if name.startswith("port:") and port_kind.get(name[5:]) == "wave":
            for f in groups2[name]:
                if kept_count.get(f) != 1:
                    problems.append(f"wave port {name[5:]}: face {f} is not on the outer boundary")
    if problems:
        raise ValueError("mesh topology:\n  " + "\n  ".join(problems))

    # drop the removed volumes and any surface no kept volume touches (sheet pieces inside vias)
    if removed:
        occ.remove([(3, v) for v in removed])
    orphans = sorted(
        {t for _, mapped in zip(b.inputs, omap) for d, t in mapped if d == 2} - set(kept_count)
    )
    if orphans:
        occ.remove([(2, f) for f in orphans], recursive=True)
    occ.synchronize()

    # physical groups: volumes first, then surfaces, numbered from 1 in a stable order
    groups3: Dict[str, List[int]] = {}
    for v in kept:
        _, role, info = owner[v]
        key = "air" if role == "air" else f"diel:{doc['stack']['dielectrics'][info]['name']}"
        groups3.setdefault(key, []).append(v)
    attr: Dict[str, Dict[str, Any]] = {}
    tag = 1
    for key in sorted(groups3, key=lambda k: (k != "air", k)):
        gmsh.model.addPhysicalGroup(3, groups3[key], tag, key)
        attr[key] = dict(dim=3, tag=tag, entities=len(groups3[key]))
        tag += 1
    order = ("port:", "cond:", "via:", "wall:")
    for key in sorted(
        groups2, key=lambda k: (next(i for i, p in enumerate(order) if k.startswith(p)), k)
    ):
        gmsh.model.addPhysicalGroup(2, sorted(groups2[key]), tag, key)
        attr[key] = dict(dim=2, tag=tag, entities=len(groups2[key]))
        tag += 1

    sizes = _size_fields(gmsh, doc, o, groups2, groups3)
    gmsh.option.setNumber("Mesh.Algorithm", 6)
    gmsh.option.setNumber("Mesh.Algorithm3D", int(o["algorithm3d"]))
    embedded = any(p["kind"] == "lumped" for p in doc.get("ports", []))
    netgen = bool(o["optimize_netgen"]) and not embedded
    gmsh.option.setNumber("Mesh.OptimizeNetgen", 1 if netgen else 0)
    t0 = time.time()
    gmsh.model.mesh.generate(3)
    t_mesh = time.time() - t0
    rec = _record(gmsh, doc, o, attr, sizes, t_mesh, ports)
    rec["optimizer"] = "gmsh + netgen" if netgen else "gmsh"
    return rec


def _size_fields(gmsh, doc, o, groups2, groups3) -> Dict[str, float]:
    fld = gmsh.model.mesh.field
    edge_faces = [f for k, fs in groups2.items() if k.startswith(("cond:", "via:")) for f in fs]
    curves = sorted(
        {
            abs(t)
            for d, t in gmsh.model.getBoundary(
                [(2, f) for f in edge_faces], combined=False, oriented=False
            )
            if d == 1
        }
    )
    bulk = _bulk_sizes(doc, o)
    h_air = bulk["air"]
    fields = []
    if curves:
        longest = max(gmsh.model.occ.getMass(1, c) for c in curves)
        fd = fld.add("Distance")
        fld.setNumbers(fd, "CurvesList", curves)
        fld.setNumber(
            fd, "Sampling", int(min(2000, max(20, math.ceil(longest / (0.5 * o["edge_h"])))))
        )
        fm = fld.add("MathEval")
        fld.setString(fm, "F", f"{o['edge_h']} + {o['grade']}*F{fd}")
        fields.append(fm)
    for key, vols in groups3.items():
        h = h_air if key == "air" else bulk[key.split(":", 1)[1]]
        fc = fld.add("Constant")
        fld.setNumber(fc, "VIn", h)
        fld.setNumber(fc, "VOut", 1e22)
        fld.setNumbers(fc, "VolumesList", vols)
        fld.setNumber(fc, "IncludeBoundary", 1)
        fields.append(fc)
    port_faces = [f for k, fs in groups2.items() if k.startswith("port:") for f in fs]
    if port_faces:
        fp = fld.add("Constant")
        fld.setNumber(fp, "VIn", o["port_h"])
        fld.setNumber(fp, "VOut", 1e22)
        fld.setNumbers(fp, "SurfacesList", port_faces)
        fld.setNumber(fp, "IncludeBoundary", 1)
        fields.append(fp)
    fmin = fld.add("Min")
    fld.setNumbers(fmin, "FieldsList", fields)
    fld.setAsBackgroundMesh(fmin)
    for k in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
        gmsh.option.setNumber(f"Mesh.{k}", 0)
    gmsh.option.setNumber("Mesh.MeshSizeMin", 0.25 * o["edge_h"])
    gmsh.option.setNumber("Mesh.MeshSizeMax", h_air)
    return dict(
        edge_h=o["edge_h"],
        air=round(h_air, 4),
        **{k: round(v, 4) for k, v in bulk.items() if k != "air"},
        edge_curves=len(curves),
    )


def _quantiles(vals, qs):
    s = sorted(vals)
    return [s[min(len(s) - 1, int(q * (len(s) - 1)))] for q in qs]


def _record(gmsh, doc, o, attr, sizes, t_mesh, ports) -> Dict[str, Any]:
    msh = gmsh.model.mesh
    tet_tags, _ = msh.getElementsByType(4)
    n_tet = len(tet_tags)
    node_tags, _, _ = msh.getNodes()
    counts = {}
    for key, a in attr.items():
        n = 0
        for ent in gmsh.model.getEntitiesForPhysicalGroup(a["dim"], a["tag"]):
            types, tags, _ = msh.getElements(a["dim"], ent)
            n += sum(len(t) for t in tags)
        counts[key] = n
        a["elements"] = n
    q: Dict[str, Any] = {}
    if n_tet:
        gam = list(msh.getElementQualities(list(tet_tags), "gamma"))
        sicn = list(msh.getElementQualities(list(tet_tags), "minSICN"))
        q = dict(
            gamma_min=round(float(min(gam)), 4),
            gamma_p01=round(float(_quantiles(gam, [0.01])[0]), 4),
            gamma_median=round(float(_quantiles(gam, [0.5])[0]), 4),
            gamma_below_0_1=int(sum(1 for g in gam if g < 0.1)),
            sicn_min=round(float(min(sicn)), 4),
            sicn_below_0=int(sum(1 for s in sicn if s <= 0)),
        )
    edges = faces = None
    try:
        msh.createEdges()
        msh.createFaces()
        edges = len(msh.getAllEdges()[0])
        faces = len(msh.getAllFaces(3)[0])
    except Exception:  # pragma: no cover - older gmsh
        pass
    if edges is None:
        edges, faces = int(round(1.18 * n_tet)), 2 * n_tet
    # Nedelec (first kind) unknowns of order p on tetrahedra: p per edge, p(p-1) per face,
    # p(p-1)(p-2)/2 per element; before boundary conditions remove the PEC ones
    dofs = {
        str(p): int(p * edges + p * (p - 1) * faces + p * (p - 1) * (p - 2) // 2 * n_tet)
        for p in (1, 2, 3)
    }
    return dict(
        schema="yapnr-palace-mesh-v1",
        model=doc.get("name"),
        geometry_sha256=model.geometry_hash(doc),
        gmsh=gmsh.__version__,
        units="mm",
        options={k: o[k] for k in sorted(o)},
        sizes=sizes,
        groups=attr,
        nodes=len(node_tags),
        tetrahedra=n_tet,
        edges=edges,
        faces=faces,
        dofs_estimate=dofs,
        quality=q,
        mesh_seconds=round(t_mesh, 2),
        ports={
            k: {
                kk: v[kk]
                for kk in (
                    "kind",
                    "rect",
                    "offset",
                    "voltage_path",
                    "z_sig",
                    "z_ref",
                    "half_width_actual",
                )
            }
            for k, v in ports.items()
        },
    )


def attribute(rec: Dict[str, Any], key: str) -> int:
    return int(rec["groups"][key]["tag"])


def attributes(rec: Dict[str, Any], prefix: str) -> Dict[str, int]:
    return {k: int(v["tag"]) for k, v in rec["groups"].items() if k.startswith(prefix)}


def summary(rec: Dict[str, Any]) -> str:
    q = rec.get("quality", {})
    d2, d3 = rec["dofs_estimate"]["2"] / 1e6, rec["dofs_estimate"]["3"] / 1e6
    return (
        f"{rec['model']}: {rec['tetrahedra']} tets, {rec['nodes']} nodes, DOFs p2 ~{d2:.2f} M "
        f"(p3 ~{d3:.2f} M), gamma min {q.get('gamma_min')} p01 {q.get('gamma_p01')}, "
        f"mesh {rec['mesh_seconds']} s"
    )


def size_summary(rec: Dict[str, Any], keys: Sequence[str] = ("edge_h", "air")) -> str:
    return ", ".join(f"{k} {rec['sizes'][k]}" for k in keys if k in rec["sizes"])
