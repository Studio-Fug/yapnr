"""Discarded KiCad items are deleted with board.Delete, never board.Remove.

board.Remove detaches an item and hands it to its Python wrapper, which frees it
whenever the wrapper dies, possibly after the board itself; a detached zone
crashed KiCad's Python at teardown (SIGSEGV, pnr.fanout_reserve.release, src12i).
board.Delete frees the item at once and invalidates the wrapper. The audit left
three Remove calls (KEPT). The static test needs no KiCad and fails on any new
Remove call; the native tests (KiCad Python only) run representative engine
paths in a child process and check that the saved board is byte-identical to
the Remove version and that the child exits cleanly after freeing the board
before the item lists.
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PNR = Path(__file__).parent.parent  # hardware/pnr (also inside Bazel runfiles)
TOOLS = PNR.parent / "tools"
NATIVE = importlib.util.find_spec("pcbnew") is not None

# The Remove calls the audit kept, one entry per call (file, call source), and why
# each is safe. A new Remove call fails the test even in one of these files.
KEPT = {
    (
        "pnr/drc_warm/model.py",
        "old.Remove(item)",
    ): "retired copper lives in the session keepalive arena until the host exits",
    (
        "pnr/escape_shove.py",
        "b.Remove(t)",
    ): "detached with thisown=False: never freed, so never after its board",
    (
        "pnr/geometric_native.py",
        "b.Remove(t)",
    ): "detached with thisown=False: never freed, so never after its board",
}


def engine_sources():
    yield from sorted(PNR.glob("pnr/**/*.py"))
    yield from sorted(PNR.glob("regression/*.py"))
    yield TOOLS / "keyhole_region.py"


def remove_calls(sources=None, root=PNR.parent):
    """(path relative to ``root``, call source, line) of every ``<x>.Remove(...)`` call."""
    for path in engine_sources() if sources is None else sources:
        text = path.read_text()
        for node in ast.walk(ast.parse(text, str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Remove"
            ):
                yield path.relative_to(root).as_posix(), ast.get_source_segment(
                    text, node
                ), node.lineno


def unaudited(calls):
    """The calls beyond KEPT: every call not listed, and any extra copy of a listed one."""
    budget = {("pnr/" + name, source): 1 for name, source in KEPT}
    extra = []
    for path, source, line in sorted(calls):
        if budget.get((path, source), 0):
            budget[path, source] -= 1
        else:
            extra.append((path, source, line))
    return extra, sorted(key for key, left in budget.items() if left)


class RemoveAuditTests(unittest.TestCase):
    def test_sources_are_scanned(self):
        names = {p.relative_to(PNR.parent).as_posix() for p in engine_sources()}
        for name in (
            "pnr/pnr/writeback.py",
            "pnr/pnr/fanout_reserve.py",
            "pnr/regression/check_native_oracle.py",
            "tools/keyhole_region.py",
        ):
            self.assertIn(name, names)

    def test_only_audited_remove_calls_remain(self):
        extra, missing = unaudited(remove_calls())
        self.assertEqual(
            extra,
            [],
            "a discarded KiCad item must be deleted with board.Delete (see this "
            "module); keep Remove only for an item that is re-added or kept alive, "
            "and list the call in KEPT",
        )
        self.assertEqual(missing, [], "a KEPT call is gone: remove its entry")

    def test_a_new_remove_in_a_kept_file_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, _ in KEPT:
                (root / "pnr" / name).parent.mkdir(parents=True, exist_ok=True)
                (root / "pnr" / name).write_text((PNR / name).read_text())
            sources = sorted(root.glob("pnr/**/*.py"))
            self.assertEqual(unaudited(remove_calls(sources, root)), ([], []))
            shove = root / "pnr/pnr/escape_shove.py"
            shove.write_text(
                shove.read_text() + "\ndef drop(b, z):\n    b.Remove(z)\n"
                "\ndef again(b, t):\n    b.Remove(t)\n"
            )
            extra, missing = unaudited(remove_calls(sources, root))
            self.assertEqual(
                [(path, source) for path, source, _ in extra],
                [
                    ("pnr/pnr/escape_shove.py", "b.Remove(t)"),
                    ("pnr/pnr/escape_shove.py", "b.Remove(z)"),
                ],
            )
            self.assertEqual(missing, [])

    def test_detached_items_are_never_owned_by_python(self):
        for (name, source), reason in KEPT.items():
            if "thisown" not in reason:
                continue
            lines = [
                line.replace(" ", "")
                for line in (PNR / name).read_text().splitlines()
                if source in line
            ]
            self.assertEqual(len(lines), 1, name)
            self.assertIn("thisown=False", lines[0], name)


# Child process: build a fixture, run engine code that discards items, save,
# free the board before the item lists, exit normally. argv: mode case out.
# mode 'remove' restores the pre-audit behaviour (Delete aliased to Remove).
CHILD = r"""
import gc, sys
import pcbnew as k
mode, case, out = sys.argv[1:4]
k.KIID.SeedGenerator(1)  # deterministic uuids: both modes must save the same bytes
if mode == 'remove':
    k.BOARD_ITEM_CONTAINER.Delete = k.BOARD_ITEM_CONTAINER.Remove
