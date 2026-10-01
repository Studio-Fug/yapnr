"""The committed synthetic atopile fixture equals what yapnr.frontends.atopile.testing writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yapnr.frontends.atopile import testing

# tests/fixtures/atopile/synthetic, next to this file in the source tree and in Bazel's runfiles.
COMMITTED = Path(__file__).parents[2] / "fixtures/atopile/synthetic"


class FixtureTest(unittest.TestCase):
    def test_fixture_is_current(self):
        committed = COMMITTED
        with tempfile.TemporaryDirectory() as tmp:
            fresh = testing.write_fixture(Path(tmp) / "synthetic")
            names = sorted(str(p.relative_to(fresh)) for p in fresh.rglob("*") if p.is_file())
            ours = sorted(
                str(p.relative_to(committed)) for p in committed.rglob("*") if p.is_file()
            )
            self.assertEqual(ours, names)
            for name in names:
                self.assertEqual(
                    (committed / name).read_bytes(),
                    (fresh / name).read_bytes(),
                    f"{name}: regenerate with testing.write_fixture",
                )


if __name__ == "__main__":
    unittest.main()
