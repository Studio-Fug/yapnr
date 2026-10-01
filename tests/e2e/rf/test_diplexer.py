"""(c) Filter bank: a two-channel diplexer, A 7.6-8.4 GHz to port 2, B 11.6-12.4 GHz to port 3
(design §11.4).

Fine re-validation (pitch 0.15 mm, 6 substrate cells): in-channel loss <= 2 dB, rejection of the
other channel >= 15 dB, |S11| <= -8 dB in both channels, passive; on the optimization grid 1.5 dB,
18 dB and -10 dB.

Full run: `bazel test //tests/e2e/rf:test_diplexer` (manual); CI runs the smoke variant
`:test_diplexer_smoke`. See yapnr/rf/testing.py (CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class DiplexerCaseTest(CaseChecks, unittest.TestCase):
    CASE = "diplexer"


if __name__ == "__main__":
    unittest.main()
