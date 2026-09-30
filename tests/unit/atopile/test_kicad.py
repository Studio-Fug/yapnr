"""KiCad for atopile builds: never the GUI bundle; the stock footprint table."""

from __future__ import annotations

import plistlib
import stat
import tempfile
import unittest
from pathlib import Path

from yapnr.frontends.atopile import kicad


def make_bundle(root: Path, name: str, background: bool) -> Path:
    bundle = root / name
    (bundle / "Contents/MacOS").mkdir(parents=True)
    info = {"CFBundleIdentifier": "org.example.kicad"}
    if background:
        info["LSBackgroundOnly"] = True
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    cli = bundle / "Contents/MacOS/kicad-cli"
    cli.write_text("#!/bin/sh\necho 10.0.0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


class GuiGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_stock_application_is_refused(self):
        for path in (
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "/opt/KiCad.app/Contents/MacOS/kicad-cli",
        ):
            with self.assertRaises(kicad.Unavailable, msg=path):
                kicad.refuse_gui(Path(path))

    def test_bundles_must_be_background_only(self):
        with self.assertRaisesRegex(kicad.Unavailable, "not a background-only"):
            kicad.refuse_gui(make_bundle(self.root, "Copy.app", background=False))
        cli = make_bundle(self.root, "KiCad-headless.app", background=True)
        kicad.refuse_gui(cli)
        self.assertEqual(kicad.find_cli(str(cli)), cli)

    def test_environment_and_missing_programs(self):
        cli = make_bundle(self.root, "Headless.app", background=True)
        self.assertEqual(kicad.find_cli(environ={"PNR_KICAD_CLI": str(cli)}), cli)
        with self.assertRaises(kicad.Unavailable):
            kicad.find_cli(str(self.root / "missing/kicad-cli"))
        gui = make_bundle(self.root, "Gui.app", background=False)
        with self.assertRaises(kicad.Unavailable):
            kicad.find_cli(environ={"YAPNR_KICAD_CLI": str(gui)})

    def test_footprints_next_to_a_bundle_or_from_the_environment(self):
        cli = make_bundle(self.root, "Headless.app", background=True)
        shared = self.root / "Headless.app/Contents/SharedSupport/footprints/Resistor_SMD.pretty"
        shared.mkdir(parents=True)
        self.assertEqual(kicad.find_footprints(cli, environ={}).resolve(), shared.parent.resolve())
        other = self.root / "fp/Connector.pretty"
        other.mkdir(parents=True)
        found = kicad.find_footprints(cli, environ={kicad.FOOTPRINTS_ENV_VAR: str(other.parent)})
        self.assertEqual(found, other.parent)


class FpLibTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.footprints = root / "footprints"
        for name in ("Resistor_SMD", "Connector_PinHeader_2.54mm", "TestPoint"):
            (self.footprints / f"{name}.pretty").mkdir(parents=True)
        self.project = root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src/main.ato").write_text(
            'x = "Resistor_SMD:R_0603_1608Metric"\n'
            '# @pnr-current {"target":"usbc","pads":["A4B9"]}\n'
            'y = "NotStock:Thing"\n'
        )
        self.table = self.project / "layout/default/fp-lib-table"

    def test_only_referenced_libraries(self):
        written = kicad.write_fp_lib_table(self.table, self.footprints, self.project)
        self.assertEqual(written, ["Resistor_SMD"])
        text = self.table.read_text()
        self.assertIn('(name "Resistor_SMD")', text)
        self.assertIn(str(self.footprints / "Resistor_SMD.pretty"), text)
        self.assertNotIn("TestPoint", text)
        self.assertTrue(text.startswith("(fp_lib_table\n\t(version 7)"))

    def test_all_libraries(self):
        written = kicad.write_fp_lib_table(self.table, self.footprints, self.project, True)
        self.assertEqual(len(written), 3)

    def test_nothing_referenced_writes_nothing(self):
        (self.project / "src/main.ato").write_text("module A:\n    pass\n")
        self.assertEqual(kicad.write_fp_lib_table(self.table, self.footprints, self.project), [])
        self.assertFalse(self.table.exists())

    def test_existing_entries_are_kept(self):
        self.table.parent.mkdir(parents=True)
        existing = (
            '(fp_lib_table\n\t(version 7)\n\t(lib (name "MyPart")(type "KiCad")'
            '(uri "${KIPRJMOD}/../../src/parts/MyPart")(options "")(descr "atopile: part lib"))\n)\n'
        )
        self.table.write_text(existing)
        kicad.write_fp_lib_table(self.table, self.footprints, self.project)
        text = self.table.read_text()
        self.assertIn('(name "MyPart")', text)
        self.assertIn('(name "Resistor_SMD")', text)
        self.assertEqual(text.count("(fp_lib_table"), 1)
        self.assertTrue(text.rstrip().endswith(")"))
        # Idempotent.
        kicad.write_fp_lib_table(self.table, self.footprints, self.project)
        self.assertEqual(self.table.read_text(), text)


if __name__ == "__main__":
    unittest.main()
