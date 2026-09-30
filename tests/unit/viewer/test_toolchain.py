"""KiCad tools for the viewer: resolution order, discovery and the refusal of the GUI
application."""

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr.viewer import runtime, toolchain
from yapnr.viewer.testing import write_fake


class ToolchainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-toolchain-")
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        self.cli = write_fake(self.dir / "bin/kicad-cli", "print('fake')\n")
        self.py = write_fake(self.dir / "bin/kicad-python", "print('fake')\n")

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, name, info):
        b = self.dir / name
        (b / "Contents/MacOS").mkdir(parents=True)
        (b / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        return write_fake(b / "Contents/MacOS/kicad-cli", "print('fake')\n")

    def test_order_flag_env_machine(self):
        other = write_fake(self.dir / "bin/other-cli", "print('other')\n")
        env = {"YAPNR_KICAD_CLI": str(self.cli), "PNR_KICAD_CLI": str(other)}
        self.assertEqual(toolchain.kicad_cli(environ=env), self.cli)
        self.assertEqual(toolchain.kicad_cli(environ={"PNR_KICAD_CLI": str(other)}), other)
        self.assertEqual(toolchain.kicad_cli(other, environ=env), other)  # the flag wins
        self.assertEqual(toolchain.kicad_cli(machine=self.cli, environ={}), self.cli)
        self.assertEqual(toolchain.kicad_cli(machine=other, environ=env), self.cli)  # env first
        py_env = {"YAPNR_KICAD_PYTHON": str(self.py)}
        self.assertEqual(toolchain.kicad_python(environ=py_env), self.py)
        self.assertEqual(toolchain.kicad_python(machine=self.py, environ={}), self.py)
        with self.assertRaisesRegex(toolchain.Unavailable, "not found"):
            toolchain.kicad_cli(self.dir / "nope", environ={})

    def test_gui_application_is_refused(self):
        gui = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
        for path in (gui, str(self.dir / "KiCad.app/Contents/MacOS/kicad-cli")):
            with self.assertRaisesRegex(toolchain.Unavailable, "refusing the GUI KiCad"):
                toolchain.kicad_cli(path, environ={})
        with self.assertRaisesRegex(toolchain.Unavailable, "refusing the GUI KiCad"):
            toolchain.kicad_python(
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/"
                "Current/bin/python3",
                environ={},
            )
        link = self.dir / "bin/link"
        link.symlink_to(gui)
        with self.assertRaises(toolchain.Unavailable):
            toolchain.kicad_cli(link, environ={})
        renamed = self.bundle("KiCad-10.app", dict(CFBundleIdentifier="org.kicad.kicad"))
        with self.assertRaisesRegex(toolchain.Unavailable, "not a background-only app bundle"):
            toolchain.kicad_cli(renamed, environ={})
        headless = self.bundle("KiCad-hl.app", dict(LSBackgroundOnly=True))
        self.assertEqual(toolchain.kicad_cli(headless, environ={}), headless)

    def test_discovery_never_uses_the_applications_folder(self):
        self.assertEqual(toolchain.HEADLESS_CLI.parts[:2], ("~", "Applications"))
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            toolchain, "HEADLESS_APP", self.dir / "missing.app"
        ), mock.patch.object(toolchain, "HEADLESS_CLI", self.dir / "missing.app/kicad-cli"):
            with self.assertRaisesRegex(toolchain.Unavailable, "no headless kicad-cli"):
                toolchain.kicad_cli(environ={})
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(
            "os.environ", {"PATH": str(self.dir / "bin")}
        ):
            self.assertEqual(toolchain.kicad_cli(environ={}), self.cli)
            # a python3 on PATH is probed (also where /usr/bin/python3 is missing, as in slim
            # container images)
            write_fake(self.dir / "bin/python3", "print('fake')\n")
            probe = mock.Mock(return_value=False)
            with self.assertRaisesRegex(toolchain.Unavailable, "imports pcbnew"):
                toolchain.kicad_python(environ={}, probe=probe)
            self.assertTrue(probe.called)

    def test_probe_is_bounded_and_false_without_pcbnew(self):
        self.assertFalse(toolchain.probe_pcbnew(Path(sys.executable), timeout=30))
        self.assertFalse(toolchain.probe_pcbnew(self.dir / "nope"))

    def test_toolchain_caches_and_reports_why(self):
        tc = toolchain.Toolchain(cli=self.dir / "nope", python=self.py, environ={})
        cli, why = tc.cli()
        self.assertIsNone(cli)
        self.assertIn("not found", why)
        self.assertEqual(tc.python(), (self.py, None))
        self.assertIs(tc.cli(), tc.cli())

    def test_child_environments(self):
        rt = runtime.imported_runtime()
        self.assertTrue((rt / "pnr/__init__.py").is_file())
        env = runtime.hermetic_env(rt)
        self.assertEqual(env["PYTHONPATH"].split(":")[0], str(rt))
        self.assertEqual(env["OMP_NUM_THREADS"], "1")
        kenv = runtime.kicad_env(rt)
        self.assertEqual(kenv["PYTHONPATH"], str(rt))  # no 3.11 site-packages for KiCad's Python
        with mock.patch.dict("os.environ", {"PNR_LIVE_DIR": "/x", "PNR_PROFILE_DIR": "/y"}):
            self.assertNotIn("PNR_LIVE_DIR", runtime.hermetic_env(rt))
            self.assertNotIn("PNR_PROFILE_DIR", runtime.kicad_env(rt))
        self.assertEqual(runtime.engine_runtime(self.dir), self.dir)


if __name__ == "__main__":
    unittest.main()
