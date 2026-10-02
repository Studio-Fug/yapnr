"""(b) Antenna: a 1-port microstrip-fed radiator over ground (design §11.3,
`yapnr.rf.cases.antenna`).

The pass criteria (on the optimization grid and on the finer re-validation grid: |S11|, the
radiated fraction, the power balance and passivity) are `yapnr.rf.cases.CRITERIA["antenna"]`;
`python -m yapnr.rf.cases criteria antenna` prints them.

Full run: `bazel test //tests/e2e/rf:test_antenna` (manual); CI runs the smoke variant
`:test_antenna_smoke`. See yapnr/rf/testing.py (CaseChecks).
"""

from __future__ import annotations

import unittest

from yapnr.rf.testing import CaseChecks


class AntennaCaseTest(CaseChecks, unittest.TestCase):
    CASE = "antenna"


if __name__ == "__main__":
    unittest.main()
