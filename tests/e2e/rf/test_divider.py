"""(a) Power divider/combiner: a 3-port equal split over 8.5-11.5 GHz (design §11.2).

Fine re-validation (pitch 0.15 mm, 6 substrate cells): |S11| <= -15 dB, |S21|, |S31| >= -3.6 dB,
||S21| - |S31|| <= 0.25 dB, passive; on the optimization grid |S11| <= -17 dB and |S21|, |S31| >=
-3.45 dB.

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
