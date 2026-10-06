"""The KiCad geometry extractor's arc sweep works on KiCad 10's PCB_ARC API (no KiCad needed)."""

import importlib.util
import math
import unittest

from yapnr.viewer import server


def load_extract():
    spec = importlib.util.spec_from_file_location("viewer_extract", server.EXTRACT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Angle:
    def __init__(self, radians):
        self.radians = radians

    def AsRadians(self):
        return self.radians


class Kicad10Arc:
    """KiCad 10: PCB_ARC has GetAngle() and no GetArcAngle()."""

    def GetAngle(self):
        return Angle(-math.pi / 2)


class OlderArc:
    def GetArcAngle(self):
        return Angle(math.pi / 3)


class ArcSweepTest(unittest.TestCase):
    def test_kicad10_get_angle(self):
        self.assertAlmostEqual(load_extract().arc_sweep(Kicad10Arc()), -math.pi / 2)

    def test_fallback_get_arc_angle(self):
        self.assertAlmostEqual(load_extract().arc_sweep(OlderArc()), math.pi / 3)


if __name__ == "__main__":
    unittest.main()
