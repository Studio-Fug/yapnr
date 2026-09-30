"""3D board view: headless KiCad GLB export queue, cache and compaction (stdlib only).

Server side (Viewer3DService, used by server.py): GET /api/3d resolves a board by its sha256 (the
immutable <root>/boards/<sha>.kicad_pcb copy, else the lane's native board when it still hashes to
that sha: re-hashed on every request and exported from a snapshot of those bytes), keys it by its
*placement fingerprint* (the board text without top-level segment/arc items, copper zones and tented
vias) plus everything else the GLB depends on (export version and flags, the resolved kicad-cli and
its 3D library, the parts folder and the size/mtime of the project model files the board references)
and queues at most one export at a time (one worker thread; a machine-wide flock shared by the
viewers under one experiment folder). Viewers may share one cache folder (prod and dev on one root):
a cached "ready" is re-checked on disk, and a viewer only ever removes its own export's scratch
folder. Routing-only revisions therefore share one GLB: copper is drawn in the browser from the
viewer's own geometry, which carries nets for picking. The export runs as
`python -m yapnr.viewer.services.viewer3d export ...` in its own process group (nice 10), killed on
timeout or server stop; requests only enqueue and return.

Job side: the board is copied into a scratch folder with every `.../parts/<Part>/<file>` model path
pointed at the atopile parts folder (so `${KIPRJMOD}/../../src/parts` resolves wherever the board
copy lives), then `kicad-cli pcb export glb` runs with the *headless* CLI only (never
/Applications/KiCad: that copy puts an icon in the Dock). The GLB is compacted: one node per
footprint (named by ref) and one primitive per material, vertices baked into the viewer's engine
frame (mm; x, y as the PCB canvas, origin at the Edge.Cuts bbox bottom-left; z up, 0 = bottom of the
board body), board body / silkscreen / soldermask as `__board`, `__silk_F`, `__mask_B`, ...
Footprints whose models are missing simply have no node; the meta lists them and the browser draws
boxes."""

import argparse
import array
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from yapnr.viewer import runtime as viewer_runtime
from yapnr.viewer.toolchain import (  # noqa: F401 (re-exported for the job side and tests)
    HEADLESS_CLI,
    Unavailable,
    app_bundle,
    background_only,
    bundle_info,
    kicad_cli,
)

JOB = "yapnr.viewer.services.viewer3d"
PYTHON_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH")
SCHEMA = "yapnr-viewer3d-v1"
# Bump when the flags, the compaction or the meta change: new cache keys. 2: yapnr schema names.
EXPORT_VERSION = 2
FLAGS = (
    "--subst-models",
    "--include-silkscreen",
    "--include-soldermask",
)  # no copper: the viewer draws it from geometry
HEX64 = re.compile("[0-9a-f]{64}")
ROUTING = re.compile(r"(segment|arc|via|zone)[\s)]")
TENTING = re.compile(r"\(tenting\b((?:[^()]|\([^()]*\))*)\)")
MASK_LAYER = re.compile(r"\(layers?\b[^)]*\.Mask")
MODEL = re.compile(r'(\(model\s+")([^"\n]*?/parts/)([^"/\n]+/[^"/\n]+)(")')
MISSING = re.compile(r"Could not add 3D model for (\S+?)\.?\s*\n\s*File not found:\s*(.+)")
REFS = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')


def headless_cli(path=None):
    """The kicad-cli to use (``yapnr.viewer.toolchain.kicad_cli``): the explicit path, else
    ``$YAPNR_KICAD_CLI`` (alias ``$PNR_KICAD_CLI``), else the headless copy on macOS or
    ``kicad-cli`` on ``PATH`` elsewhere. Refuses the GUI application's CLI (anything inside
    KiCad.app or /Applications/KiCad, also through a symlink) and a CLI inside any .app bundle whose
    Info.plist is not background-only (LSBackgroundOnly / LSUIElement), such as a renamed copy of
    the GUI application: LaunchServices registers that bundle as a regular application, which puts
    an icon in the Dock. (The headless bundle still registers, as BackgroundOnly: no Dock icon,
    never frontmost.)"""
    return kicad_cli(explicit=path)


def model_env(cli):
    """KiCad's stock 3D library for ${KICADn_3DMODEL_DIR} references, from the CLI's own bundle.

    The viewer's Python path is not passed on: KiCad embeds its own Python.
    """
    env = {k: v for k, v in os.environ.items() if k not in PYTHON_ENV}
    lib = Path(cli).parent.parent / "SharedSupport/3dmodels"
    if lib.is_dir():
        for v in (6, 7, 8, 9, 10):
            env.setdefault(f"KICAD{v}_3DMODEL_DIR", str(lib))
    return env


def export_env(cli):
    """What the GLB depends on besides the board and the parts folder: the resolved kicad-cli (path,
    size, mtime, bundle version) and the KICADn_3DMODEL_DIR library it resolves stock models from
    (the server's environment wins over the CLI's own bundle). Part of every cache key: another
    KiCad or another library means new GLBs (and new URLs, which the browser caches as
    immutable)."""
    p = Path(cli).resolve()
    st = p.stat()
    b = app_bundle(p)
    env = model_env(cli)
    libs = {}
    for k in sorted(env):
        if re.fullmatch(r"KICAD\d+_3DMODEL_DIR", k):
            try:
                libs[k] = [env[k], Path(env[k]).stat().st_mtime_ns]
            except OSError:
                libs[k] = [env[k], None]
    return dict(
        cli=str(p),
        size=st.st_size,
        mtime=st.st_mtime_ns,
        version=bundle_info(b).get("CFBundleVersion") if b else None,
        libs=libs,
    )


