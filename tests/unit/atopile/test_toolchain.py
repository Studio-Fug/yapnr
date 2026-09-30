"""The atopile locks and pins, and how the environment is discovered (no network, no uv)."""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yapnr.frontends.atopile import ATOPILE_VERSION, toolchain


def requirements(lock: Path):
    """{name: (version, [hashes])} of a hashed lock."""
    out = {}
    current = None
    for line in lock.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-\[\],]+)==([^ ;\\]+)", line)
        if match:
            current = match.group(1).split("[")[0].lower()
            out[current] = (match.group(2), [])
        elif current and "--hash=sha256:" in line:
            out[current][1].append(line.split("--hash=sha256:")[1].split()[0])
    return out


class LockTest(unittest.TestCase):
    def test_pins(self):
        pin = toolchain.pins()
        self.assertEqual(pin["atopile"], ATOPILE_VERSION)
        self.assertRegex(pin["atopile_sdist"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(pin["python"].startswith("3.14."))
        self.assertRegex(pin["scikit_build_core"]["commit"], r"^[0-9a-f]{40}$")
        text = (toolchain.LOCK_DIR / "requirements.in").read_text()
        self.assertIn(f"atopile=={ATOPILE_VERSION}", text)

    def test_every_platform_has_a_fully_hashed_lock(self):
        for platform in toolchain.PLATFORMS:
            with self.subTest(platform=platform):
                reqs = requirements(toolchain.lock_path(platform))
                self.assertGreater(len(reqs), 100)
                for name, (_version, hashes) in reqs.items():
                    self.assertTrue(hashes, f"{platform}: {name} has no hash")
                if platform in toolchain.SELF_BUILT:
                    self.assertNotIn("atopile", reqs)
                else:
                    self.assertEqual(reqs["atopile"][0], ATOPILE_VERSION)

    def test_locks_agree_across_platforms(self):
        versions = {}
        for platform in toolchain.PLATFORMS:
            for name, (version, _hashes) in requirements(toolchain.lock_path(platform)).items():
                versions.setdefault(name, set()).add(version)
        drift = {name: v for name, v in versions.items() if len(v) > 1}
        self.assertEqual(drift, {})

    def test_build_requirements_lock(self):
        reqs = requirements(toolchain.LOCK_DIR / "build-requirements-linux-aarch64.lock")
        self.assertEqual(reqs["ziglang"][0], "0.16.0")
        for name in ("hatchling", "hatch-vcs", "nanobind", "cmake", "ninja"):
            self.assertIn(name, reqs)

    def test_lock_id(self):
        self.assertRegex(toolchain.lock_id("linux-x86_64"), r"^[0-9a-f]{12}$")
        with self.assertRaises(toolchain.ToolchainError):
            toolchain.lock_path("plan9-mips")


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(toolchain, "_config_python", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_env(self, version: str) -> Path:
        venv = self.root / f"venv-{version}"
        python = venv / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\nprint({version!r})\n")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (venv / f"lib/python3.14/site-packages/atopile-{version}.dist-info").mkdir(parents=True)
        return python

    def test_host_platform_names(self):
        cases = {
            ("darwin", "arm64"): "darwin-arm64",
            ("linux", "aarch64"): "linux-aarch64",
            ("linux", "x86_64"): "linux-x86_64",
            ("linux", "AMD64"): "linux-x86_64",
        }
        for (plat, machine), expected in cases.items():
            with mock.patch.object(sys, "platform", plat), mock.patch(
                "platform.machine", return_value=machine
            ):
                self.assertEqual(toolchain.host_platform(), expected)

    def test_environment_variable_first(self):
        python = self.fake_env(ATOPILE_VERSION)
        found = toolchain.discover({toolchain.ENV_PYTHON: str(python)}, root=self.root)
        self.assertEqual(found.python, python)
        self.assertEqual(found.version, ATOPILE_VERSION)
        self.assertEqual(found.source, toolchain.ENV_PYTHON)

    def test_wrong_version_or_missing_explicit_interpreter_is_an_error(self):
        other = self.fake_env("0.15.9")
        with self.assertRaisesRegex(toolchain.ToolchainError, "0.15.9"):
            toolchain.discover({toolchain.ENV_PYTHON: str(other)}, root=self.root)
        with self.assertRaisesRegex(toolchain.ToolchainError, "does not exist"):
            toolchain.discover({toolchain.ENV_PYTHON: str(self.root / "nope")}, root=self.root)

    def test_setup_environment_of_the_current_lock(self):
        env_dir = self.root / toolchain.lock_id()
        python = self.fake_env(ATOPILE_VERSION)
        (env_dir / "venv/bin").mkdir(parents=True)
        os.symlink(python, env_dir / "venv/bin/python")
        with mock.patch.object(toolchain, "IMAGE_PYTHON", self.root / "no-image/python"):
            with self.assertRaises(toolchain.ToolchainError):  # no marker: incomplete
                toolchain.discover({}, root=self.root)
            (env_dir / toolchain.MARKER).write_text("{}")
            found = toolchain.discover({}, root=self.root)
        self.assertTrue(found.source.startswith("yapnr atopile setup"))

    def test_static_status_runs_nothing(self):
        python = self.fake_env(ATOPILE_VERSION)
        report = toolchain.static_status({toolchain.ENV_PYTHON: str(python)})
        self.assertEqual((report["atopile"], report["ok"]), (ATOPILE_VERSION, True))
        report = toolchain.static_status({toolchain.ENV_PYTHON: str(self.fake_env("0.15.9"))})
        self.assertFalse(report["ok"])

    def test_default_root(self):
        self.assertEqual(toolchain.default_root({"XDG_CACHE_HOME": "/c"}), Path("/c/yapnr/atopile"))
        self.assertEqual(toolchain.default_root({toolchain.ENV_HOME: "/x"}), Path("/x"))

    def test_setup_refuses_other_hosts_and_missing_wheels(self):
        other = "linux-x86_64" if toolchain.host_platform() != "linux-x86_64" else "darwin-arm64"
        with self.assertRaises(toolchain.ToolchainError):
            toolchain.setup(root=self.root, platform=other)
        if toolchain.host_platform() in toolchain.SELF_BUILT:
            with self.assertRaisesRegex(toolchain.ToolchainError, "build_wheel"):
                toolchain.setup(root=self.root)


if __name__ == "__main__":
    unittest.main()
