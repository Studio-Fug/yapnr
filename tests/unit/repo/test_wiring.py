"""Every test file must be wired to a Bazel target (tools/check_test_wiring.py)."""

from __future__ import annotations

import os
import tempfile
import unittest

from tools import check_test_wiring
from tools.privacy_scan import list_repo_files
from tools.repo_root import workspace_root


def _write(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return rel


class FindOrphansTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _orphans(self, files):
        return [rel for rel, _ in check_test_wiring.find_orphans(self.root, files)]

    def test_glob_macro_wires_files_in_its_package(self):
        files = [
            _write(self.root, "tests/a/BUILD.bazel", "yapnr_py_tests(deps = [])\n"),
            _write(self.root, "tests/a/test_one.py"),
            _write(self.root, "tests/a/test_two.py"),
        ]
        self.assertEqual(self._orphans(files), [])

    def test_glob_macro_does_not_reach_subdirectories(self):
        files = [
            _write(self.root, "tests/a/BUILD.bazel", "yapnr_py_tests()\n"),
            _write(self.root, "tests/a/sub/test_deep.py"),
        ]
        self.assertEqual(self._orphans(files), ["tests/a/sub/test_deep.py"])

    def test_explicit_reference_wires_a_file(self):
        files = [
            _write(self.root, "tests/b/BUILD", 'py_test(name = "t", srcs = ["sub/test_x.py"])\n'),
            _write(self.root, "tests/b/sub/test_x.py"),
        ]
        self.assertEqual(self._orphans(files), [])

    def test_unreferenced_file_is_an_orphan(self):
        files = [
            _write(self.root, "tests/c/BUILD.bazel", 'py_test(name = "t", srcs = ["test_a.py"])\n'),
            _write(self.root, "tests/c/test_a.py"),
            _write(self.root, "tests/c/test_b.py"),
        ]
        self.assertEqual(self._orphans(files), ["tests/c/test_b.py"])

    def test_commented_out_reference_does_not_count(self):
        files = [
            _write(
                self.root, "tests/d/BUILD.bazel", '# srcs = ["test_a.py"]\n# yapnr_py_tests()\n'
            ),
            _write(self.root, "tests/d/test_a.py"),
        ]
        self.assertEqual(self._orphans(files), ["tests/d/test_a.py"])

    def test_file_without_any_package_is_an_orphan(self):
        files = [_write(self.root, "tests/e/test_lonely.py")]
        self.assertEqual(self._orphans(files), ["tests/e/test_lonely.py"])

    def test_files_outside_the_scope_are_ignored(self):
        files = [_write(self.root, "tools/test_helper.py")]
        self.assertEqual(self._orphans(files), [])


class TreeTest(unittest.TestCase):
    """The gate: no test file in the working tree is left unwired."""

    def test_no_orphans(self):
        root = workspace_root()
        files = list_repo_files(root)
        self.assertTrue(any(f.startswith("tests/") for f in files))
        self.assertEqual(check_test_wiring.find_orphans(root, files), [])


if __name__ == "__main__":
    unittest.main()
