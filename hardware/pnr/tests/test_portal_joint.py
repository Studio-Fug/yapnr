import unittest
from pnr.route.detail.portal_joint import Portal, portal_conflict, solve_portal_region
from pnr.route.detail.regional import Request
from pnr.route.detail.joint import conflicts
from pnr.route.detail.layered import primitives


class PortalTest(unittest.TestCase):
    def test_via_collision_on_every_layer_and_same_net_drills(self):
        a = Request("a", "n", [], [])
        b = Request("b", "other", [], [])
        port = Portal(a, 0, [(0, 0, 0), (0.3, 0, 0)], (0, 1, 2), True, 1)
        crossing = Portal(b, 1, [(0.3, -1, 2), (0.3, 1, 2)], (2,), False, 1)
        self.assertTrue(portal_conflict(port, crossing))
        close = Portal(a, 1, [(0.6, 0, 0)], (0, 1, 2), True, 1)
        self.assertTrue(portal_conflict(port, close))
        self.assertFalse(portal_conflict(port, port))

    def test_full_transaction_preserves_original_endpoints(self):
        rs = [Request("a", "A", [(0, 0)], [(4, 0)]), Request("b", "B", [(2, -2)], [(2, 2)])]
        result = solve_portal_region(
            rs,
            (-1, -3, 5, 3),
            lambda *a: True,
            lambda *a: True,
            pitch=0.2,
            max_expansions=3000,
            max_seconds=30,
        )
        self.assertEqual(result.status, "routed")
        self.assertFalse(conflicts(rs, result.paths))
        for r in rs:
            self.assertEqual(result.paths[r.name][0][:2], r.sources[0])
            self.assertEqual(result.paths[r.name][-1][:2], r.targets[0])

    def test_existing_inner_access_does_not_create_escape_vias(self):
        r = Request("a", "A", [(0, 0)], [(2, 0)])
        result = solve_portal_region(
            [r],
            (-1, -1, 3, 1),
            lambda r, la, a, b: la == 1,
            lambda *a: False,
            terminal_layers=lambda r, p: (1,),
            pitch=0.2,
            max_expansions=1000,
        )
        self.assertEqual(result.status, "routed")
        self.assertTrue(
            all(kind == "track" and la == 1 for kind, la, a, b in primitives(result.paths["a"]))
        )

    def test_blocked_access_never_emits_partial_copper(self):
        r = Request("a", "A", [(0, 0)], [(2, 0)])
        result = solve_portal_region(
            [r], (-1, -1, 3, 1), lambda *a: True, lambda *a: False, pitch=0.2, max_expansions=1000
        )
        self.assertEqual(result.status, "portal_assignment_incomplete")
        self.assertFalse(result.paths)

    def test_expired_enumeration_is_honest_timeout(self):
        r = Request("a", "A", [(0, 0)], [(2, 0)])
        result = solve_portal_region(
            [r], (-1, -1, 3, 1), lambda *a: True, lambda *a: True, max_seconds=1e-12
        )
        self.assertEqual(result.status, "time_budget")
        self.assertFalse(result.paths)


if __name__ == "__main__":
    unittest.main()
