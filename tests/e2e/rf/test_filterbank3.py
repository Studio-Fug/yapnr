"""(c2) Three-channel filter bank from port 1 to ports 2, 3 and 4 (design §11.4,
`yapnr.rf.cases.filterbank3`).

The pass criteria are `yapnr.rf.cases.CRITERIA["filterbank3"]`; `python -m yapnr.rf.cases
criteria filterbank3` prints them.

Full run: `bazel test //tests/e2e/rf:test_filterbank3` (manual). See yapnr/rf/testing.py
(CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class Filterbank3CaseTest(CaseChecks, unittest.TestCase):
    CASE = "filterbank3"


if __name__ == "__main__":
    unittest.main()
