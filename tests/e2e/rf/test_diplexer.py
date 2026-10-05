"""(c) Filter bank: a two-channel diplexer, channel A to port 2 and channel B to port 3 (design
§11.4, `yapnr.rf.cases.diplexer`).

The pass criteria (in-channel loss, rejection of the other channel, the common port's match and
passivity, on the optimization grid and on the finer re-validation grid) are
`yapnr.rf.cases.CRITERIA["diplexer"]`; `python -m yapnr.rf.cases criteria diplexer` prints them.

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