def mm(x, y):
    return k.VECTOR2I(round(x * 1e6), round(y * 1e6))
b = k.BOARD(); b.SetCopperLayerCount(4)
n = k.NETINFO_ITEM(b, 'n'); b.Add(n)
def track(a, z, width=0.2):
    t = k.PCB_TRACK(b); t.SetStart(mm(*a)); t.SetEnd(mm(*z)); t.SetLayer(k.F_Cu)
    t.SetWidth(round(width * 1e6)); t.SetNetCode(n.GetNetCode()); b.Add(t)
    return t
def pad(ref, at):
    f = k.FOOTPRINT(b); f.SetReference(ref); b.Add(f)
    p = k.PAD(f); p.SetNumber('1'); p.SetPosition(mm(*at)); p.SetSize(mm(.4, .4))
    p.SetShape(k.PAD_SHAPE_RECT); p.SetAttribute(k.PAD_ATTRIB_SMD)
    ls = k.LSET(); ls.AddLayer(k.F_Cu); p.SetLayerSet(ls); p.SetNetCode(n.GetNetCode()); f.Add(p)
kept = []  # wrappers that outlive the board below
if case == 'writeback':
    from pnr.graph import BoardGraph, Component
    from pnr.writeback import _clear_tracks, apply_copper_keepouts, apply_mounting_holes
    kept += [track((1, 1), (3, 1)), track((3, 1), (3, 3))]
    v = k.PCB_VIA(b); v.SetPosition(mm(3, 3)); v.SetWidth(600000); v.SetDrill(300000)
    v.SetNetCode(n.GetNetCode()); b.Add(v); kept.append(v)
    graph = BoardGraph('t', components=[Component('U1', 't', (10, 20), 90, 'top', (4, 4), (4, 4))])
    rules = {'copper_keepouts': [{'name': 'die', 'ref': 'U1', 'rect_mm': [-1, -2, 1, 2]}],
             'mounting_holes': [{'name': 'H1', 'at': [5, 5], 'clearance_diameter_mm': 3, 'drill_mm': 2}]}
    for _ in range(2):  # the second pass deletes the first pass's areas and hole footprint
        apply_copper_keepouts(b, graph, rules, 50); apply_mounting_holes(b, rules, 50)
        kept += list(b.Zones()) + list(b.GetFootprints())
    assert _clear_tracks(b) == 3
