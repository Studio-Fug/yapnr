import unittest, math
from pnr.route.detail.coupled import relaxed_centerlines, offset_path


class PairSmoothTest(unittest.TestCase):
    def test_clear_shortcut_removes_grid_hooks(self):
        raw = [(0, 0), (2, 0), (2, 0.02), (4, 2), (6, 2)]
        paths = relaxed_centerlines(raw, lambda a, z: True, 0.35)
        self.assertEqual(paths[0], [(0, 0), (6, 2)])

    def test_retains_endpoint_heading_and_checks_new_edges(self):
        calls = []

        def clear(a, z):
            calls.append((a, z))
            if math.dist(a, (0, 0)) < 1e-8:
                return abs(z[1]) < 1e-8 and z[0] > 0
            if math.dist(z, (6, 2)) < 1e-8:
                return abs(a[1] - 2) < 1e-8 and a[0] < 6
            return True

        raw = [(0, 0), (2, 0), (2, 0.02), (4, 2), (6, 2)]
        paths = relaxed_centerlines(raw, clear, 0.35)
        self.assertTrue(paths)
        self.assertTrue(all(all(clear(a, z) for a, z in zip(p, p[1:])) for p in paths))
        self.assertTrue(any(len(offset_path(p, 0.175)) >= 2 for p in paths))

    def test_blocked_shortcuts_and_bevels_do_not_pass(self):
        raw = [(0, 0), (2, 0), (2, 2), (4, 2)]
        edges = set(zip(raw, raw[1:]))
        paths = relaxed_centerlines(raw, lambda a, z: (a, z) in edges, 0.35)
        self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