def tented(t):
    """A (tenting ...) body: KiCad 9/10 `(front yes) (back yes)`, KiCad 8 `front back`."""
    return (
        bool(re.search(r"\(front\s+yes\)", t) and re.search(r"\(back\s+yes\)", t))
        if "(" in t
        else {"front", "back"} <= set(t.split())
    )


def placement_text(text):
    """The board without what its GLB never shows (KiCad writes each top-level item at one tab):
    segment/arc items, copper zones and vias, unless a via is untented (board setup, or its own
    tenting): its soldermask opening is in the GLB (--include-soldermask), so it stays in the
    fingerprint; so does a zone on a mask layer."""
    parts = text.split("\n\t(")
    if len(parts) < 3:
        return text
    setup = next((p for p in parts[1:] if p.startswith("setup")), "")
    m = TENTING.search(setup)
    tent = bool(m) and tented(m[1])

    def routing(p):
        m = ROUTING.match(p)
        if not m:
            return False
        if m[1] == "via":
            own = TENTING.search(p)
            return tented(own[1]) if own else tent
        return m[1] != "zone" or not MASK_LAYER.search(p)

    return "\n\t(".join([parts[0]] + [p for p in parts[1:] if not routing(p)])


def model_rels(text):
    """`<Part>/<file>` of every `.../parts/<Part>/<file>` model the board references."""
    return tuple(sorted({m[3] for m in MODEL.finditer(text)}))


def models_digest(parts, rels):
    """Size and mtime of each referenced project model file (or its absence): an edited model means
    a new GLB."""
    h = hashlib.sha256()
    for r in rels:
        try:
            st = (Path(parts) / r).stat()
            h.update(f"{r}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{r}\0-\n".encode())
    return h.hexdigest()


def key_of(placement_sha, rels, parts=None, env=None, digest=None):
    head = json.dumps([EXPORT_VERSION, FLAGS, str(parts or ""), env], sort_keys=True)
    if digest is None:
        digest = models_digest(parts, rels) if parts and rels else ""
    return hashlib.sha256("\n".join([head, digest, placement_sha]).encode()).hexdigest()


def placement_sha(text):
    return hashlib.sha256(placement_text(text).encode()).hexdigest()


def cache_key(text, parts=None, env=None):
    """The GLB cache key of a board: placement fingerprint + export version/flags + parts folder and
    its model files + export environment (export_env)."""
    return key_of(placement_sha(text), model_rels(text), parts, env)


def rewrite_models(text, parts):
    """Point `.../parts/<Part>/<file>` model paths at the parts folder when that file exists
    there."""
    if not parts or '"' in str(parts) or "\\" in str(parts):
        return text
    parts = Path(parts)

    def sub(m):
        f = parts / m[3]
        return m[1] + str(f) + m[4] if f.is_file() else m[0]

    return MODEL.sub(sub, text)


# ------------------------------------------------------------------ GLB compaction
def read_glb(data):
    magic, ver, length = struct.unpack_from("<4sII", data, 0) if len(data) >= 12 else (b"", 0, 0)
    if magic != b"glTF" or ver != 2:
        raise ValueError("not a binary glTF 2.0 file")
    off, js, binary = 12, None, b""
    while off + 8 <= min(length, len(data)):
        n, kind = struct.unpack_from("<II", data, off)
        chunk = data[off + 8 : off + 8 + n]
        off += 8 + n + (-n % 4)
        if kind == 0x4E4F534A:
            js = json.loads(chunk)
        elif kind == 0x004E4942:
            binary = chunk
    if js is None:
        raise ValueError("GLB without a JSON chunk")
    return js, binary


def accessor(js, binary, i):
    a = js["accessors"][i]
    if "sparse" in a or "bufferView" not in a:
        raise ValueError("sparse or bufferless accessors are not supported")
    bv = js["bufferViews"][a["bufferView"]]
    comps = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
    fmt = {5126: "f", 5125: "I", 5123: "H", 5121: "B"}[a["componentType"]]
    size = struct.calcsize(fmt)
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride") or size * comps
    n = a["count"]
    out = array.array(fmt)
    if stride == size * comps:
        out.frombytes(binary[start : start + n * stride])
    else:
        for k in range(n):
            out.frombytes(binary[start + k * stride : start + k * stride + size * comps])
    return out


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def local_matrix(n):
    if "matrix" in n:
        m = n["matrix"]
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]  # glTF: column-major
    x, y, z, w = n.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = n.get("scale", (1, 1, 1))
    tx, ty, tz = n.get("translation", (0, 0, 0))
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return [
        [r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, tx],
        [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, ty],
        [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, tz],
        [0, 0, 0, 1],
    ]


IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def compact(data):
    """KiCad GLB -> (compact GLB bytes, info). One node per top-level item (footprint ref or board
    part), one primitive per material, positions/normals baked into the engine frame (mm, y up in
    the board plane, z = height)."""
    if sys.byteorder != "little":
        raise RuntimeError("big-endian hosts are not supported")
    js, binary = read_glb(data)
    nodes = js.get("nodes", [])
    meshes = js.get("meshes", [])
    scene = js["scenes"][js.get("scene", 0)]["nodes"]
    # top-level items: children of KiCad's unnamed root (or the scene nodes themselves)
    tops = []
    for s in scene:
        n = nodes[s]
        tops += (
            [(c, local_matrix(n)) for c in n.get("children", [])]
            if n.get("name") is None and "mesh" not in n
            else [(s, IDENTITY)]
        )

    def walk(i, m, out):
        n = nodes[i]
        m = mat_mul(m, local_matrix(n))
        if "mesh" in n:
            out.append((n["mesh"], m))
        for c in n.get("children", []):
            walk(c, m, out)
        return out

    items = []
    for i, m in tops:
        inst = walk(i, m, [])
        name = nodes[i].get("name")
        mesh_names = {meshes[k].get("name", "") for k, _ in inst}
        if not inst:
            continue
        if name and not name.startswith("=>"):
            items.append(dict(name=name, kind="component", inst=inst))
        else:
            items.append(
                dict(
                    name=next(iter(sorted(mesh_names))) if mesh_names else "part",
                    kind="board",
                    inst=inst,
                )
            )

    # the engine frame: Edge.Cuts bbox from the board body (min X, max Z in metres), else from
    # everything
    def bounds(its):
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        for it in its:
            for k, m in it["inst"]:
                for p in meshes[k]["primitives"]:
                    a = js["accessors"][p["attributes"]["POSITION"]]
                    if "min" not in a or "max" not in a:
                        v = accessor(js, binary, p["attributes"]["POSITION"])
                        a = dict(
                            min=[min(v[j::3]) for j in range(3)],
                            max=[max(v[j::3]) for j in range(3)],
                        )
                    for cx in (a["min"][0], a["max"][0]):
                        for cy in (a["min"][1], a["max"][1]):
                            for cz in (a["min"][2], a["max"][2]):
                                for j, v in enumerate(
                                    row[0] * cx + row[1] * cy + row[2] * cz + row[3]
                                    for row in m[:3]
                                ):
                                    lo[j] = min(lo[j], v)
                                    hi[j] = max(hi[j], v)
        return lo, hi

    body = [it for it in items if it["kind"] == "board" and it["name"] == "board_PCB"]
    lo, hi = bounds(body or items)
    if not all(map(math.isfinite, lo + hi)):
        raise ValueError("empty GLB: no meshes")
    E = [[1000, 0, 0, -1000 * lo[0]], [0, 0, -1000, 1000 * hi[2]], [0, 1000, 0, 0], [0, 0, 0, 1]]
    out_bin = bytearray()
    views = []
    accs = []
    out_meshes = []
    out_nodes = [dict(name="board", children=[])]
    tris = 0

    def put(arr, target, kind, ctype, count, mn=None, mx=None):
        out_bin.extend(b"\0" * (-len(out_bin) % 4))
        views.append(
            dict(
                buffer=0, byteOffset=len(out_bin), byteLength=len(arr) * arr.itemsize, target=target
            )
        )
        out_bin.extend(arr.tobytes())
        a = dict(bufferView=len(views) - 1, componentType=ctype, count=count, type=kind)
        if mn is not None:
            a.update(min=mn, max=mx)
        accs.append(a)
        return len(accs) - 1

    zmid = None
    if body:
        blo, bhi = bounds(body)
        zmid = (bhi[1] + blo[1]) * 500  # board body mid-height in mm (glTF Y)
    info = dict(
        frame=dict(left_m=lo[0], bottom_m=hi[2]),
        board=dict(
            width=(hi[0] - lo[0]) * 1000,
            height=(hi[2] - lo[2]) * 1000,
            z0=lo[1] * 1000,
            z1=hi[1] * 1000,
        ),
        refs={},
        parts=[],
    )
    if not body:
        info["board"] = None
    used_names = set()
    for it in items:
        groups = {}
        for k, m in it["inst"]:
            M = mat_mul(E, m)
            A = [r[:3] for r in M[:3]]
            t = [r[3] for r in M[:3]]
            det = (
                A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1])
                - A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0])
                + A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0])
            )
            C = [
                [
                    A[1][1] * A[2][2] - A[1][2] * A[2][1],
                    A[1][2] * A[2][0] - A[1][0] * A[2][2],
                    A[1][0] * A[2][1] - A[1][1] * A[2][0],
                ],
                [
                    A[0][2] * A[2][1] - A[0][1] * A[2][2],
                    A[0][0] * A[2][2] - A[0][2] * A[2][0],
                    A[0][1] * A[2][0] - A[0][0] * A[2][1],
                ],
                [
                    A[0][1] * A[1][2] - A[0][2] * A[1][1],
                    A[0][2] * A[1][0] - A[0][0] * A[1][2],
                    A[0][0] * A[1][1] - A[0][1] * A[1][0],
                ],
            ]  # cofactors: inverse-transpose * det
            sg = 1 if det >= 0 else -1
            for p in meshes[k]["primitives"]:
                if p.get("mode", 4) != 4:
                    continue
                P = accessor(js, binary, p["attributes"]["POSITION"])
                nv = len(P) // 3
                Nr = (
                    accessor(js, binary, p["attributes"]["NORMAL"])
                    if "NORMAL" in p["attributes"]
                    else None
                )
                idx_in = (
                    accessor(js, binary, p["indices"])
                    if "indices" in p
                    else array.array("I", range(nv))
                )
                g = groups.setdefault(
                    p.get("material", -1),
                    dict(P=array.array("f"), N=array.array("f"), I=array.array("I"), n=0),
                )
                a00, a01, a02 = A[0]
                a10, a11, a12 = A[1]
                a20, a21, a22 = A[2]
                t0, t1, t2 = t
                o = g["P"]
                for j in range(0, len(P), 3):
                    x, y, z = P[j], P[j + 1], P[j + 2]
                    o.extend(
                        (
                            a00 * x + a01 * y + a02 * z + t0,
                            a10 * x + a11 * y + a12 * z + t1,
                            a20 * x + a21 * y + a22 * z + t2,
                        )
                    )
                if Nr is not None:
                    c00, c01, c02 = C[0]
                    c10, c11, c12 = C[1]
                    c20, c21, c22 = C[2]
                    o = g["N"]
                    for j in range(0, len(Nr), 3):
                        x, y, z = Nr[j], Nr[j + 1], Nr[j + 2]
                        u, v, w = (
                            c00 * x + c01 * y + c02 * z,
                            c10 * x + c11 * y + c12 * z,
                            c20 * x + c21 * y + c22 * z,
                        )
                        scale = sg / (math.sqrt(u * u + v * v + w * w) or 1.0)
                        o.extend((u * scale, v * scale, w * scale))
                else:
                    g["N"].extend([0.0, 0.0, 1.0] * nv)
                base = g["n"]
                o = g["I"]
                if sg > 0:
                    o.extend(i + base for i in idx_in)
                else:
                    for j in range(0, len(idx_in) - 2, 3):
                        o.extend((idx_in[j] + base, idx_in[j + 2] + base, idx_in[j + 1] + base))
                g["n"] += nv
        if not groups:
            continue
        prims = []
        lo3 = [math.inf] * 3
        hi3 = [-math.inf] * 3
        for mat, g in groups.items():
            P = g["P"]
            mn = [min(P[j::3]) for j in range(3)]
            mx = [max(P[j::3]) for j in range(3)]
            for j in range(3):
                lo3[j] = min(lo3[j], mn[j])
                hi3[j] = max(hi3[j], mx[j])
            pos = put(P, 34962, "VEC3", 5126, g["n"], mn, mx)
            nor = put(g["N"], 34962, "VEC3", 5126, g["n"])
            idx = g["I"] if g["n"] > 65535 else array.array("H", g["I"])
            ind = put(idx, 34963, "SCALAR", 5125 if idx.typecode == "I" else 5123, len(idx))
            tris += len(idx) // 3
            prim = dict(attributes=dict(POSITION=pos, NORMAL=nor), indices=ind)
            if mat >= 0:
                prim["material"] = mat
            prims.append(prim)
        bbox = [round(v, 4) for v in lo3 + hi3]
        zc = (lo3[2] + hi3[2]) / 2
        side = "B" if zmid is not None and zc < zmid else "F"
        if it["kind"] == "component":
            name, extras = it["name"], dict(kind="component", ref=it["name"], side=side, bbox=bbox)
            info["refs"][name] = dict(side=side, bbox=bbox)
        else:
            base = {
                "board_PCB": "board",
                "board_silkscreen": "silk",
                "board_soldermask": "mask",
            }.get(it["name"], it["name"].removeprefix("board_"))
            name = "__" + base + ("" if base == "board" else "_" + side)
            extras = dict(kind=base, side=side, bbox=bbox)
            info["parts"].append(name)
        while name in used_names:
            name += "+"
        used_names.add(name)
        out_meshes.append(dict(name=name, primitives=prims))
        out_nodes[0]["children"].append(len(out_nodes))
        out_nodes.append(dict(name=name, mesh=len(out_meshes) - 1, extras=extras))
    mats = []
    for m in js.get("materials", []):
        q = {
            k: m[k]
            for k in (
                "name",
                "pbrMetallicRoughness",
                "alphaMode",
                "alphaCutoff",
                "doubleSided",
                "emissiveFactor",
            )
            if k in m
        }
        q.get("pbrMetallicRoughness", {}).pop("baseColorTexture", None)
        q.get("pbrMetallicRoughness", {}).pop("metallicRoughnessTexture", None)
        mats.append(q)
    info["triangles"] = tris
    source_asset = js.get("asset", {})
    source_generator = source_asset.get("extras", {}).get("generator") or source_asset.get(
        "generator", "KiCad"
    )
    doc = dict(
        asset=dict(
            version="2.0",
            generator=f"yapnr viewer3d v{EXPORT_VERSION} (compacted from {source_generator})",
            extras=dict(
                schema=SCHEMA,
                frame="engine mm, x/y as the PCB canvas (y up), z up; 0 = board body bottom",
                board=info["board"],
            ),
        ),
        scene=0,
        scenes=[dict(nodes=[0])],
        nodes=out_nodes,
        meshes=out_meshes,
        materials=mats,
        accessors=accs,
        bufferViews=views,
        buffers=[dict(byteLength=len(out_bin))],
    )
    if not mats:
        doc.pop("materials")
    raw = json.dumps(doc, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 4)
    out_bin.extend(b"\0" * (-len(out_bin) % 4))
    glb = (
        struct.pack("<4sII", "glTF".encode(), 2, 12 + 8 + len(raw) + 8 + len(out_bin))
        + struct.pack("<II", len(raw), 0x4E4F534A)
        + raw
        + struct.pack("<II", len(out_bin), 0x004E4942)
        + bytes(out_bin)
    )
    return glb, info