elif case == 'cycle':
    from pnr.track_graph import cycle_candidates, apply_cycle
    kept += [track((7.55, 6.5), (8, 6.95)), track((8, 6.95), (8, 7.7)),
             track((7.55, 6.5), (7.55, 7.25)), track((7.55, 7.25), (8.5, 8.2))]
    pad('A', (7.55, 6.5)); pad('B', (8.5, 8.2)); b.BuildConnectivity()
    removed = apply_cycle(b, cycle_candidates(b, {}, [])[0])
    assert sorted(removed) and len(list(b.GetTracks())) == 2, removed
elif case == 'release':
    from pnr import fanout_reserve
    for i in range(3):
        poly = k.SHAPE_POLY_SET(); poly.NewOutline()
        for x, y in ((i, 0), (i + .5, 0), (i + .5, .5), (i, .5)):
            poly.Append(round(x * 1e6), round(y * 1e6))
        kept += fanout_reserve.add_rule_area(b, fanout_reserve.PREFIX + 'U1.%d' % i, poly,
                                              [k.F_Cu] if i else list(b.GetEnabledLayers().CuStack()))
    assert fanout_reserve.release(b) == 3 and not list(b.Zones())
b.BuildConnectivity(); k.SaveBoard(out, b)
del b; gc.collect()  # the board goes first, as when a worker rebinds or returns
del kept; gc.collect()
print('clean', flush=True)
"""


@unittest.skipUnless(NATIVE, "requires KiCad Python (pcbnew)")
class DeleteNativeTests(unittest.TestCase):
    def run_python(self, *args, cwd=None):
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [str(PNR.resolve())]
                + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
            ),
        )
        return subprocess.run(
            [sys.executable, *map(str, args)],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def child(self, mode, case, directory):
        out = Path(directory) / (mode + "-" + case + ".kicad_pcb")
        result = self.run_python("-c", CHILD, mode, case, out)
        return result, out

    def test_discarding_paths_match_remove_output_and_exit_cleanly(self):
        for case in ("writeback", "cycle", "release"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                deleted, out = self.child("delete", case, directory)
                self.assertEqual(deleted.returncode, 0, deleted.stderr[-2000:])
                self.assertIn("clean", deleted.stdout)
                removed, reference = self.child("remove", case, directory)
                self.assertEqual(removed.returncode, 0, removed.stderr[-2000:])
                self.assertEqual(out.read_bytes(), reference.read_bytes())

    def test_fanout_release_worker(self):
        import pcbnew as k
        from pnr import fanout_reserve

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "board.kicad_pcb"
            b = k.BOARD()
            poly = k.SHAPE_POLY_SET()
            poly.NewOutline()
            for x, y in ((0, 0), (500000, 0), (500000, 500000)):
                poly.Append(x, y)
            fanout_reserve.add_rule_area(b, fanout_reserve.PREFIX + "U1.1", poly, [k.F_Cu])
            k.SaveBoard(str(source), b)
            out, report = root / "out.kicad_pcb", root / "report.json"
            result = self.run_python(
                "-m", "pnr.fanout_reserve", source, "--release", "--out", out, "--report", report
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(json.loads(report.read_text())["released"], 1)
            self.assertEqual(list(k.LoadBoard(str(out)).Zones()), [])

    def test_subpcb_worker_keeps_only_listed_footprints(self):
        import pcbnew as k

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "board.kicad_pcb"
            b = k.BOARD()
            for ref in ("R1", "R2", "U1"):
                f = k.FOOTPRINT(b)
                f.SetReference(ref)
                b.Add(f)
            k.SaveBoard(str(source), b)
            keep = root / "keep.json"
            keep.write_text(json.dumps(["U1"]))
            out = root / "out.kicad_pcb"
            result = self.run_python("-m", "pnr.hier.subpcb", source, out, "--keep", keep)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(
                json.loads(result.stdout.strip().splitlines()[-1]), dict(kept=1, removed=2)
            )
            self.assertEqual(
                [f.GetReference() for f in k.LoadBoard(str(out)).GetFootprints()], ["U1"]
            )


if __name__ == "__main__":
    unittest.main()
