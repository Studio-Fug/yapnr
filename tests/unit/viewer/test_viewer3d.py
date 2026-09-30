"""The 3D view: placement cache keys (routing-only revisions share one; untented vias, the CLI / 3D
library and project model files change it), model-path rewrite, GLB compaction into the viewer
frame, queue / time / size bounds, failure caching, one export at a time (also across two services
sharing a lock), two services sharing one cache folder (eviction by the other one, only its own
scratch folder removed), mutable boards exported from a snapshot, leftover sweep, eviction, SIGTERM
during an export, the Host guard, and that the GUI KiCad CLI (or any non-background app bundle) is
never used. Offline: a fake kicad-cli writes a synthetic KiCad-like GLB. The real headless export is
in tests/e2e/viewer."""

import array
import gzip
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from yapnr.viewer.services import viewer3d as v
from yapnr.viewer.testing import start_viewer, stop, write_fake

BOARD = """(kicad_pcb
\t(version 20241229)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t)
\t(setup
\t\t(tenting
\t\t\t(front yes)
\t\t\t(back yes)
\t\t)
\t)
\t(footprint "R0402"
\t\t(at 11 21)
\t\t(property "Reference" "R1")
\t\t(model "${KIPRJMOD}/../../src/parts/Res/R0402.step")
\t)
\t(footprint "TP"
\t\t(at 30 20)
\t\t(property "Reference" "TP1")
\t\t(model "${KIPRJMOD}/../../src/parts/Nope/TP.step")
\t)
\t(segment
\t\t(start 1 2)
\t\t(end 3 4)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t)
\t(via
\t\t(at 5 5)
\t\t(size 0.45)
\t)
\t(zone
\t\t(net 1)
\t\t(layer "In1.Cu")
\t)
)
"""
ROUTED = (
    BOARD.replace("(start 1 2)", "(start 7 8)")
    .replace("(at 5 5)", "(at 6 6)")
    .replace("\t(zone\n", "\t(arc\n\t\t(start 0 0)\n\t)\n\t(zone\n")
)
MOVED = BOARD.replace("(at 11 21)", "(at 12 21)")
UNTENTED = BOARD.replace("\t\t\t(back yes)", "\t\t\t(back no)")
GUI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def box(x0, y0, z0, x1, y1, z1):
    """Positions/normals/indices of an axis-aligned box (glTF units, metres)."""
    P = []
    N = []
    IDX = []
    for axis in range(3):
        for s in (0, 1):
            n = [0, 0, 0]
            n[axis] = 1 if s else -1
            quad = []
            for a in (0, 1):
                for b in (0, 1):
                    p = [0, 0, 0]
                    p[axis] = (x0, y0, z0)[axis] if not s else (x1, y1, z1)[axis]
                    o = [k for k in range(3) if k != axis]
                    p[o[0]] = (x0, y0, z0)[o[0]] if not a else (x1, y1, z1)[o[0]]
                    p[o[1]] = (x0, y0, z0)[o[1]] if not b else (x1, y1, z1)[o[1]]
                    quad.append(p)
            k = len(P)
            P += quad
            N += [n] * 4
            IDX += [k, k + 1, k + 3, k, k + 3, k + 2]
    return P, N, IDX


def kicad_like_glb():
    """Unnamed root -> R1 (translated, with a nested translated child like KiCad's VRML shells),
    board_PCB, two silkscreens."""
    js = dict(
        asset=dict(version="2.0", generator="test"),
        scene=0,
        scenes=[dict(nodes=[0])],
        nodes=[],
        meshes=[],
        accessors=[],
        bufferViews=[],
        buffers=[],
        materials=[
            dict(name="m0", pbrMetallicRoughness=dict(baseColorFactor=[0.1, 0.1, 0.1, 1])),
            dict(
                name="fr4",
                alphaMode="BLEND",
                pbrMetallicRoughness=dict(baseColorFactor=[0.4, 0.45, 0.3, 0.98]),
            ),
        ],
    )
    blob = bytearray()

    def mesh(name, P, N, IDX, mat):
        acc = []
        for data, fmt, kind, ct, target in (
            (sum(P, []), "f", "VEC3", 5126, 34962),
            (sum(N, []), "f", "VEC3", 5126, 34962),
            (IDX, "H", "SCALAR", 5123, 34963),
        ):
            blob.extend(b"\0" * (-len(blob) % 4))
            a = array.array(fmt, data)
            js["bufferViews"].append(
                dict(buffer=0, byteOffset=len(blob), byteLength=len(a) * a.itemsize, target=target)
            )
            blob.extend(a.tobytes())
            d = dict(
                bufferView=len(js["bufferViews"]) - 1,
                componentType=ct,
                count=len(data) // (3 if kind == "VEC3" else 1),
                type=kind,
            )
            if kind == "VEC3" and target == 34962 and not acc:
                d.update(
                    min=[min(p[k] for p in P) for k in range(3)],
                    max=[max(p[k] for p in P) for k in range(3)],
                )
            js["accessors"].append(d)
            acc.append(len(js["accessors"]) - 1)
        js["meshes"].append(
            dict(
                name=name,
                primitives=[
                    dict(
                        attributes=dict(POSITION=acc[0], NORMAL=acc[1]),
                        indices=acc[2],
                        material=mat,
                    )
                ],
            )
        )
        return len(js["meshes"]) - 1

    # R1 body: 1 x 0.5 x 0.5 mm box around its origin; a second shell nested 0.2 mm higher
    r1 = mesh("R0402", *box(-0.0005, 0, -0.00025, 0.0005, 0.0005, 0.00025), 0)
    r1b = mesh("R0402 shell", *box(-0.0001, 0, -0.0001, 0.0001, 0.0001, 0.0001), 0)
    board = mesh("board_PCB", *box(0.010, 0, 0.010, 0.040, 0.00151, 0.030), 1)
    silk_f = mesh("board_silkscreen", *box(0.012, 0.001585, 0.012, 0.013, 0.001585, 0.013), 0)
    silk_b = mesh("board_silkscreen", *box(0.012, -0.000075, 0.012, 0.013, -0.000075, 0.013), 0)
    N = js["nodes"]
    N += [
        dict(children=[1, 4, 6, 7, 8]),
        dict(
            name="R1",
            translation=[0.011, 0.001595, 0.021],
            rotation=[0, 0.7071067811865476, 0, 0.7071067811865476],
            children=[2],
        ),
        dict(name="=>[0:1:1:3]", children=[3], mesh=r1),
        dict(name="R0402 shell", translation=[0, 0.0002, 0], mesh=r1b),
        dict(name="=>[0:1:1:9]", children=[5]),
        dict(name="=>[0:1:1:10]", mesh=board),
        dict(name="=>[0:1:1:11]", mesh=silk_f),
        dict(name="=>[0:1:1:12]", mesh=silk_b),
        dict(name="TP9", children=[]),
    ]
    js["buffers"] = [dict(byteLength=len(blob))]
    blob.extend(b"\0" * (-len(blob) % 4))
    raw = json.dumps(js).encode()
    raw += b" " * (-len(raw) % 4)
    return (
        struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(raw) + 8 + len(blob))
        + struct.pack("<II", len(raw), 0x4E4F534A)
        + raw
        + struct.pack("<II", len(blob), 0x004E4942)
        + bytes(blob)
    )


