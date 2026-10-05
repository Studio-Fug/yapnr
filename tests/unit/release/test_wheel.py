"""The yapnr wheel (//release:wheel) has the expected contents and metadata.

//release:wheel_for_test is built from the same attributes as the release
wheel, with the fixed version 0.0.0.dev0 instead of the stamped release version.
It carries yapnr.rf's native FDTD library for the platform it was built on and
is tagged for that platform (release/wheel.bzl); the library in it loads, with
numpy's arithmetic (`yapnr.rf.fdtd.native_kernel.Kernel`).
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import sys
import tempfile
import unittest
import zipfile
from email.parser import Parser

from packaging.requirements import Requirement

# The wheel's platform tag by (sys.platform, machine) of the host that built it.
PLATFORM_TAGS = {
    ("darwin", "arm64"): "macosx_11_0_arm64",
    ("darwin", "x86_64"): "macosx_11_0_x86_64",
    ("linux", "aarch64"): "manylinux_2_34_aarch64",
    ("linux", "x86_64"): "manylinux_2_34_x86_64",
}
LIBRARY = "yapnr/rf/libyapnr_fdtd.so"


def _runfile(*parts):
    srcdir = os.environ.get("TEST_SRCDIR")
    if srcdir:
        return os.path.join(srcdir, os.environ.get("TEST_WORKSPACE", "_main"), *parts)
    return os.path.join(os.path.dirname(__file__), "..", "..", "..", *parts)


def _requirement_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [
            line.split("#", 1)[0].strip()
            for line in handle
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        ]


class WheelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheels = glob.glob(_runfile("release", "*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"expected one wheel in the runfiles, found {wheels}")
        cls.path = wheels[0]
        cls.zip = zipfile.ZipFile(cls.path)
        cls.names = cls.zip.namelist()
        dist_info = "yapnr-0.0.0.dev0.dist-info"
        cls.metadata = Parser().parsestr(cls.zip.read(f"{dist_info}/METADATA").decode())
        cls.entry_points = cls.zip.read(f"{dist_info}/entry_points.txt").decode()
        cls.dist_info = dist_info

    @classmethod
    def tearDownClass(cls):
        cls.zip.close()

    def test_file_name(self):
        tag = PLATFORM_TAGS.get((sys.platform, platform.machine().lower()))
        if tag is None:
            self.skipTest(f"no wheel platform for {sys.platform} {platform.machine()}")
        self.assertEqual(os.path.basename(self.path), f"yapnr-0.0.0.dev0-py3-none-{tag}.whl")
        wheel = self.zip.read(f"{self.dist_info}/WHEEL").decode()
        self.assertIn(f"Tag: py3-none-{tag}", wheel)
        self.assertIn("Root-Is-Purelib: false", wheel)

    def test_package_files(self):
        for name in ["yapnr/__init__.py", "yapnr/__main__.py", "yapnr/cli.py"]:
            self.assertIn(name, self.names)
        # Only the package and its metadata: no tests, tools or repository files.
        for name in self.names:
            self.assertTrue(name.startswith(("yapnr/", self.dist_info + "/")), name)

    def test_rf_package(self):
        """yapnr.rf with its native library (where the loader looks beside the package) and
        the library's C sources; not the test helpers."""
        for name in [
            "yapnr/rf/__init__.py",
            "yapnr/rf/problem.py",
            "yapnr/rf/fdtd/engine.py",
            "yapnr/rf/fdtd/native_kernel.py",
            "yapnr/rf/fdtd/native/fdtd.c",
            "yapnr/rf/fdtd/native/fdtd_kernels.h",
            LIBRARY,
        ]:
            self.assertIn(name, self.names)
        self.assertNotIn("yapnr/rf/testing.py", self.names)

    def test_native_library_loads(self):
        """The wheel's library loads and passes the loader's checks (ABI, structure sizes,
        no fused or reassociated arithmetic, built from the wheel's own sources)."""
        from yapnr.rf.fdtd import native_kernel

        out = tempfile.mkdtemp(prefix="yapnr-wheel-")
        self.addCleanup(shutil.rmtree, out, True)
        path = self.zip.extract(LIBRARY, out)
        for src in ("fdtd.c", "fdtd_kernels.h"):
            self.assertEqual(
                self.zip.read(f"yapnr/rf/fdtd/native/{src}"),
                open(
                    os.path.join(os.path.dirname(native_kernel.__file__), "native", src), "rb"
                ).read(),
                src,
            )
        import ctypes

        kernel = native_kernel.Kernel(ctypes.CDLL(path), path)
        self.assertEqual(kernel.src_sha, native_kernel.source_sha256())
        self.assertTrue(kernel.isa)

    def test_license(self):
        self.assertEqual(self.metadata["License"], "AGPL-3.0-or-later")
        self.assertIn(f"{self.dist_info}/LICENSE", self.names)
        text = self.zip.read(f"{self.dist_info}/LICENSE").decode()
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)

    def test_metadata(self):
        self.assertEqual(self.metadata["Name"], "yapnr")
        self.assertEqual(self.metadata["Version"], "0.0.0.dev0")
        self.assertEqual(self.metadata["Requires-Python"], ">=3.11")

    def test_requires_dist_is_requirements_runtime_in(self):
        wanted = _requirement_lines(_runfile("requirements-runtime.in"))
        self.assertEqual(wanted, ["torch>=2.2,<2.4", "numpy>=1.26,<2", "pyyaml>=6"])

        def normal(requirements):
            parsed = [Requirement(r) for r in requirements]
            return sorted((r.name, str(r.specifier), str(r.marker)) for r in parsed)

        self.assertEqual(normal(self.metadata.get_all("Requires-Dist")), normal(wanted))

    def test_console_script(self):
        self.assertIn("[console_scripts]", self.entry_points)
        self.assertIn("yapnr = yapnr.cli:main", self.entry_points)


if __name__ == "__main__":
    unittest.main()
