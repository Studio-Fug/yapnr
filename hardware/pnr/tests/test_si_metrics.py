"""SI edge metrics on synthetic waveforms: RC edges, staircase, ringback, overshoot, garbage."""

import math
import unittest

from pnr.si import metrics as M

RAIL, VIH, VIL = 5.0, 3.5, 1.5
T_RISE, T_FALL, T_END = 2e-9, 42e-9, 82e-9


def grid(dt=1e-11):
    n = int(round(T_END / dt)) + 1
    return [i * dt for i in range(n)]


def rc(t, tau, delay=0.0, rail=RAIL):
    out = []
    for x in t:
        if x < T_RISE + delay:
            v = 0.0
        elif x < T_FALL + delay:
            v = rail * (1 - math.exp(-(x - T_RISE - delay) / tau))
        else:
            hi = rail * (1 - math.exp(-(T_FALL - T_RISE) / tau))
            v = hi * math.exp(-(x - T_FALL - delay) / tau)
        out.append(v)
    return out


def ideal_driver(t):
    return rc(t, 0.2e-9)


def run(t, rx, drv=None):
    return M.edge_metrics(
        t,
        rx,
        drv or ideal_driver(t),
        rail=RAIL,
        vih=VIH,
        vil=VIL,
        t_rise=T_RISE,
        t_fall=T_FALL,
        t_end=T_END,
    )


class EdgeMetricsTest(unittest.TestCase):
    def test_rc_edge_matches_ln9_tau(self):
        t = grid()
        tau = 1e-9
        m = run(t, rc(t, tau, delay=0.5e-9))
        self.assertAlmostEqual(m["rise_10_90_ns"], math.log(9) * tau * 1e9, delta=0.005)
        self.assertAlmostEqual(m["fall_10_90_ns"], math.log(9) * tau * 1e9, delta=0.005)
        self.assertEqual(m["nonmonotonic_edges"], 0)
        self.assertLessEqual(m["overshoot_v"], 1e-9)
        self.assertAlmostEqual(m["undershoot_v"], 0.0, places=9)
        # delay: rx VIH crossing (0.5 ns + tau ln(5/1.5)) - driver 50 % crossing (0.2 ns ln 2)
        exp_delay = (0.5e-9 + tau * math.log(RAIL / (RAIL - VIH)) - 0.2e-9 * math.log(2)) * 1e9
        self.assertAlmostEqual(m["delay_rise_ns"], exp_delay, delta=0.005)
        self.assertAlmostEqual(
            m["ringback_margin_v"], RAIL * (1 - math.exp(-40)) * 0.9 - VIH, delta=0.2
        )
        self.assertAlmostEqual(
            m["pulse_width_error_ns"],
            (tau * math.log(RAIL / VIL) - tau * math.log(RAIL / (RAIL - VIH))) * 1e9,
            delta=0.01,
        )

    def test_staircase_is_slow_but_monotonic(self):
        t = grid()
        d = 0.5e-9  # the pixel sees the edge after the driver
        rx = [
            (
                0.0
                if x < T_RISE + d
                else (
                    (2.6 if x < T_RISE + d + 10e-9 else 5.0)
                    if x < T_FALL + d
                    else (2.4 if x < T_FALL + d + 10e-9 else 0.0)
                )
            )
            for x in t
        ]
        m = run(t, rx)
        self.assertAlmostEqual(m["rise_10_90_ns"], 10.0, delta=0.02)
        self.assertAlmostEqual(m["fall_10_90_ns"], 10.0, delta=0.02)
        self.assertEqual(m["nonmonotonic_edges"], 0)

    def test_ringback_into_band_is_nonmonotonic(self):
        t = grid()
        base = rc(t, 0.3e-9)
        rx = [v - (2.3 if T_RISE + 3e-9 < x < T_RISE + 5e-9 else 0.0) for x, v in zip(t, base)]
        m = run(t, rx)
        self.assertEqual(m["band_crossings"]["rise"]["vih_crossings"], 3)
        self.assertFalse(m["band_crossings"]["rise"]["monotonic"])
        self.assertTrue(m["band_crossings"]["fall"]["monotonic"])
        self.assertEqual(m["nonmonotonic_edges"], 1)
        self.assertLess(m["ringback_margin_v"], 0)

    def test_overshoot_and_undershoot(self):
        t = grid()
        rx = []
        for x, v in zip(t, rc(t, 0.3e-9)):
            if T_RISE + 1e-9 < x < T_RISE + 2e-9:
                v += 1.2
            if T_FALL + 1e-9 < x < T_FALL + 2e-9:
                v -= 0.9
            rx.append(v)
        m = run(t, rx)
        self.assertAlmostEqual(m["overshoot_v"], 1.2, delta=0.01)
        self.assertAlmostEqual(m["undershoot_v"], 0.9, delta=0.01)
        self.assertEqual(m["nonmonotonic_edges"], 0)

    def test_never_reaching_90_percent(self):
        t = grid()
        rx = rc(t, 1e-9, rail=4.0)  # settles at 80 % of the 5 V rail
        m = run(t, rx)
        self.assertIsNone(m["rise_10_90_ns"])
        self.assertIsNone(m["fall_10_90_ns"])  # never above 90 %: no fall start either
        j = M.judge(m, {"rise_10_90_ns": {"max": 25, "gate": True}})
        self.assertFalse(j["passed"])
        self.assertIsNone(j["gate_failures"][0]["value"])