# ------------------------------------------------------------------ job (subprocess)
def write_atomic(path, data):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def run_job(
    board,
    out,
    meta_path,
    cli,
    parts=None,
    timeout=240,
    raw_cap=512 << 20,
    max_bytes=64 << 20,
    sha=None,
    key=None,
    work=None,
):
    """Export + compact one board. Writes out (.glb), out.gz and, last, meta_path (its presence
    means ready). work: the scratch folder to create (the service names it, so it can remove exactly
    this one if it has to kill the job)."""
    try:
        os.nice(10)
    except OSError:
        pass
    t0 = time.time()
    data = Path(board).read_bytes()
    out = Path(out)
    meta_path = Path(meta_path)
    if sha and hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(
            "the board changed on disk after it was requested (its content no longer matches the checkpoint sha)"
        )
    text = data.decode("utf-8", "replace")
    if work:
        work = Path(work)
        work.mkdir()
    else:
        work = Path(tempfile.mkdtemp(prefix="v3d-", dir=out.parent))
    try:
        b = work / "board.kicad_pcb"
        b.write_text(rewrite_models(text, parts))
        raw = work / "raw.glb"
        print("stage export", flush=True)
        try:
            r = subprocess.run(
                [str(cli), "pcb", "export", "glb", "--force", *FLAGS, "-o", str(raw), str(b)],
                cwd=work,
                env=model_env(cli),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"kicad-cli timed out after {timeout:.0f} s")
        log = (r.stdout or "") + (r.stderr or "")
        if r.returncode or not raw.is_file():
            raise RuntimeError(
                f"kicad-cli exit {r.returncode}: "
                + " | ".join(
                    line
                    for line in log.strip().splitlines()[-6:]
                    if "NSCocoaErrorDomain" not in line
                )[:600]
            )
        size = raw.stat().st_size
        if size > raw_cap:
            raise RuntimeError(
                f"KiCad GLB too large ({size/1e6:.0f} MB > {raw_cap/1e6:.0f} MB cap)"
            )
        t1 = time.time()
        print("stage compact", flush=True)
        glb, info = compact(raw.read_bytes())
        if len(glb) > max_bytes:
            raise RuntimeError(
                f"compacted GLB too large ({len(glb)/1e6:.1f} MB > {max_bytes/1e6:.0f} MB cap)"
            )
        gz = gzip.compress(glb, 6)
        missing = [dict(ref=m[1], file=m[2].strip()) for m in MISSING.finditer(log)]
        refs = sorted(set(REFS.findall(text)))
        info.update(
            schema=SCHEMA,
            key=key,
            board_sha=sha,
            version=EXPORT_VERSION,
            flags=list(FLAGS),
            no_model=[x for x in refs if x not in info["refs"]],
            missing=missing[:200],
            raw_bytes=size,
            bytes=len(glb),
            gz_bytes=len(gz),
            export_s=round(t1 - t0, 2),
            compact_s=round(time.time() - t1, 2),
            created=time.time(),
        )
        write_atomic(out, glb)
        write_atomic(out.with_name(out.name + ".gz"), gz)
        write_atomic(meta_path, json.dumps(info, separators=(",", ":")).encode())
        print("stage done", flush=True)
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ service (server process)
class Viewer3DService:
    """Queue + cache of compacted GLBs under cache_dir, keyed by placement fingerprint. request()
    never blocks on an export: it enqueues (at most max_queue pending, newest first) and reports
    queued / exporting / ready / failed. The 3D pane polls every 1.5 s while it waits, so a key
    nobody polled for stale_s (12 s) is dropped before it starts and a running export nobody polled
    for abandon_s (20 s) is killed: flicking through checkpoints or lanes (or a viewer replaying its
    events after a restart) does not leave a trail of 2 GB exports behind. peek=True reports without
    enqueueing ('idle' when nothing is cached or running): the pane peeks first and enqueues once
    its board has stayed the same for a moment. One worker; a flock on lock_path caps exports across
    viewers. Several viewers may share cache_dir (prod and dev on one root): 'ready' is re-checked
    on disk (the other one may have evicted it), each removes only its own scratch folders, and
    leftovers (logs, failure records) are swept after retry_s."""

    def __init__(
        self,
        cache_dir,
        *,
        cli=None,
        parts=None,
        timeout=240,
        max_bytes=64 << 20,
        raw_cap=512 << 20,
        board_cap=64 << 20,
        max_files=48,
        max_total=768 << 20,
        retry_s=900,
        stale_s=12,
        abandon_s=20,
        max_queue=4,
        lock_path=None,
        python=None,
        unavailable=None,
    ):
        """cli: the kicad-cli to use (checked again here); None resolves it from the environment,
        unless ``unavailable`` already says why there is none."""
        self.cache = Path(cache_dir)
        self.parts = Path(parts) if parts else None
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.raw_cap = raw_cap
        self.board_cap = board_cap
        self.max_files = max_files
        self.max_total = max_total
        self.retry_s = retry_s
        self.stale_s = stale_s
        self.abandon_s = abandon_s
        self.max_queue = max_queue
        self.python = python or sys.executable
        self.lock_path = Path(lock_path) if lock_path else self.cache / "export.lock"
        try:
            if cli is None and unavailable:
                raise Unavailable(unavailable)
            self.cli = headless_cli(cli)
            self.env = export_env(self.cli)
            self.disabled = None
        except (Unavailable, OSError) as ex:
            self.cli = None
            self.env = None
            self.disabled = str(ex)
        self.jobs = {}
        self.keys = {}
        self.metas = {}
        self.digests = {}
        self.durations = []
        self.lock = threading.Condition()
        self.proc = None
        self.cur = None
        self.stopped = False
        self.worker = None
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sweep()

    def sweep(self, age=1800):
        """Leftovers older than age: scratch folders and board snapshots (v3d-*), *.tmp; logs and
        failure records older than retry_s (a failure record is ignored after that anyway; a running
        export is < timeout old)."""
        now = time.time()
        self.swept = now
        for pats, old in (
            (("v3d-*", "*.tmp"), age),
            (("*.log", "*.fail.json"), max(self.retry_s, self.timeout + 60)),
        ):
            for pat in pats:
                for p in self.cache.glob(pat):
                    try:
                        if now - p.stat().st_mtime > old:
                            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
                    except OSError:
                        pass

    def paths(self, key):
        return (
            self.cache / (key + ".glb"),
            self.cache / (key + ".json"),
            self.cache / (key + ".fail.json"),
            self.cache / (key + ".log"),
        )

    def key_of(self, psha, rels):
        """Cache key from a placement sha and the model files it references (their size/mtime digest
        cached for 5 s: the pane polls every 1.5 s)."""
        digest = ""
        if self.parts and rels:
            now = time.time()
            d = self.digests.get(rels)
            if d is None or now - d[0] > 5:
                if len(self.digests) > 256:
                    self.digests.clear()
                d = self.digests[rels] = (now, models_digest(self.parts, rels))
            digest = d[1]
        return key_of(psha, rels, self.parts, self.env, digest)

    def key_of_text(self, text):
        return self.key_of(placement_sha(text), model_rels(text))

    def key_for(self, sha, candidates):
        """(key, board path, bytes or None) for a board sha: the first candidate whose content
        hashes to sha. The content-addressed <sha>.kicad_pcb copy is remembered by sha; any other
        candidate (the lane's working board, which PnR rewrites) is re-read and re-hashed on every
        request, and its bytes come back so the export uses exactly them."""
        hit = self.keys.get(sha)
        if hit and hit[2].is_file():
            return self.key_of(hit[0], hit[1]), hit[2], None
        for c in candidates:
            if not c:
                continue
            p = Path(c)
            try:
                if not p.is_file():
                    continue
                if p.stat().st_size > self.board_cap:
                    raise Unavailable(
                        (
                            f"board too large for 3D export ({p.stat().st_size/1e6:.0f} MB > {self.board_cap/1e6:.0f} "
                            "MB cap)"
                        )
                    )
                data = p.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != sha:
                continue
            text = data.decode("utf-8", "replace")
            psha, rels = placement_sha(text), model_rels(text)
            if p.name != sha + ".kicad_pcb":
                return self.key_of(psha, rels), p, data
            if len(self.keys) > 4096:
                self.keys.clear()
            self.keys[sha] = (psha, rels, p)
            return self.key_of(psha, rels), p, None
        return None

    def meta(self, key, fresh=False):
        """The meta of a ready key, else None. A remembered meta is only trusted while its files
        exist: a viewer sharing this cache may have evicted them (or someone removed them);
        fresh=True re-reads it from disk."""
        glb, meta, _, _ = self.paths(key)
        m = None if fresh else self.metas.get(key)
        if not (glb.is_file() and meta.is_file()):
            self.metas.pop(key, None)
            return None
        if m is None:
            try:
                m = json.loads(meta.read_text())
            except (OSError, ValueError):
                return None
            if len(self.metas) > 64:
                self.metas.clear()
            self.metas[key] = m
        return m

    def drop(self, key):
        """Forget a job (lock held) and its board snapshot."""
        j = self.jobs.pop(key, None)
        if j and j.get("snap"):
            Path(j["snap"]).unlink(missing_ok=True)

    def request(self, sha, candidates=(), retry=False, peek=False):
        if not isinstance(sha, str) or not HEX64.fullmatch(sha):
            return dict(
                status="unavailable",
                sha=None,
                error="this checkpoint has no native board (placement preview)",
            )
        if self.disabled:
            return dict(status="unavailable", sha=sha, error=self.disabled)
        try:
            hit = self.key_for(sha, candidates)
        except Unavailable as ex:
            return dict(status="failed", sha=sha, error=str(ex))
        if hit is None:
            return dict(
                status="unavailable",
                sha=sha,
                error="the native board file for this checkpoint is not on disk",
            )
        key, board, data = hit
        m = self.meta(key, fresh=retry)
        if m:
            return dict(
                status="ready",
                sha=sha,
                key=key,
                url="/api/3d/glb/" + key,
                meta={k: v for k, v in m.items() if k != "missing"},
                missing=m.get("missing", [])[:40],
            )
        _, _, fail, _ = self.paths(key)
        now = time.time()
        with self.lock:
            job = self.jobs.get(key)
            if job and job["state"] == "done":
                self.drop(key)
                job = None  # finished, but its files are gone (evicted or removed): export again
            if job and job["state"] == "failed" and (retry or now - job["time"] > self.retry_s):
                self.drop(key)
                job = None
            if job is None and fail.is_file() and not retry:
                try:
                    f = json.loads(fail.read_text())
                except (OSError, ValueError):
                    f = None
                if f and now - f.get("time", 0) < self.retry_s:
                    return dict(
                        status="failed",
                        sha=sha,
                        key=key,
                        error=f.get("error"),
                        retry_after=round(f["time"] + self.retry_s - now),
                    )
            if job is None and peek and not retry:
                return dict(status="idle", sha=sha, key=key)
            if job is None:
                job = dict(
                    state="queued",
                    key=key,
                    sha=sha,
                    board=str(board),
                    queued=now,
                    seen=now,
                    stage="queued",
                )
                if (
                    data
                    is not None
                    # a mutable board (the lane's own file): export these very bytes, whatever PnR
                    # writes there meanwhile
                ):
                    snap = self.cache / f"v3d-src-{uuid.uuid4().hex[:16]}.kicad_pcb"
                    try:
                        write_atomic(snap, data)
                        job.update(board=str(snap), snap=str(snap))
                    except OSError as ex:
                        return dict(
                            status="failed",
                            sha=sha,
                            key=key,
                            error=f"cannot snapshot the board for export: {ex}",
                        )
                self.jobs[key] = job
                if self.worker is None:
                    self.worker = threading.Thread(target=self.work, daemon=True, name="viewer3d")
                    self.worker.start()
            job["seen"] = now
            self.lock.notify_all()
            if job["state"] == "failed":
                return dict(
                    status="failed",
                    sha=sha,
                    key=key,
                    error=job.get("error"),
                    retry_after=round(job["time"] + self.retry_s - now),
                )
            queued = sorted(
                (j for j in self.jobs.values() if j["state"] == "queued"), key=lambda j: -j["seen"]
            )
            out = dict(
                status="exporting" if job["state"] == "running" else "queued",
                sha=sha,
                key=key,
                stage=job["stage"],
                ahead=sum(1 for j in self.jobs.values() if j["state"] == "running" and j is not job)
                + (queued.index(job) if job in queued else 0),
                elapsed=round(now - job.get("started", now), 1),
                expected=(
                    round(sorted(self.durations)[len(self.durations) // 2], 1)
                    if self.durations
                    else None
                ),
            )
        return out

    def work(self):
        while not self.stopped:
            if time.time() - self.swept > 600:
                self.sweep()
            with self.lock:
                now = time.time()
                for k, j in list(self.jobs.items()):
                    if j["state"] == "queued" and now - j["seen"] > self.stale_s:
                        self.drop(k)  # nobody is looking at it any more
                    elif (
                        j["state"] in ("failed", "done") and now - j.get("time", now) > self.retry_s
                    ):
                        self.drop(k)
                queued = sorted(
                    (j for j in self.jobs.values() if j["state"] == "queued"),
                    key=lambda j: -j["seen"],
                )
                for j in queued[self.max_queue :]:
                    self.drop(j["key"])
                if not queued:
                    self.lock.wait(5)
                    continue
                job = queued[0]
                job.update(state="running", started=now, stage="waiting for another export")
            self.run(job)

    def run(self, job):
        key = job["key"]
        glb, meta, fail, logf = self.paths(key)
        self.cur = job
        try:
            with open(self.lock_path, "a") as lk:
                while True:  # machine-wide: the viewers under one experiment folder share this lock
                    try:
                        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if self.stopped:
                            return
                        if (
                            time.time() - job["seen"] > self.stale_s
                        ):  # nobody waits for it any more (another viewer's export held the lock)
                            with self.lock:
                                self.jobs.pop(key, None)
                            return
                        time.sleep(1)
                try:
                    if self.meta(key):
                        job.update(state="done", time=time.time())
                        return
                    job.update(
                        stage="export",
                        started=time.time(),
                        work=str(self.cache / ("v3d-" + uuid.uuid4().hex[:16])),
                    )
                    fail.unlink(missing_ok=True)  # a (re)try supersedes the recorded failure
                    cmd = [
                        self.python,
                        "-m",
                        JOB,
                        "export",
                        "--board",
                        job["board"],
                        "--out",
                        str(glb),
                        "--meta",
                        str(meta),
                        "--cli",
                        str(self.cli),
                        "--timeout",
                        str(max(10, self.timeout - 15)),
                        "--max-bytes",
                        str(self.max_bytes),
                        "--raw-cap",
                        str(self.raw_cap),
                        "--sha",
                        job["sha"],
                        "--key",
                        key,
                        "--work",
                        job["work"],
                    ] + (["--parts", str(self.parts)] if self.parts else [])
                    with open(logf, "w") as log:
                        p = self.proc = subprocess.Popen(
                            cmd,
                            env=viewer_runtime.hermetic_env(threads=False),
                            stdout=subprocess.PIPE,
                            stderr=log,
                            text=True,
                            start_new_session=True,
                        )

                        def read():
                            for line in p.stdout:
                                if line.startswith("stage "):
                                    job["stage"] = line[6:].strip()

                        rd = threading.Thread(target=read, daemon=True)
                        rd.start()
                        end = time.time() + self.timeout
                        code = None
                        try:
                            while code is None:
                                try:
                                    code = p.wait(timeout=1)
                                except subprocess.TimeoutExpired:
                                    if time.time() > end:
                                        self.kill()
                                        raise RuntimeError(
                                            f"3D export timed out after {self.timeout:.0f} s"
                                        )
                                    if (
                                        time.time() - job["seen"] > self.abandon_s
                                    ):  # nobody looks at this board any more: free the CPU and the 2 GB
                                        self.kill()
                                        with self.lock:
                                            self.jobs.pop(key, None)
                                        logf.unlink(missing_ok=True)
                                        return
                        finally:
                            self.proc = None
                            rd.join(2)
                            try:
                                p.stdout.close()
                            except OSError:
                                pass
                    if self.stopped:
                        logf.unlink(missing_ok=True)
                        return
                    m = self.meta(key, fresh=True)
                    if code or not m:
                        tail = [
                            line
                            for line in logf.read_text(errors="replace").strip().splitlines()
                            if line.strip()
                        ][-3:]
                        raise RuntimeError(
                            (tail[-1] if tail else f"export exited with {code}")[:600]
                        )
                    self.durations = (self.durations + [time.time() - job["started"]])[-9:]
                    job.update(state="done", time=time.time())
                    fail.unlink(missing_ok=True)
                    logf.unlink(missing_ok=True)
                    self.evict(keep=key)
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)
        except Exception as ex:
            err = f"{ex}" if isinstance(ex, RuntimeError) else f"{type(ex).__name__}: {ex}"
            job.update(state="failed", error=err[:600], time=time.time())
            try:
                write_atomic(
                    fail,
                    json.dumps(dict(error=err[:600], time=time.time(), sha=job["sha"])).encode(),
                )
            except OSError:
                pass
        finally:
            self.cur = None
            if job.get("snap"):
                Path(job["snap"]).unlink(missing_ok=True)

    def evict(self, keep=None):
        """LRU by mtime (serving touches it): at most max_files GLBs and max_total bytes. Tolerates
        files vanishing under it (another viewer sharing the cache evicts too)."""
        files = []
        for f in self.cache.glob("*.glb"):
            try:
                files.append((f.stat().st_mtime, f))
            except OSError:
                pass
        files.sort(reverse=True)
        total = 0
        for i, (_, f) in enumerate(files):
            k = f.stem
            size = 0
            for p in (f, f.with_name(f.name + ".gz")):
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
            total += size
            if k != keep and (i >= self.max_files or total > self.max_total):
                for p in (
                    self.cache / (k + ".json"),
                    f,
                    f.with_name(f.name + ".gz"),
                    self.cache / (k + ".log"),
                ):
                    p.unlink(missing_ok=True)  # the meta first: 'ready' goes away before the GLB
                self.metas.pop(k, None)
                total -= size

    def glb(self, key, gzip_ok=False):
        """(bytes, content-encoding or None) for a ready key; FileNotFoundError otherwise (then the
        key is no longer 'ready' here either: the next request exports it again)."""
        if not isinstance(key, str) or not HEX64.fullmatch(key):
            raise ValueError("invalid key")
        f, meta, _, _ = self.paths(key)
        z = f.with_name(f.name + ".gz")
        try:
            if not meta.is_file():
                raise FileNotFoundError("no 3D model for this key (yet)")
            try:
                os.utime(f)  # LRU
            except FileNotFoundError:
                raise
            except OSError:
                pass
            if gzip_ok and z.is_file():
                return z.read_bytes(), "gzip"
            return f.read_bytes(), None
        except FileNotFoundError:
            self.metas.pop(key, None)
            raise

    def kill(self):
        """Kill the running export's process group and remove its scratch folder (only that one:
        another viewer sharing this cache may be exporting into its own)."""
        p, job = self.proc, self.cur
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if job and job.get("work"):
            shutil.rmtree(job["work"], ignore_errors=True)

    def shutdown(self):
        """Server stop: kill a running export (process group + scratch folder) and drop its log (the
        server exits right after, before the worker thread could)."""
        job = self.cur
        self.stopped = True
        self.kill()
        if job:
            self.paths(job["key"])[3].unlink(missing_ok=True)
        with self.lock:
            self.lock.notify_all()

    def status(self):
        with self.lock:
            return dict(
                available=not self.disabled,
                reason=self.disabled,
                cli=str(self.cli) if self.cli else None,
                parts=str(self.parts) if self.parts else None,
                env=self.env,
                jobs=[
                    {k: j.get(k) for k in ("key", "sha", "state", "stage", "error")}
                    for j in self.jobs.values()
                ],
            )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="(job) export + compact one board")
    e.add_argument("--board", required=True, type=Path)
    e.add_argument("--out", required=True, type=Path)
    e.add_argument("--meta", required=True, type=Path)
    e.add_argument("--cli")
    e.add_argument("--parts", type=Path)
    e.add_argument("--timeout", type=float, default=240)
    e.add_argument("--max-bytes", type=int, default=64 << 20)
    e.add_argument("--raw-cap", type=int, default=512 << 20)
    e.add_argument("--sha")
    e.add_argument("--key")
    e.add_argument("--work", type=Path)
    c = sub.add_parser("compact", help="compact an existing KiCad GLB")
    c.add_argument("glb", type=Path)
    c.add_argument("out", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "compact":
        glb, info = compact(a.glb.read_bytes())
        a.out.write_bytes(glb)
        print(
            json.dumps(
                {k: v for k, v in info.items() if k != "refs"}
                | dict(refs=len(info["refs"]), bytes=len(glb))
            )
        )
        return 0
    try:
        cli = headless_cli(a.cli)
        run_job(
            a.board,
            a.out,
            a.meta,
            cli,
            a.parts,
            a.timeout,
            a.raw_cap,
            a.max_bytes,
            a.sha,
            a.key,
            a.work,
        )
        return 0
    except Exception as ex:
        print(
            (
                f"{ex}"
                if isinstance(ex, (RuntimeError, Unavailable))
                else f"{type(ex).__name__}: {ex}"
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
