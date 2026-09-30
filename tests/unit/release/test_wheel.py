"""The yapnr wheel (//release:wheel) has the expected contents and metadata.

//release:wheel_for_test is built from the same attributes as the release
wheel, with the fixed version 0.0.0.dev0 instead of the stamped release version.
"""

from __future__ import annotations

import glob
import os
import unittest
import zipfile
from email.parser import Parser

from packaging.requirements import Requirement


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
        self.assertEqual(os.path.basename(self.path), "yapnr-0.0.0.dev0-py3-none-any.whl")

    def test_package_files(self):
        for name in ["yapnr/__init__.py", "yapnr/__main__.py", "yapnr/cli.py"]:
            self.assertIn(name, self.names)
        # Only the package and its metadata: no tests, tools or repository files.
        for name in self.names:
            self.assertTrue(name.startswith(("yapnr/", self.dist_info + "/")), name)

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
