"""(b) Antenna: a 1-port microstrip-fed radiator over ground, 9.7-10.3 GHz (design §11.3).

Fine re-validation (pitch 0.2 mm, 9 substrate cells): |S11| <= -10 dB over 9.75-10.25 GHz and a
radiated fraction >= 0.60 at 9.75, 10.0 and 10.25 GHz; on the optimization grid |S11| <= -10 dB
over 9.7-10.3 GHz and a radiated fraction >= 0.65 at the four objective frequencies.

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
