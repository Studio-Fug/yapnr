"""No third-party part data in the repository (tools/check_part_data.py)."""

from __future__ import annotations

import os
import tempfile
import unittest

from tools import check_part_data
from tools.privacy_scan import list_repo_files
from tools.repo_root import workspace_root

EASY = "easy" + "eda"  # assembled, so this file is not itself a finding


def _write(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return rel


class FindPartDataTest(unittest.TestCase):
    def test_generated_parts_and_raw_cache_are_found(self):
        with tempfile.TemporaryDirectory() as root:
            generated = (
                'component P_package:\n    trait is_auto_generated<system="ato_part",'
                f' source="{EASY}:C1", date="x", checksum="y">\n'
            )
            files = [
                _write(root, "a/parts/P/P.ato", generated),
                _write(root, "a/parts/P/fp.kicad_mod"),
                _write(root, "a/parts/P/model.STEP"),
                _write(root, "a/parts/P/NOTES.md"),
                _write(root, f"b/build/cache/parts/{EASY}/C1/C1.json", "{}"),
                _write(root, "c/parts/Mine/Mine.ato", "component Mine_package:\n"),
                _write(root, "c/parts/Mine/fp.kicad_mod"),
            ]
            found = dict(check_part_data.find_part_data(root, files))
            self.assertEqual(
                sorted(found),
                sorted(
                    [
                        "a/parts/P/P.ato",
                        "a/parts/P/fp.kicad_mod",
                        "a/parts/P/model.STEP",
                        f"b/build/cache/parts/{EASY}/C1/C1.json",
                    ]
                ),
            )


class RepositoryTest(unittest.TestCase):
    def test_repository_has_no_part_data(self):
        root = workspace_root()
        findings = check_part_data.find_part_data(root, list_repo_files(root))
        self.assertEqual(findings, [], "\n".join(f"{f}: {r}" for f, r in findings))


if __name__ == "__main__":
    unittest.main()
