"""(c2, stretch) Three-channel filter bank: 7.0-7.6, 9.7-10.3 and 12.4-13.0 GHz from port 1 to
ports 2, 3 and 4 (design §11.4).

Fine re-validation (pitch 0.15 mm, 6 substrate cells): in-channel loss <= 3 dB, rejection of the
other channels >= 12 dB, |S11| <= -6 dB; on the optimization grid 2.5 dB, 15 dB and -8 dB.

Full run: `bazel test //tests/e2e/rf:test_filterbank3` (manual, tagged rf-stretch). See
yapnr/rf/testing.py (CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class Filterbank3CaseTest(CaseChecks, unittest.TestCase):
    CASE = "filterbank3"


if __name__ == "__main__":
    unittest.main()