class JudgeSanityTest(unittest.TestCase):
    def test_judge_gate_vs_report_only(self):
        limits = {
            "rise_10_90_ns": {"max": 25, "gate": True},
            "overshoot_v": {"max": 0.5, "gate": False},
            "nonmonotonic_edges": {"max": 0, "gate": True},
            "ringback_margin_v": {"min": 0.0, "gate": False},
        }
        j = M.judge(
            dict(rise_10_90_ns=3.3, overshoot_v=1.1, nonmonotonic_edges=0, ringback_margin_v=0.9),
            limits,
        )
        self.assertTrue(j["passed"])
        self.assertEqual([r["metric"] for r in j["report_violations"]], ["overshoot_v"])
        self.assertAlmostEqual(j["report_violations"][0]["margin"], -0.6)
        j = M.judge(
            dict(rise_10_90_ns=30.0, overshoot_v=0.1, nonmonotonic_edges=1, ringback_margin_v=0.9),
            limits,
        )
        self.assertFalse(j["passed"])
        self.assertEqual(
            sorted(r["metric"] for r in j["gate_failures"]), ["nonmonotonic_edges", "rise_10_90_ns"]
        )

    def test_sanity_rejects_garbage(self):
        t = grid()
        good_drv, good_rx = ideal_driver(t), rc(t, 1e-9)
        kw = dict(rail=RAIL, t_rise=T_RISE, t_fall=T_FALL, t_end=T_END)
        self.assertIsNone(M.sanity(t, good_rx, good_drv, **kw))
        self.assertIn("flat", M.sanity(t, [0.001] * len(t), good_drv, **kw))
        self.assertIn("driver pin", M.sanity(t, good_rx, [0.001] * len(t), **kw))
        self.assertIn("driver pin", M.sanity(t, good_rx, [v * 0.5 for v in good_drv], **kw))
        stuck_high = [RAIL if x >= T_RISE else 0.0 for x in t]
        self.assertIn("falling window", M.sanity(t, good_rx, stuck_high, **kw))
        self.assertIn("too short", M.sanity(t[:10], good_rx[:10], good_drv[:10], **kw))

    def test_crossings_and_interpolation(self):
        t = [0.0, 1.0, 2.0, 3.0]
        v = [0.0, 2.0, 0.0, 2.0]
        self.assertEqual(M.crossings(t, v, 1.0), [(0.5, 1), (1.5, -1), (2.5, 1)])
        self.assertEqual(M.crossings(t, v, 1.0, 1.0, 2.0), [(1.5, -1)])
        self.assertAlmostEqual(M.value_at(t, v, 2.25), 0.5)
        self.assertEqual(M.value_at(t, v, -1), 0.0)

    def test_decimate_wave_keeps_extrema(self):
        t = grid(1e-11)
        rx = rc(t, 0.3e-9)
        rx[5000] = 9.0
        wt, wy = M.decimate_wave(t, {"rx": rx, "drv": ideal_driver(t)}, max_points=500)
        self.assertLessEqual(len(wt), 502)
        self.assertIn(9.0, wy["rx"])
        self.assertEqual(wt[0], t[0])
        self.assertEqual(wt[-1], t[-1])


if __name__ == "__main__":
    unittest.main()
