"""Fixed blocks and copper keepouts through KiCad (KiCad Python only, by hand:
``python3 -m unittest tests.test_fixed_block_kicad``; the custom-rules case also needs
``PNR_KICAD_CLI``): fixed.json schema 2 export (arcs, pads, solid zones, rule areas,
held-out footprints), the copper digest and its frame, validate (moved arc, lock state,
group identity through save), writeback keeping the block, ``plane_fallback_drops``,
and the copper-keepout rule areas and custom rules judged by KiCad's DRC."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NATIVE = importlib.util.find_spec("pcbnew") is not None
CLI = os.environ.get("PNR_KICAD_CLI")
OFFSET = 30.0
SIZE = (30.0, 20.0)


def nm(v):
    return round(v * 1e6)


def at(x, y):
    """Engine mm (y up, origin at the outline's lower-left) to a pcbnew point."""
    import pcbnew as k

    return k.VECTOR2I(nm(OFFSET + x), nm(OFFSET + SIZE[1] - y))


def track(b, net, a, z, width=0.2, layer=None):
    import pcbnew as k

    t = k.PCB_TRACK(b)
    t.SetStart(at(*a))
    t.SetEnd(at(*z))
    t.SetWidth(nm(width))
    t.SetLayer(k.F_Cu if layer is None else layer)
    t.SetNet(net)
    b.Add(t)
    return t


def arc(b, net, s, m, e, width=0.2):
    import pcbnew as k

    t = k.PCB_ARC(b)
    t.SetStart(at(*s))
    t.SetMid(at(*m))
    t.SetEnd(at(*e))
    t.SetWidth(nm(width))
    t.SetLayer(k.F_Cu)
    t.SetNet(net)
    b.Add(t)
    return t


def via(b, net, xy, d=0.45, drill=0.25):
    import pcbnew as k

    v = k.PCB_VIA(b)
    v.SetPosition(at(*xy))
    v.SetViaType(k.VIATYPE_THROUGH)
    v.SetLayerPair(k.F_Cu, k.B_Cu)
    v.SetWidth(nm(d))
    v.SetDrill(nm(drill))
    v.SetNet(net)
    b.Add(v)
    return v


def block_board(rotate=0.0, shift=(0.0, 0.0)):
    """A 4-layer board (S P P S) with anchor U1 and group BLK: a CLK lead, a CLK arc
    and a return lead, a GND via, a GND zone on In1.Cu, a rule area barring tracks on
    B.Cu, and footprint ANT (one CLK pad). ``rotate`` (degrees) and ``shift`` (mm)
    move the anchor and the block together."""
    import pcbnew as k
    from test_stack_kicad import board, smd

    b, nets = board("SPPS", size=SIZE, offset=OFFSET)
    for name in ("CLK",):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    smd(b, "U1", (OFFSET + 10.0, OFFSET + 10.0), [("1", "CLK", (-0.8, 0)), ("2", "GND", (0.8, 0))])
    group = k.PCB_GROUP(b)
    group.SetName("BLK")
    b.Add(group)
    items = [
        track(b, nets["CLK"], (15, 10), (18, 10)),
        arc(b, nets["CLK"], (18, 10), (19, 11), (18, 12)),
        track(b, nets["CLK"], (18, 12), (15.5, 12)),
        via(b, nets["GND"], (20, 13)),
    ]
    zone = k.ZONE(b)
    zone.SetLayer(b.GetLayerID("In1.Cu"))
    zone.SetNet(nets["GND"])
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((14, 8), (22, 8), (22, 14), (14, 14)):
        outline.Append(at(x, y))
    b.Add(zone)
    items.append(zone)
    area = k.ZONE(b)
    area.SetIsRuleArea(True)
    lset = k.LSET()
    lset.AddLayer(k.B_Cu)
    area.SetLayerSet(lset)
    area.SetDoNotAllowTracks(True)
    area.SetDoNotAllowVias(False)
    area.SetDoNotAllowZoneFills(False)
    area.SetDoNotAllowPads(False)
    area.SetDoNotAllowFootprints(False)
    outline = area.Outline()
    outline.NewOutline()
    for x, y in ((15, 9), (19, 9), (19, 13), (15, 13)):
        outline.Append(at(x, y))
    b.Add(area)
    items.append(area)
    ant = smd(
        b, "ANT", (OFFSET + 21.0, OFFSET + SIZE[1] - 10.0), [("1", "CLK", (0, 0))], size=(0.6, 0.6)
    )
    items.append(ant)
    for item in items:
        group.AddItem(item)
        if hasattr(item, "SetLocked"):
            item.SetLocked(True)
    if rotate or shift != (0.0, 0.0):
        u1 = b.FindFootprintByReference("U1")
        centre = u1.GetPosition()
        for item in items + [u1]:
            if rotate:
                item.Rotate(centre, k.EDA_ANGLE(rotate, k.DEGREES_T))
            if shift != (0.0, 0.0):
                item.Move(k.VECTOR2I(nm(shift[0]), nm(-shift[1])))
    return b, nets


RULES = dict(
    layers=4,
    fixed_blocks=[
        dict(name="blk", group="BLK", anchor="U1", solid_layers=["In1.Cu"], refs=["ANT"])
    ],
)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class ExportDigestValidate(unittest.TestCase):
    def save(self, b, folder, name="source.kicad_pcb"):
        import pcbnew as k

        path = Path(folder) / name
        k.SaveBoard(str(path), b)
        return path

    def test_schema_two_export_holds_the_block_out(self):
        from pnr.fixed_copper import export

        b, _ = block_board()
        with tempfile.TemporaryDirectory() as tmp:
            source = self.save(b, tmp)
            export(source, Path(tmp) / "out", RULES)
            fixed = json.loads((Path(tmp) / "out" / "fixed.json").read_text())
            graph = json.loads((Path(tmp) / "out" / "placed.json").read_text())
        self.assertEqual(fixed["schema"], 2)
        self.assertEqual((fixed["tracks"], fixed["vias"]), ([], []))
        self.assertNotIn("arcs", fixed)
        (block,) = fixed["blocks"]
        self.assertEqual((block["name"], block["group"], block["anchor"]), ("blk", "BLK", "U1"))
        self.assertEqual(len(block["tracks"]), 2)
        self.assertEqual(len(block["arcs"]), 1)
        net, layer, s, m, e, w = block["arcs"][0]
        self.assertEqual((net, layer, w), ("CLK", "F.Cu", 0.2))
        for got, want in zip((s, m, e), ((18, 10), (19, 11), (18, 12))):
            self.assertAlmostEqual(got[0], want[0], places=6)
            self.assertAlmostEqual(got[1], want[1], places=6)
        self.assertEqual([v["net"] for v in block["vias"]], ["GND"])
        kinds = sorted((p["kind"], p.get("layer"), p["net"]) for p in block["polygons"])
        self.assertEqual(
            kinds, [("pad", "F.Cu", "CLK"), ("rule_area", None, ""), ("zone", "In1.Cu", "GND")]
        )
        area = next(p for p in block["polygons"] if p["kind"] == "rule_area")
        self.assertEqual((area["layers"], area["tracks"], area["vias"]), (["B.Cu"], True, False))
        self.assertEqual(block["refs"], ["ANT"])
        self.assertEqual(len(block["sha256"]), 64)
        self.assertEqual(sorted(c["ref"] for c in graph["components"]), ["U1"])

    def test_export_refuses_a_wrong_declared_digest(self):
        from pnr.fixed_copper import export

        b, _ = block_board()
        rules = json.loads(json.dumps(RULES))
        rules["fixed_blocks"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            source = self.save(b, tmp)
            with self.assertRaises(ValueError):
                export(source, Path(tmp) / "out", rules)

    def test_legacy_boards_export_schema_one(self):
        from test_stack_kicad import board

        from pnr.fixed_copper import extract

        b, nets = board("SS", size=SIZE, offset=OFFSET)
        track(b, nets["SIG"], (1, 1), (5, 1))
        self.assertEqual(sorted(extract(b)), ["frame", "tracks", "vias"])
        arc(b, nets["SIG"], (5, 1), (6, 2), (5, 3))
        copper = extract(b)
        self.assertEqual((copper["schema"], len(copper["arcs"])), (2, 1))
        self.assertNotIn("blocks", copper)

    def test_digest_follows_the_anchor_frame(self):
        from pnr.fixed_copper import block_digest

        base = block_digest(block_board()[0], "BLK", "U1")
        self.assertEqual(base, block_digest(block_board(shift=(2.0, -1.5))[0], "BLK", "U1"))
        for turn in (90.0, 180.0, 270.0):
            moved = block_board(rotate=turn)[0]
            self.assertEqual(base, block_digest(moved, "BLK", "U1"), turn)
        self.assertNotEqual(base, block_digest(block_board(shift=(2.0, -1.5))[0], "BLK", None))
        # A renamed net hashes as the old name under ``rename``.
        b, nets = block_board()
        nets["CLK"].SetNetname("CLK_X")
        self.assertEqual(base, block_digest(b, "BLK", "U1", {"CLK_X": "CLK"}))

    def drc(self):
        return dict(violations=[], unconnected_items=[])

    def test_validate_accepts_the_block_and_rejects_a_moved_arc_or_lock(self):
        import pcbnew as k

        from pnr.fixed_copper import validate
        from pnr.writeback import group_name

        rules = dict(RULES, fab=dict(track_width_mm=0.2, clearance_mm=0.15))
        b, _ = block_board()
        with tempfile.TemporaryDirectory() as tmp:
            source = self.save(b, tmp)
            same = self.save(k.LoadBoard(str(source)), tmp, "same.kicad_pcb")
            checks = validate(source, same, rules, self.drc(), self.drc())
            self.assertTrue(checks["accepted"], checks)
            self.assertTrue(checks["fixed_block_digest_equal"])
            moved = k.LoadBoard(str(source))
            for t in moved.GetTracks():
                if t.GetClass() == "PCB_ARC":
                    t.SetMid(k.VECTOR2I(t.GetMid().x + 1000, t.GetMid().y))
            moved_path = self.save(moved, tmp, "moved.kicad_pcb")
            checks = validate(source, moved_path, rules, self.drc(), self.drc())
            self.assertFalse(checks["fixed_copper_preserved"])
            self.assertFalse(checks["fixed_blocks"]["blk"]["digest_equal"])
            self.assertFalse(checks["accepted"])
            unlocked = k.LoadBoard(str(source))
            for t in unlocked.GetTracks():
                if group_name(t) == "BLK":
                    t.SetLocked(False)
                    break
            path = self.save(unlocked, tmp, "unlocked.kicad_pcb")
            checks = validate(source, path, rules, self.drc(), self.drc())
            self.assertFalse(checks["fixed_blocks"]["blk"]["locked_preserved"])
            self.assertFalse(checks["accepted"])

    def test_group_survives_uuid_repair_and_save(self):
        import pcbnew as k

        from pnr.fixed_copper import block_digest
        from pnr.writeback import group_name, normalize_item_uuids

        b, _ = block_board()
        before = block_digest(b, "BLK", "U1")
        normalize_item_uuids(b)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.save(b, tmp)
            again = k.LoadBoard(str(path))
        members = [t for t in again.GetTracks() if group_name(t) == "BLK"]
        self.assertEqual(len(members), 4)
        self.assertTrue(all(t.IsLocked() for t in members))
        self.assertEqual(block_digest(again, "BLK", "U1"), before)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class WritebackKeepsBlock(unittest.TestCase):
    def test_writeback_keeps_block_copper_and_drops_the_rest(self):
        import pcbnew as k

        from pnr.fixed_block import hold_out
        from pnr.fixed_copper import block_digest
        from pnr.ingest import build_graph
        from pnr.writeback import writeback

        b, nets = block_board()
        stray = track(b, nets["SIG"], (2, 2), (6, 2))  # preview copper: removed
        self.assertIsNotNone(stray)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.kicad_pcb"
            k.SaveBoard(str(source), b)
            graph = build_graph(k.LoadBoard(str(source)))
            hold_out(graph, ["ANT"])
            out = Path(tmp) / "placed.kicad_pcb"
            writeback(
                str(source), graph, str(out), width=SIZE[0], height=SIZE[1], rules=RULES, layers=4
            )
            placed = k.LoadBoard(str(out))
            plain = Path(tmp) / "plain.kicad_pcb"
            writeback(
                str(source),
                graph,
                str(plain),
                width=SIZE[0],
                height=SIZE[1],
                rules={"layers": 4},
                layers=4,
            )
            bare = k.LoadBoard(str(plain))
        self.assertEqual(block_digest(placed, "BLK", "U1"), block_digest(b, "BLK", "U1"))
        self.assertEqual(len(placed.GetTracks()), 4)
        self.assertEqual(len(bare.GetTracks()), 0)  # without the block: every track goes
        self.assertIsNotNone(placed.FindFootprintByReference("ANT"))

    def test_plane_fallback_drops_false_adds_no_copper(self):
        from test_stack_kicad import board, smd, stack_of

        from pnr.writeback import form_planes

        for flag, vias in ((None, 2), (True, 2), (False, 0)):
            b, _ = board("SPPS")
            smd(b, "C1", (38.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
            stack, rules = stack_of("SPPS", {"GND": "In1.Cu", "VCC": "In2.Cu"})
            if flag is not None:
                rules["plane_fallback_drops"] = flag
            formed = form_planes(b, stack, rules, 30.0, 20.0)
            self.assertEqual(formed["fallback_vias"], vias, flag)
            self.assertEqual(sum(t.GetClass() == "PCB_VIA" for t in b.GetTracks()), vias)
            if flag is False:
                self.assertEqual(formed["unreached"], ["C1.1", "C1.2"])
            else:
                self.assertNotIn("unreached", formed)


def drc(cli, board):
    out = Path(board).with_suffix(".drc.json")
    subprocess.run(
        [
            cli,
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "--units",
            "mm",
            "-o",
            str(out),
            str(board),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return json.loads(out.read_text())


@unittest.skipUnless(NATIVE and CLI, "requires KiCad Python and PNR_KICAD_CLI")
class KeepoutRulesJudgedByKiCad(unittest.TestCase):
    """The keepouts writeback writes, judged by kicad-cli: a class keepout bars
    other nets' tracks and vias on its layers only, never an allowed class or an
    exempt group's copper; a plain v1 keepout bars every net on its layers."""

    def test_rule_areas_and_custom_rules(self):
        import pcbnew as k

        from pnr.fab_profile import append_board_rules
        from pnr.graph import BoardGraph
        from pnr.writeback import _keepout_v1_area, _WriteFrame, keepout_dru

        b, nets = block_board()
        guard = dict(
            name="guard",
            polygon=[[14, 6], [23, 6], [23, 15], [14, 15]],
            layers=["F.Cu", "In1.Cu"],
            items=["tracks", "vias"],
            allow_classes=["pwr"],
            allow_nets=[],
            allowed_nets=["VCC"],
            exempt_groups=["BLK"],
        )
        plain = dict(
            name="plain",
            polygon=[[2, 2], [8, 2], [8, 6], [2, 6]],
            layers=["B.Cu"],
            items=["tracks"],
            allow_classes=[],
            allow_nets=[],
            allowed_nets=[],
            exempt_groups=[],
        )
        rules = dict(
            layers=4,
            copper_keepouts=[guard, plain],
            net_classes=[dict(name="pwr", nets=["VCC"])],
        )
        frame = _WriteFrame(SIZE[1], OFFSET)
        for spec in (guard, plain):
            b.Add(_keepout_v1_area(b, BoardGraph("t"), spec, frame))
        planted = {
            "sig_front": track(b, nets["SIG"], (14.5, 7), (16.5, 7)),  # barred
            "sig_back": track(
                b, nets["SIG"], (14.5, 7.5), (16.5, 7.5), layer=k.B_Cu
            ),  # other layer
            "vcc_front": track(b, nets["VCC"], (20.5, 7), (22.5, 7)),  # allowed class
            "sig_via": via(b, nets["SIG"], (22, 14.5)),  # barred (vias, through F.Cu)
            "plain_back": track(b, nets["SIG"], (3, 3), (6, 3), layer=k.B_Cu),  # plain: barred
            "plain_front": track(b, nets["SIG"], (3, 4), (6, 4)),  # plain: F.Cu free
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keepout.kicad_pcb"
            k.SaveBoard(str(path), b)
            project = {
                "net_settings": {
                    "classes": [{"name": "Default"}, {"name": "pwr"}],
                    "netclass_patterns": [{"netclass": "pwr", "pattern": "VCC"}],
                }
            }
            path.with_suffix(".kicad_pro").write_text(json.dumps(project))
            block = keepout_dru(rules)
            self.assertIn("A.intersectsArea('PNR keepout:guard')", block)
            self.assertIn("!A.memberOfGroup('BLK')", block)
            self.assertNotIn("plain", block)
            append_board_rules(path, block)
            report = drc(CLI, path)
        hits = {}
        for v in report["violations"]:
            if v["type"] != "items_not_allowed":  # the planted copper also dangles
                continue
            for item in v.get("items", []):
                hits.setdefault(item.get("uuid"), set()).add(v["type"])
        flagged = {name for name, item in planted.items() if item.m_Uuid.AsString() in hits}
        self.assertEqual(flagged, {"sig_front", "sig_via", "plain_back"}, report["violations"])
        group_items = {t.m_Uuid.AsString() for t in b.GetTracks() if t.GetParentGroup()}
        self.assertFalse(group_items & set(hits), "the exempt group's copper is never flagged")


if __name__ == "__main__":
    unittest.main()
