"""(a) Power divider/combiner: a 3-port equal split (design §11.2, `yapnr.rf.cases.divider`).

The pass criteria (on the optimization grid and on the finer re-validation grid) are
`yapnr.rf.cases.CRITERIA["divider"]`; `python -m yapnr.rf.cases criteria divider` prints them.

Full run: `bazel test //tests/e2e/rf:test_divider` (manual); CI runs the smoke variant
`:test_divider_smoke`. See yapnr/rf/testing.py (CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class DividerCaseTest(CaseChecks, unittest.TestCase):
    CASE = "divider"


if __name__ == "__main__":
    unittest.main()