FAKE = """import json,os,shutil,sys,time
a=sys.argv[1:];out=a[a.index('-o')+1];board=a[-1]
with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps(dict(argv=a,start=time.time(),pid=os.getpid(),cwd=os.getcwd()))+'\\n')
if os.environ.get('FAKE_COPY'):shutil.copy(board,os.environ['FAKE_COPY'])
time.sleep(float(os.environ.get('FAKE_SLEEP','0')))
sys.stderr.write('Could not add 3D model for TP1.\\nFile not found: ${{KIPRJMOD}}/../../src/parts/Nope/TP.step\\n')
if int(os.environ.get('FAKE_EXIT','0')):sys.stderr.write('fake failure\\n');sys.exit(int(os.environ['FAKE_EXIT']))
if os.environ.get('FAKE_JUNK'):open(out,'wb').write(b'not a glb')
else:shutil.copy(os.environ['FAKE_GLB'],out)
with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps(dict(end=time.time(),pid=os.getpid()))+'\\n')
"""  # noqa: E501 (one fake program)


def sha(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def wait(fn, timeout=40, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(step)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3d-test-")
        self.d = Path(self.tmp.name)
        (self.d / "bin").mkdir()
        self.cli = write_fake(self.d / "bin/kicad-cli", FAKE.format())
        (self.d / "parts/Res").mkdir(parents=True)
        (self.d / "parts/Res/R0402.step").write_text("step")
        (self.d / "boards").mkdir()
        self.glb = self.d / "fixture.glb"
        self.glb.write_bytes(kicad_like_glb())
        self.env = {
            k: os.environ.get(k)
            for k in (
                "FAKE_LOG",
                "FAKE_GLB",
                "FAKE_SLEEP",
                "FAKE_EXIT",
                "FAKE_COPY",
                "FAKE_JUNK",
                "PNR_KICAD_CLI",
                "YAPNR_KICAD_CLI",
            )
        }
        os.environ.update(FAKE_LOG=str(self.d / "calls.jsonl"), FAKE_GLB=str(self.glb))
        for k in (
            "FAKE_SLEEP",
            "FAKE_EXIT",
            "FAKE_COPY",
            "FAKE_JUNK",
            "PNR_KICAD_CLI",
            "YAPNR_KICAD_CLI",
        ):
            os.environ.pop(k, None)
        self.services = []

    def tearDown(self):
        for s in self.services:
            s.shutdown()
        for k, val in self.env.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self.tmp.cleanup()

    def board(self, text):
        s = sha(text)
        p = self.d / "boards" / (s + ".kicad_pcb")
        p.write_text(text)
        return s, p

    def service(self, **kw):
        kw.setdefault("cli", self.cli)
        kw.setdefault("parts", self.d / "parts")
        s = v.Viewer3DService(self.d / "cache", **kw)
        self.services.append(s)
        return s

    def calls(self):
        f = self.d / "calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def ready(self, s, board_sha, path, **kw):
        r = wait(
            lambda: (lambda r: r if r["status"] in ("ready", "failed", "unavailable") else None)(
                s.request(board_sha, [path], **kw)
            )
        )
        wait(
            lambda: all(j["state"] != "running" for j in list(s.jobs.values()))
        )  # the job's meta appears just before the worker's cleanup
        return r


class KeysAndRewrite(unittest.TestCase):
    def test_placement_key_ignores_routing_only(self):
        parts = Path("/p")
        self.assertEqual(v.cache_key(BOARD, parts), v.cache_key(ROUTED, parts))
        self.assertNotEqual(v.cache_key(BOARD, parts), v.cache_key(MOVED, parts))
        self.assertNotEqual(v.cache_key(BOARD, parts), v.cache_key(BOARD, Path("/other")))
        self.assertNotIn("(segment", v.placement_text(BOARD))
        self.assertNotIn("(via", v.placement_text(BOARD))
        self.assertIn("R0402", v.placement_text(BOARD))
        k, old = v.cache_key(BOARD, parts), v.EXPORT_VERSION
        try:
            v.EXPORT_VERSION = old + 1
            self.assertNotEqual(v.cache_key(BOARD, parts), k)
        finally:
            v.EXPORT_VERSION = old
        self.assertEqual(
            v.placement_text("(kicad_pcb)"), "(kicad_pcb)"
        )  # unformatted text: hashed whole

    def test_untented_vias_and_mask_zones_stay_in_the_key(self):
        # --include-soldermask: an untented via opens the mask, which the GLB shows; so does a zone
        # on a mask layer
        parts = Path("/p")

        def moved_via(t):
            return t.replace("(at 5 5)", "(at 6 6)")

        self.assertEqual(v.cache_key(BOARD, parts), v.cache_key(moved_via(BOARD), parts))
        self.assertNotEqual(v.cache_key(UNTENTED, parts), v.cache_key(moved_via(UNTENTED), parts))
        self.assertIn("(via", v.placement_text(UNTENTED))
        self.assertNotIn("(segment", v.placement_text(UNTENTED))
        no_setup = BOARD.replace(
            "\t(setup\n\t\t(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)\n\t)\n", ""
        )
        self.assertNotIn("(setup", no_setup)
        self.assertIn("(via", v.placement_text(no_setup))  # unknown tenting: kept (safe side)
        self.assertIn(
            "(via",
            v.placement_text(
                BOARD.replace(
                    "(size 0.45)",
                    "(size 0.45)\n\t\t(tenting\n\t\t\t(front no)\n\t\t\t(back yes)\n\t\t)",
                )
            ),
        )  # its own override
        self.assertEqual(
            v.placement_text(
                BOARD.replace(
                    "(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)", "(tenting front back)"
                )
            ).count("(via"),
            0,
        )  # KiCad 8 syntax
        mask = BOARD.replace('(layer "In1.Cu")', '(layer "F.Mask")')
        self.assertIn("(zone", v.placement_text(mask))
        self.assertNotIn("(zone", v.placement_text(BOARD))

    def test_key_covers_cli_library_and_model_files(self):
        with tempfile.TemporaryDirectory() as t:
            parts = Path(t) / "parts"
            (parts / "Res").mkdir(parents=True)
            f = parts / "Res/R0402.step"
            f.write_text("step")
            env = dict(
                cli="/x/kicad-cli",
                size=1,
                mtime=1,
                version="10.0.6",
                libs={"KICAD10_3DMODEL_DIR": ["/lib", 1]},
            )
            k = v.cache_key(BOARD, parts, env)
            self.assertNotEqual(k, v.cache_key(BOARD, parts, dict(env, version="10.0.7")))
            self.assertNotEqual(k, v.cache_key(BOARD, parts, dict(env, cli="/y/kicad-cli")))
            self.assertNotEqual(
                k, v.cache_key(BOARD, parts, dict(env, libs={"KICAD10_3DMODEL_DIR": ["/other", 1]}))
            )
            f.write_text("step, edited")
            os.utime(f, (1e9, 1e9))
            self.assertNotEqual(k, v.cache_key(BOARD, parts, env))  # same name, new content
            k2 = v.cache_key(BOARD, parts, env)
            (parts / "Nope").mkdir()
            (parts / "Nope/TP.step").write_text("x")
            self.assertNotEqual(k2, v.cache_key(BOARD, parts, env))  # a missing model appears
            self.assertEqual(v.cache_key(BOARD, parts, env), v.cache_key(ROUTED, parts, env))

    def test_model_paths_point_at_the_parts_folder_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "Res").mkdir()
            (Path(t) / "Res/R0402.step").write_text("x")
            out = v.rewrite_models(BOARD, Path(t))
            self.assertIn(f'(model "{t}/Res/R0402.step")', out)
            self.assertIn(
                '"${KIPRJMOD}/../../src/parts/Nope/TP.step"', out
            )  # missing file: untouched (KiCad reports it)
            self.assertEqual(v.rewrite_models(BOARD, Path(t + '/a"b')), BOARD)
            self.assertEqual(v.rewrite_models(BOARD, None), BOARD)


class Compaction(unittest.TestCase):
    def test_kicad_glb_to_engine_frame(self):
        glb, info = v.compact(kicad_like_glb())
        js, binary = v.read_glb(glb)
        names = [n["name"] for n in js["nodes"]]
        self.assertEqual(names[0], "board")
        self.assertEqual(
            set(names[1:]), {"R1", "__board", "__silk_F", "__silk_B"}
        )  # TP9 has no mesh
        self.assertAlmostEqual(info["board"]["width"], 30, 6)
        self.assertAlmostEqual(info["board"]["height"], 20, 6)
        self.assertAlmostEqual(info["board"]["z1"], 1.51, 6)
        r1 = info["refs"]["R1"]
        b = r1["bbox"]
        # KiCad (11, 21) mm -> engine x = 11-10, y = 30-21 (Edge.Cuts bbox bottom-left, y up); body
        # 0.5 mm tall from 1.595 mm
        self.assertAlmostEqual((b[0] + b[3]) / 2, 1, 3)
        self.assertAlmostEqual((b[1] + b[4]) / 2, 9, 3)
        self.assertAlmostEqual(b[2], 1.595, 3)
        self.assertAlmostEqual(b[5], 2.095, 3)
        self.assertAlmostEqual(b[3] - b[0], 0.5, 3)
        self.assertAlmostEqual(b[4] - b[1], 1.0, 3)  # rotated 90 degrees about the board normal
        self.assertEqual(r1["side"], "F")
        node = next(n for n in js["nodes"] if n["name"] == "R1")
        self.assertEqual(node["extras"]["ref"], "R1")
        self.assertEqual(
            len(js["meshes"][node["mesh"]]["primitives"]), 1
        )  # nested shell merged into one primitive per material
        silk = {
            n["name"]: n["extras"]["bbox"]
            for n in js["nodes"][1:]
            if n["name"].startswith("__silk")
        }
        self.assertGreater(silk["__silk_F"][2], 1.5)
        self.assertLess(silk["__silk_B"][2], 0)
        for m in js["meshes"]:
            for p in m["primitives"]:
                nr = v.accessor(js, binary, p["attributes"]["NORMAL"])
                for k in range(0, len(nr), 3):
                    self.assertAlmostEqual(math.hypot(nr[k], nr[k + 1], nr[k + 2]), 1, 4)
                self.assertIn("min", js["accessors"][p["attributes"]["POSITION"]])
        self.assertEqual(len(js["materials"]), 2)
        self.assertTrue(json.dumps(js["asset"]["extras"]))

    def test_rejects_non_glb(self):
        with self.assertRaises(ValueError):
            v.compact(b"not a glb at all")


class Headless(Base):
    def test_gui_cli_is_refused_directly_via_symlink_and_env(self):
        for p in (
            GUI,
            "/Applications/KiCad/kicad-cli",
            str(self.d / "KiCad.app/Contents/MacOS/kicad-cli"),
        ):
            with self.assertRaises(v.Unavailable):
                v.headless_cli(p)
        link = self.d / "bin/kicad-link"
        link.symlink_to(GUI)
        with self.assertRaises(v.Unavailable):
            v.headless_cli(link)
        os.environ["PNR_KICAD_CLI"] = GUI
        s = v.Viewer3DService(self.d / "c2", parts=self.d / "parts")
        self.services.append(s)
        self.assertIn("refusing the GUI KiCad", s.disabled)
        bs, bp = self.board(BOARD)
        r = s.request(bs, [bp])
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("refusing", r["error"])
        self.assertEqual(s.worker, None)  # nothing was ever started

    def test_app_bundles_must_be_background_only(self):
        import plistlib

        def bundle(name, info):
            b = self.d / name
            (b / "Contents/MacOS").mkdir(parents=True)
            (b / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            c = b / "Contents/MacOS/kicad-cli"
            c.write_text(self.cli.read_text())
            c.chmod(0o755)
            return c

        gui = bundle(
            "KiCad-10.app", dict(CFBundleIdentifier="org.kicad.kicad")
        )  # a renamed GUI bundle
        with self.assertRaises(v.Unavailable) as ex:
            v.headless_cli(gui)
        self.assertIn("not a background-only app bundle", str(ex.exception))
        link = self.d / "bin/kicad-renamed"
        link.symlink_to(gui)
        with self.assertRaises(v.Unavailable):
            v.headless_cli(link)
        self.assertEqual(
            v.headless_cli(
                bundle("KiCad-hl.app", dict(LSBackgroundOnly=True, CFBundleVersion="10.0.6"))
            ).name,
            "kicad-cli",
        )
        self.assertEqual(
            v.headless_cli(bundle("KiCad-ui.app", dict(LSUIElement="1"))).name, "kicad-cli"
        )
        self.assertEqual(
            v.export_env(self.d / "KiCad-hl.app/Contents/MacOS/kicad-cli")["version"], "10.0.6"
        )
        s = v.Viewer3DService(self.d / "c3", cli=gui, parts=self.d / "parts")
        self.services.append(s)
        self.assertIn("background-only", s.disabled)
        headless = v.HEADLESS_CLI.expanduser()
        if headless.is_file():  # a machine's headless copy passes
            self.assertTrue(v.background_only(v.bundle_info(v.app_bundle(headless))))

    def test_default_is_the_headless_copy(self):
        os.environ.pop("PNR_KICAD_CLI", None)
        os.environ.pop("YAPNR_KICAD_CLI", None)
        self.assertEqual(
            v.HEADLESS_CLI, Path("~/Applications/KiCad-headless.app/Contents/MacOS/kicad-cli")
        )
        headless = v.HEADLESS_CLI.expanduser()
        if sys.platform == "darwin" and headless.is_file():
            self.assertEqual(v.headless_cli(), headless)
        elif sys.platform == "darwin":
            with self.assertRaisesRegex(v.Unavailable, "no headless kicad-cli"):
                v.headless_cli()
        with mock.patch.object(
            v.kicad_cli.__globals__["sys"], "platform", "linux"
        ), mock.patch.dict(os.environ, {"PATH": str(self.cli.parent)}):
            self.assertEqual(v.headless_cli(), self.cli)  # on Linux: kicad-cli on PATH
        # the environment wins, YAPNR_KICAD_CLI first, PNR_KICAD_CLI as its alias
        os.environ["PNR_KICAD_CLI"] = str(self.cli)
        self.assertEqual(v.headless_cli(), self.cli)
        os.environ["YAPNR_KICAD_CLI"] = GUI
        with self.assertRaisesRegex(v.Unavailable, "refusing the GUI KiCad"):
            v.headless_cli()

    def test_job_refuses_gui_cli(self):
        bs, bp = self.board(BOARD)
        rc = v.main(
            [
                "export",
                "--board",
                str(bp),
                "--out",
                str(self.d / "o.glb"),
                "--meta",
                str(self.d / "o.json"),
                "--cli",
                GUI,
            ]
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.d / "o.json").exists())
        self.assertEqual(self.calls(), [])


class Service(Base):
    def test_export_ready_meta_gzip_and_routing_revisions_share_it(self):
        os.environ["FAKE_COPY"] = str(self.d / "copy.kicad_pcb")
        s = self.service()
        bs, bp = self.board(BOARD)
        t = time.time()
        first = s.request(bs, [None, self.d / "missing.kicad_pcb", bp])
        self.assertLess(time.time() - t, 0.5)
        self.assertIn(first["status"], ("queued", "exporting"))
        r = self.ready(s, bs, bp)
        self.assertEqual(r["status"], "ready", r)
        self.assertEqual(r["url"], "/api/3d/glb/" + r["key"])
        self.assertEqual(r["key"], s.key_of_text(BOARD))
        self.assertEqual(s.env["cli"], str(self.cli.resolve()))
        self.assertEqual(r["key"], v.cache_key(BOARD, self.d / "parts", s.env))
        m = r["meta"]
        self.assertEqual(m["no_model"], ["TP1"])
        self.assertEqual(r["missing"][0]["ref"], "TP1")
        self.assertEqual(m["board_sha"], bs)
        self.assertIn("R1", m["refs"])
        call = self.calls()[0]
        a = call["argv"]
        self.assertEqual(a[:3], ["pcb", "export", "glb"])
        self.assertTrue(set(v.FLAGS) <= set(a))
        self.assertNotEqual(a[-1], str(bp))  # a scratch copy, never the original
        self.assertIn(str(self.d / "parts/Res/R0402.step"), (self.d / "copy.kicad_pcb").read_text())
        raw, enc = s.glb(r["key"])
        self.assertIsNone(enc)
        self.assertEqual(raw[:4], b"glTF")
        gz, enc = s.glb(r["key"], gzip_ok=True)
        self.assertEqual(enc, "gzip")
        self.assertEqual(gzip.decompress(gz), raw)
        self.assertEqual(
            sorted(
                p.name for p in (self.d / "cache").iterdir() if not p.name.startswith("export.lock")
            ),
            sorted([r["key"] + ".glb", r["key"] + ".glb.gz", r["key"] + ".json"]),
        )
        rs, rp = self.board(ROUTED)
        r2 = s.request(rs, [rp])
        self.assertEqual(r2["status"], "ready")
        self.assertEqual(r2["key"], r["key"])
        self.assertEqual(len([c for c in self.calls() if "argv" in c]), 1)
        with self.assertRaises(ValueError):
            s.glb("../etc")
        with self.assertRaises(FileNotFoundError):
            s.glb("0" * 64)

    def test_unavailable_inputs(self):
        s = self.service()
        bs, bp = self.board(BOARD)
        self.assertEqual(s.request("nope", [bp])["status"], "unavailable")
        self.assertEqual(s.request(None)["status"], "unavailable")
        self.assertEqual(
            s.request("f" * 64, [bp])["status"], "unavailable"
        )  # content does not hash to the sha
        self.assertEqual(s.request(bs, [self.d / "missing"])["status"], "unavailable")
        small = self.service(board_cap=100)
        r = small.request(bs, [bp])
        self.assertEqual(r["status"], "failed")
        self.assertIn("too large", r["error"])
        self.assertEqual(self.calls(), [])

    def test_cli_failure_is_cached_until_retry(self):
        os.environ["FAKE_EXIT"] = "3"
        s = self.service()
        bs, bp = self.board(BOARD)
        r = self.ready(s, bs, bp)
        self.assertEqual(r["status"], "failed")
        self.assertIn("kicad-cli exit 3", r["error"])
        self.assertIn("fake failure", r["error"])
        n = len(self.calls())
        time.sleep(0.3)
        self.assertEqual(s.request(bs, [bp])["status"], "failed")
        time.sleep(0.5)
        self.assertEqual(len(self.calls()), n)
        s2 = self.service()
        self.assertEqual(
            s2.request(bs, [bp])["status"], "failed"
        )  # persisted: a restarted viewer does not re-run it
        os.environ.pop("FAKE_EXIT")
        r = s.request(bs, [bp], retry=True)
        self.assertIn(r["status"], ("queued", "exporting"))
        self.assertEqual(self.ready(s, bs, bp)["status"], "ready")
        self.assertFalse(any((self.d / "cache").glob("*.fail.json")))

    def test_timeout_kills_the_export_process_group(self):
        os.environ["FAKE_SLEEP"] = "60"
        s = self.service(timeout=3)
        bs, bp = self.board(BOARD)
        r = self.ready(s, bs, bp)
        self.assertEqual(r["status"], "failed")
        self.assertIn("timed out", r["error"])
        pid = self.calls()[0]["pid"]

        def gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True

        wait(gone, 10)

    def test_size_caps(self):
        s = self.service(max_bytes=200)
        bs, bp = self.board(BOARD)
        r = self.ready(s, bs, bp)
        self.assertEqual(r["status"], "failed")
        self.assertIn("too large", r["error"])
        os.environ["FAKE_JUNK"] = "1"
        s2 = v.Viewer3DService(self.d / "cache2", cli=self.cli, parts=self.d / "parts")
        self.services.append(s2)
        r = self.ready(s2, bs, bp)
        self.assertEqual(r["status"], "failed")
        self.assertIn("glTF", r["error"])

    def test_one_export_at_a_time_across_services_sharing_the_lock(self):
        os.environ["FAKE_SLEEP"] = "1.2"
        lock = self.d / "shared.lock"
        a = self.service(lock_path=lock)
        b = v.Viewer3DService(
            self.d / "cache-b", cli=self.cli, parts=self.d / "parts", lock_path=lock
        )
        self.services.append(b)
        s1, p1 = self.board(BOARD)
        s2, p2 = self.board(MOVED)
        s3, p3 = self.board(MOVED.replace("(at 30 20)", "(at 31 20)"))
        a.request(s1, [p1])
        a.request(s2, [p2])
        b.request(s3, [p3])
        time.sleep(0.4)
        qs = [
            a.request(h, [p]) for h, p in ((s1, p1), (s2, p2))
        ]  # newest first: either may be the running one
        self.assertEqual(sorted(q["status"] for q in qs), ["exporting", "queued"])
        self.assertGreaterEqual(next(q for q in qs if q["status"] == "queued")["ahead"], 1)
        for s, h, p in ((a, s1, p1), (a, s2, p2), (b, s3, p3)):
            self.assertEqual(self.ready(s, h, p)["status"], "ready")
        runs = {}
        [runs.setdefault(c["pid"], {}).update(c) for c in self.calls()]
        spans = sorted((r["start"], r["end"]) for r in runs.values())
        self.assertEqual(len(spans), 3)
        for (s0, e0), (s1_, e1) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s1_, e0)

    def test_eviction_keeps_the_newest(self):
        s = self.service(max_files=1)
        s1, p1 = self.board(BOARD)
        s2, p2 = self.board(MOVED)
        k1 = self.ready(s, s1, p1)["key"]
        time.sleep(1.1)
        k2 = self.ready(s, s2, p2)["key"]
        self.assertTrue((self.d / "cache" / (k2 + ".glb")).exists())
        self.assertFalse((self.d / "cache" / (k1 + ".glb")).exists())
        self.assertFalse((self.d / "cache" / (k1 + ".json")).exists())

    def test_stale_queue_entries_are_dropped(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        s = self.service(stale_s=0.5, max_queue=1)
        s1, p1 = self.board(BOARD)
        s2, p2 = self.board(MOVED)
        s3, p3 = self.board(MOVED.replace("(at 30 20)", "(at 32 20)"))
        s.request(s1, [p1])
        time.sleep(0.3)
        s.request(s2, [p2])
        s.request(s3, [p3])  # max_queue=1: only the newest waits
        self.assertEqual(self.ready(s, s1, p1)["status"], "ready")
        time.sleep(0.3)
        with s.lock:
            keys = {j["key"] for j in s.jobs.values() if j["state"] in ("queued", "running")}
        self.assertNotIn(s.key_of_text(MOVED), keys)

    def test_placements_nobody_waits_for_are_not_exported(self):
        # the pane polls every 1.5 s while it waits: a checkpoint flicked past (asked once) must not
        # cost an export later, neither from this viewer's queue nor while waiting for the
        # machine-wide lock held by another viewer's export
        os.environ["FAKE_SLEEP"] = "1.2"
        lock = self.d / "shared.lock"
        a = self.service(lock_path=lock, stale_s=0.6)
        b = v.Viewer3DService(
            self.d / "cache-b", cli=self.cli, parts=self.d / "parts", lock_path=lock, stale_s=0.6
        )
        self.services.append(b)
        s1, p1 = self.board(BOARD)
        s2, p2 = self.board(MOVED)
        s3, p3 = self.board(MOVED.replace("(at 30 20)", "(at 33 20)"))
        a.request(s1, [p1])
        wait(
            lambda: any(j["stage"] in ("export", "compact") for j in list(a.jobs.values()))
        )  # a holds the lock
        self.assertEqual(a.request(s2, [p2])["status"], "queued")
        self.assertEqual(b.request(s3, [p3])["status"], "queued")
        self.assertEqual(self.ready(a, s1, p1)["status"], "ready")
        time.sleep(1.5)
        self.assertEqual(len({c["pid"] for c in self.calls()}), 1)
        for s, h, p in ((a, s2, p2), (b, s3, p3)):
            with s.lock:
                self.assertFalse(any(j["sha"] == h for j in s.jobs.values()))
            self.assertIsNone(s.meta(s.key_of_text(p.read_text())))

    def test_peek_never_enqueues(self):
        s = self.service()
        bs, bp = self.board(BOARD)
        r = s.request(bs, [bp], peek=True)
        self.assertEqual(r["status"], "idle")
        time.sleep(0.5)
        self.assertEqual(self.calls(), [])
        self.assertEqual(s.jobs, {})
        self.assertEqual(self.ready(s, bs, bp)["status"], "ready")
        self.assertEqual(s.request(bs, [bp], peek=True)["status"], "ready")
        os.environ["FAKE_SLEEP"] = "1"
        s2, p2 = self.board(MOVED)
        self.assertEqual(s.request(s2, [p2])["status"], "queued")
        self.assertIn(
            s.request(s2, [p2], peek=True)["status"], ("queued", "exporting")
        )  # an existing job is reported (and kept alive)

    def test_abandoned_export_is_killed(self):
        os.environ["FAKE_SLEEP"] = "4"
        s = self.service(abandon_s=0.8)
        bs, bp = self.board(BOARD)
        s.request(bs, [bp])
        wait(lambda: self.calls())
        t0 = time.time()
        wait(lambda: not s.jobs, timeout=10)
        self.assertLess(time.time() - t0, 4)
        self.assertFalse(
            any("end" in c for c in self.calls())
        )  # the fake kicad-cli never finished: its process group was killed
        key = s.key_of_text(BOARD)
        self.assertIsNone(s.meta(key))
        self.assertFalse((self.d / "cache" / (key + ".fail.json")).exists())
        self.assertEqual(
            list((self.d / "cache").glob("v3d-*")), []
        )  # the job's scratch folder went with it
        os.environ["FAKE_SLEEP"] = "0"
        self.assertEqual(self.ready(s, bs, bp)["status"], "ready")  # asked again: exported normally

    def test_two_services_sharing_one_cache_dir(self):
        # prod and dev viewers on one root share <root>/viewer3d: one evicts a key the other has seen
        # 'ready'
        lock = self.d / "shared.lock"
        a = self.service(lock_path=lock, max_files=1)
        b = self.service(lock_path=lock, max_files=1)
        s1, p1 = self.board(BOARD)
        s2, p2 = self.board(MOVED)
        k1 = self.ready(a, s1, p1)["key"]
        self.assertEqual(b.request(s1, [p1])["status"], "ready")
        self.assertEqual(b.glb(k1)[0][:4], b"glTF")
        time.sleep(1.1)
        self.assertEqual(self.ready(a, s2, p2)["status"], "ready")
        self.assertFalse((self.d / "cache" / (k1 + ".glb")).exists())  # a evicted k1
        r = b.request(s1, [p1])
        self.assertIn(
            r["status"], ("queued", "exporting"), r
        )  # not a stale 'ready' with a URL that 404s
        self.assertEqual(self.ready(b, s1, p1)["status"], "ready")
        self.assertEqual(b.glb(k1)[0][:4], b"glTF")
        (self.d / "cache" / (k1 + ".json")).unlink()
        (
            self.d / "cache" / (k1 + ".glb")
        ).unlink()  # removed by hand: glb() 404s once, then it exports again
        with self.assertRaises(FileNotFoundError):
            b.glb(k1)
        self.assertIn(b.request(s1, [p1], retry=True)["status"], ("queued", "exporting"))
        self.assertEqual(self.ready(b, s1, p1)["status"], "ready")

    def test_mutable_board_is_hashed_every_time_and_exported_from_a_snapshot(self):
        # the lane's working board (no boards/<sha> copy): PnR may rewrite it between the request and
        # the export
        os.environ["FAKE_COPY"] = str(self.d / "copy.kicad_pcb")
        s = self.service()
        nat = self.d / "native.kicad_pcb"
        nat.write_text(BOARD)
        bs = sha(BOARD)
        cands = [self.d / "boards" / (bs + ".kicad_pcb"), nat]
        self.assertEqual(s.request(bs, cands, peek=True)["status"], "idle")
        nat.write_text(MOVED)
        self.assertEqual(
            s.request(bs, cands)["status"], "unavailable"
        )  # it no longer hashes to the checkpoint: never exported under its key
        nat.write_text(BOARD)
        os.environ["FAKE_SLEEP"] = ".8"
        r = s.request(bs, cands)
        self.assertIn(r["status"], ("queued", "exporting"))
        nat.write_text(MOVED)
        wait(lambda: (self.d / "copy.kicad_pcb").exists())
        os.environ["FAKE_SLEEP"] = "0"
        self.assertIn("(at 11 21)", (self.d / "copy.kicad_pcb").read_text())
        self.assertNotIn("(at 12 21)", (self.d / "copy.kicad_pcb").read_text())
        nat.write_text(BOARD)
        self.assertEqual(
            wait(
                lambda: (lambda r: r if r["status"] in ("ready", "failed") else None)(
                    s.request(bs, cands)
                )
            )["status"],
            "ready",
        )
        wait(lambda: all(j["state"] != "running" for j in list(s.jobs.values())))
        # The worker marks the job done before its outer finally removes the snapshot, so wait
        # for the removal rather than checking once.
        wait(lambda: not list((self.d / "cache").glob("v3d-*")))  # snapshot removed

    def test_leftovers_are_swept(self):
        c = self.d / "cache"
        c.mkdir()
        old = time.time() - 3 * 3600
        for n in (
            "a" * 64 + ".log",
            "b" * 64 + ".fail.json",
            "v3d-src-1.kicad_pcb",
            "c" * 64 + ".log",
        ):
            (c / n).write_text("x")
        (c / "v3d-old").mkdir()
        for n in ("a" * 64 + ".log", "b" * 64 + ".fail.json", "v3d-src-1.kicad_pcb", "v3d-old"):
            os.utime(c / n, (old, old))
        self.service()
        self.assertEqual(
            sorted(p.name for p in c.iterdir() if not p.name.startswith("export.lock")),
            ["c" * 64 + ".log"],
        )  # only the fresh log is left

    def test_kill_removes_only_its_own_scratch_folder(self):
        os.environ["FAKE_SLEEP"] = "4"
        s = self.service(abandon_s=0.8)
        bs, bp = self.board(BOARD)
        (self.d / "cache/v3d-other-viewer").mkdir()
        s.request(bs, [bp])  # another viewer's export sharing the cache
        wait(lambda: self.calls())
        wait(lambda: not s.jobs, timeout=10)
        self.assertEqual([p.name for p in (self.d / "cache").glob("v3d-*")], ["v3d-other-viewer"])
        self.assertEqual(list((self.d / "cache").glob("*.log")), [])


class Http(Base):
    """server.py routes: /api/3d (peek, enqueue, ready), /api/3d/glb/<key> (gzip, immutable),
    /api/3d/status, errors."""

    def serve(self, *extra):
        root = self.d / "live"
        for d in ("events", "geometry", "boards"):
            (root / d).mkdir(parents=True, exist_ok=True)
        bs = sha(BOARD)
        (root / "boards" / (bs + ".kicad_pcb")).write_text(BOARD)
        (root / "geometry" / (bs + ".json")).write_text(
            json.dumps(
                dict(frame="mm-y-up", width=40, height=30, parts=[], tracks=[], vias=[], zones=[])
            )
        )
        (root / "events/event-01.json").write_text(
            json.dumps(
                dict(
                    schema="pnr-live-event-v1",
                    id="event-01",
                    time=1,
                    kind="phase_complete",
                    candidate="lane/a",
                    iteration="probe",
                    board="board.kicad_pcb",
                    board_sha256=bs,
                    data=dict(opens=0, violations=0),
                )
            )
        )
        p, base = start_viewer(
            root,
            "--net-summaries",
            "off",
            "--agent",
            "off",
            "--kicad-cli",
            str(self.cli),
            "--parts",
            str(self.d / "parts"),
            *extra,
            stderr=subprocess.DEVNULL,
        )
        self.procs = getattr(self, "procs", []) + [p]
        self.assertTrue(base.startswith("http://127.0.0.1:"), base)
        wait(lambda: self.get(base, "/api/state")[1]["revision"], timeout=10)
        return base, bs

    def tearDown(self):
        for p in getattr(self, "procs", []):
            stop(p)
        super().tearDown()

    def get(self, base, path, headers=None):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        try:
            with urlopen(Request(base + path, headers=headers or {}), timeout=10) as r:
                raw = r.read()
                return (
                    r.status,
                    (
                        json.loads(raw)
                        if r.headers.get("Content-Type") == "application/json"
                        else raw
                    ),
                    r.headers,
                )
        except HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}"), e.headers
            except ValueError:
                return e.code, raw, e.headers

    def test_routes(self):
        base, bs = self.serve()
        q = f"/api/3d?lane=lane/a&phase=live&sha={bs}"
        self.assertEqual(self.get(base, q + "&peek=1")[1]["status"], "idle")
        time.sleep(0.3)
        self.assertEqual(self.calls(), [])
        self.assertIn(self.get(base, q)[1]["status"], ("queued", "exporting", "ready"))
        r = wait(lambda: (lambda r: r if r["status"] == "ready" else None)(self.get(base, q)[1]))
        self.assertEqual(r["url"], "/api/3d/glb/" + r["key"])
        self.assertEqual(r["meta"]["no_model"], ["TP1"])
        self.assertEqual(
            self.get(base, "/api/3d?lane=lane/a&phase=live")[1]["key"], r["key"]
        )  # sha from the lane
        code, raw, h = self.get(base, r["url"], {"Accept-Encoding": "gzip"})
        self.assertEqual(
            (code, h["Content-Type"], h["Content-Encoding"]), (200, "model/gltf-binary", "gzip")
        )
        self.assertIn("immutable", h["Cache-Control"])
        self.assertEqual(gzip.decompress(raw)[:4], b"glTF")
        self.assertEqual(self.get(base, "/api/3d/glb/" + "0" * 64)[0], 404)
        self.assertEqual(self.get(base, "/api/3d/glb/nothex")[0], 400)
        self.assertEqual(self.get(base, "/api/3d?lane=lane/a&sha=xyz")[0], 400)
        self.assertEqual(self.get(base, "/api/3d?lane=nope&phase=live")[0], 404)
        self.assertEqual(
            self.get(base, f'/api/3d?lane=lane/a&sha={"b"*64}')[1]["status"], "unavailable"
        )
        st = self.get(base, "/api/3d/status")[1]
        self.assertTrue(st["available"])
        self.assertEqual(st["cli"], str(self.cli))
        if not self.get(base, "/api/about")[1]["missing_assets"]:  # the assembled dist
            code, raw, h = self.get(base, "/vendor/three/build/three.module.js")
            self.assertEqual(code, 200)
            self.assertIn("max-age", h["Cache-Control"])
            self.assertEqual(h["Content-Type"], "application/javascript")
            code, raw, h = self.get(base, "/vendor/three/LICENSE")
            self.assertEqual((code, h["Content-Type"]), (200, "text/plain; charset=utf-8"))
        self.assertEqual(self.get(base, "/viewer3d.js")[2]["Cache-Control"], "no-store")
        for path in (q + "&peek=1", r["url"], "/api/3d/status"):
            self.assertEqual(
                self.get(base, path, {"Host": "evil.example"})[0], 421, path
            )  # Host guard (DNS rebinding)

    def test_sigterm_during_an_export_kills_it(self):
        import signal

        os.environ["FAKE_SLEEP"] = "60"
        base, bs = self.serve()
        root = self.d / "live"
        self.assertIn(
            self.get(base, f"/api/3d?lane=lane/a&sha={bs}")[1]["status"], ("queued", "exporting")
        )
        pid = wait(lambda: self.calls() and self.calls()[0]["pid"], timeout=20)
        wait(lambda: list((root / "viewer3d").glob("v3d-*")))
        p = self.procs.pop()
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(10), 128 + signal.SIGTERM)
        stop(p)

        def gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True

        wait(gone, 10)
        self.assertEqual(
            list((root / "viewer3d").glob("v3d-*")), []
        )  # the fake kicad-cli's process group and its scratch folder
        self.assertEqual(list((root / "viewer3d").glob("*.log")), [])

    def test_off_and_gui_cli(self):
        base, bs = self.serve("--viewer3d", "off")
        self.assertEqual(
            self.get(base, f"/api/3d?lane=lane/a&sha={bs}")[1]["status"], "unavailable"
        )
        base, bs = self.serve("--kicad-cli", GUI)
        r = self.get(base, f"/api/3d?lane=lane/a&sha={bs}")[1]
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("headless", r["error"])


if __name__ == "__main__":
    unittest.main()
