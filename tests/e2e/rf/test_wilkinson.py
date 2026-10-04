"""(a2) Wilkinson-type combiner/divider: equal split, matched and isolated outputs, with a fixed
100 Ω isolation resistor as a lumped element (design §11.2, `yapnr.rf.cases.wilkinson`).

The pass criteria (on the optimization grid and on the finer re-validation grids) are
`yapnr.rf.cases.CRITERIA["wilkinson"]`; `python -m yapnr.rf.cases criteria wilkinson` prints
them.

Full run: `bazel test //tests/e2e/rf:test_wilkinson` (manual); CI runs the smoke variant
`:test_wilkinson_smoke`. See yapnr/rf/testing.py (CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class WilkinsonCaseTest(CaseChecks, unittest.TestCase):
    CASE = "wilkinson"


if __name__ == "__main__":
    unittest.main()
