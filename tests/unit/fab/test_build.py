"""``yapnr fab build`` end to end with a fake kicad-cli (no KiCad, no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from yapnr.fab import build, bundle, testing


def no_network(*_args, **_kwargs):
    raise AssertionError("yapnr fab opened a network connection")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = testing.FakeKicadCli()
        for target, value in (
            ("yapnr.fab.build.kicad.find_cli", mock.Mock(return_value=Path("kicad-cli"))),
            ("yapnr.fab.build.kicad.Cli", mock.Mock(side_effect=lambda *a, **k: self.fake)),
            ("socket.socket.connect", no_network),
            ("socket.create_connection", no_network),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, vendor="oshpark", layers=4, out="out", **kwargs):
        board = testing.write_board(self.dir / "src", "board", layers=layers)
        kwargs.setdefault("name", "demo")
        return build.Request(board=board, vendor=vendor, out=self.dir / out, **kwargs)

    def test_osh_park_bundle(self):
        built = build.build(self.request())
        d = built.bundle_dir
        self.assertEqual(d.name, "demo-oshpark-4l")
        self.assertEqual(
            sorted(p.name for p in d.iterdir()),
            [
                "README.md",
                "demo-oshpark-bundle.zip",
                "demo-oshpark-gerbers.zip",
                "drc.json",
                "fab-check.json",
                "manifest.json",
                "order-card.json",
                "order-card.md",
            ],
        )
        with zipfile.ZipFile(built.gerber_zip) as zf:
            self.assertEqual(
                zf.namelist(),
                [
                    "demo.G2L",
                    "demo.G3L",
                    "demo.GBL",
                    "demo.GBO",
                    "demo.GBS",
                    "demo.GKO",
                    "demo.GTL",
                    "demo.GTO",
                    "demo.GTS",
                    "demo.XLN",
                ],
            )
        card = built.card
        self.assertEqual(card["vendor"]["id"], "oshpark")
        self.assertEqual(card["stackup"]["id"], "oshpark-4l-fr408hr")
        self.assertEqual(card["qty"], {"value": 3, "rule": "multiples of 3"})
        self.assertAlmostEqual(card["estimate"]["usd"], round(30 * 20 / 645.16 * 10, 2))
        self.assertEqual(card["staging"]["url"], "https://oshpark.com/")
        self.assertEqual(
            card["files"]["upload"]["path"], "demo-oshpark-4l/demo-oshpark-gerbers.zip"
        )

    def test_the_manifest_hashes_every_file_and_holds_no_absolute_path(self):
        built = build.build(
            self.request(argv=["yapnr", "fab", "build", str(self.dir / "src/board.kicad_pcb")])
        )
        manifest = built.manifest
        for entry in manifest["files"]:
            self.assertEqual(bundle.sha256_file(built.bundle_dir / entry["name"]), entry["sha256"])
        self.assertEqual(len(manifest["gerber_zip"]["members"]), 10)
        self.assertEqual(manifest["inputs"]["profile"]["name"], "oshpark-4l")
        self.assertEqual(manifest["time"], "1980-01-01T00:00:00+00:00")
        texts = [p for p in built.bundle_dir.iterdir() if p.suffix in (".json", ".md")]
        leaks = [x for x in bundle.privacy_findings(texts) if x[1] != "e-mail address"]
        self.assertEqual(leaks, [])
        self.assertNotIn(str(self.dir), json.dumps(manifest))

    def test_two_builds_are_byte_identical_whatever_kicad_stamps(self):
        first = build.build(self.request(out="a"))
        self.fake = testing.FakeKicadCli(date="2027-01-02T03:04:05")
        second = build.build(self.request(out="b"))
        self.assertEqual(first.gerber_zip.read_bytes(), second.gerber_zip.read_bytes())
        for name in ("order-card.json", "README.md", "drc.json", "fab-check.json"):
            self.assertEqual(
                (first.bundle_dir / name).read_bytes(),
                (second.bundle_dir / name).read_bytes(),
                name,
            )

    def test_an_unchanged_board_runs_no_kicad_the_second_time(self):
        build.build(self.request())
        self.fake.calls.clear()
        build.build(self.request())
        self.assertEqual(self.fake.calls, [])

    def test_a_fab_check_error_stops_the_build(self):
        self.fake = testing.FakeKicadCli(
            violations=[
                {
                    "type": "annular_width",
                    "severity": "error",
                    "description": "Annular",
                    "items": [],
                }
            ]
        )
        with self.assertRaises(build.BuildStopped) as stop:
            build.build(self.request())
        d = stop.exception.bundle_dir
        self.assertFalse(any(p.suffix == ".zip" for p in d.iterdir()))
        doc = json.loads((d / "fab-check.json").read_text())
        self.assertEqual(doc["summary"]["error"], 1)

    def test_jlc_assembly_bundle(self):
        built = build.build(self.request("jlcpcb", assembly=True, consign=["J1"]))
        d = built.bundle_dir
        self.assertTrue((d / "demo-jlcpcb-bom.csv").is_file())
        cpl = (d / "demo-jlcpcb-cpl.csv").read_text().splitlines()
        self.assertEqual(cpl[0], "Designator,Mid X,Mid Y,Layer,Rotation")
        self.assertEqual([line.split(",")[0] for line in cpl[1:]], ["R1", "R2"])
        self.assertEqual(built.card["options"]["specify_stackup"], "JLC04161H-7628")
        self.assertEqual(built.card["assembly"]["parts"], 2)
        self.assertIn("PCB Assembly", " ".join(built.card["staging"]["steps"]))
        with zipfile.ZipFile(built.gerber_zip) as zf:
            self.assertFalse(any(n.endswith(".csv") for n in zf.namelist()))

    def test_bare_jlc_card_has_no_assembly_step(self):
        built = build.build(self.request("jlcpcb"))
        self.assertNotIn("Assembly", " ".join(built.card["staging"]["steps"]))

    def test_draft_profiles_need_allow_draft(self):
        with self.assertRaises(build.BuildStopped):
            build.build(self.request("pcbway"))
        built = build.build(self.request("pcbway", allow_draft=True))
        self.assertEqual(built.card["profile"]["status"], "draft")
        self.assertTrue(built.card["warnings"])

    def test_public_refuses_private_text(self):
        req = self.request(public=True)
        with mock.patch.object(
            build.bundle, "readme", return_value="built in /" + "Users/someone/x\n"
        ):
            with self.assertRaises(build.BuildStopped) as stop:
                build.build(req)
        codes = [f.code for f in stop.exception.checked.findings]
        self.assertIn("FAB-PRIVACY", codes)

    def test_the_default_profile(self):
        def chosen(vendor, layers=4, dru=None, **kwargs):
            board = testing.write_board(
                self.dir / f"p{len(list(self.dir.iterdir()))}", "b", layers=layers, **kwargs
            )
            if dru:
                board.with_suffix(".kicad_dru").write_text(
                    f"(version 1)\n# Generated by pnr.fab_profile (profile {dru}); x\n"
                )
            return build.default_profile(vendor, build.board_mod.read(board))

        self.assertEqual(chosen("oshpark", 2), "oshpark-2l")
        self.assertEqual(chosen("oshpark", 6), "oshpark-6l")
        self.assertEqual(chosen("jlcpcb"), "jlc-4l")
        # A via in an SMD pad needs JLC's filled vias.
        self.assertEqual(chosen("jlcpcb", via_in_pad=(0.35, 0.20)), "jlc-pofv")
        # The profile the board was routed under wins when it is this vendor's ...
        self.assertEqual(chosen("jlcpcb", dru="jlc-pofv"), "jlc-pofv")
        # ... and not when it is another vendor's or another layer count's.
        self.assertEqual(chosen("oshpark", dru="jlc-pofv"), "oshpark-4l")
        self.assertEqual(chosen("oshpark", 4, dru="oshpark-2l"), "oshpark-4l")

    def test_a_vendor_without_a_profile_for_the_layer_count(self):
        with self.assertRaises(build.profiles.ProfileError) as err:
            build.build(self.request("jlcpcb", layers=2))
        self.assertIn("no profile for 2 copper layers", str(err.exception))


if __name__ == "__main__":
    unittest.main()
