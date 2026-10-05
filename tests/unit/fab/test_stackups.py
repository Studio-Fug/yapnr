"""Stackup views: microstrip, the yapnr.rf adapter and the nominal widths of Appendix A."""

from __future__ import annotations

import unittest

from yapnr.fab import stackups

# Appendix A of docs/design/fab-and-ordering.md: W50 on F.Cu over the next copper layer
# (Hammerstad-Jensen, zero thickness, no mask), to 0.005 mm.
W50 = {
    "oshpark-2l-fr4": 2.88,
    "oshpark-4l-fr408hr": 0.44,
    "oshpark-4l-em528": 0.42,
    "oshpark-4l-mixed": 0.44,
    "oshpark-6l-fr408hr": 0.265,  # Appendix A rounds it up to 0.27
    "jlc04161h-7628": 0.40,
    "jlc04161h-2116": 0.23,
    "jlc04161h-3313": 0.20,
    "jlc04161h-1080": 0.16,
    "jlc06161h-7628": 0.40,
    "jlc06161h-3313": 0.20,
    "pcbway-4l-7628": 0.34,
    "pcbway-ro4003c-0.813": 1.82,
    "pcbway-ro4350b-0.508": 1.11,
}


class MicrostripTest(unittest.TestCase):
    def test_oshpark_4l_view(self):
        m = stackups.load("oshpark-4l-fr408hr").microstrip("F.Cu")
        self.assertEqual((m.reference, m.h_mm, m.er, m.er_freq_hz), ("In1.Cu", 0.1999, 3.61, 1e9))
        self.assertAlmostEqual(m.t_um, 43.18, 2)
        # Df 0.009 from OSH Park's construction drawing (O-4l-stack), no stand-in needed
        self.assertEqual(m.tan_delta, 0.009)

    def test_bottom_layer_view_mirrors_the_top(self):
        s = stackups.load("oshpark-4l-fr408hr")
        top, bottom = s.microstrip("F.Cu"), s.microstrip("B.Cu")
        self.assertEqual(bottom.reference, "In2.Cu")
        self.assertEqual((bottom.h_mm, bottom.er), (top.h_mm, top.er))

    def test_inner_layers_and_mixed_dielectrics_are_refused(self):
        with self.assertRaises(stackups.StackupError):
            stackups.load("oshpark-4l-fr408hr").microstrip("In1.Cu")

    def test_plies_of_one_material_add_up(self):
        m = stackups.load("jlc06161h-2116c").microstrip("F.Cu")
        self.assertAlmostEqual(m.h_mm, 0.127 + 0.1194, 6)

    def test_rf_spec_refuses_an_unpublished_df_without_the_prior(self):
        m = stackups.load("jlc04161h-7628").microstrip("F.Cu")
        with self.assertRaises(stackups.StackupError):
            m.rf_stackup_spec(10.0)
        spec = m.rf_stackup_spec(10.0, use_prior=True)
        self.assertEqual(
            {k: spec[k] for k in ("er", "tan_delta", "h_mm", "f_ref_ghz")},
            {"er": 4.4, "tan_delta": 0.017, "h_mm": 0.2104, "f_ref_ghz": 10.0},
        )
        self.assertEqual(spec["provenance"]["priors_used"], ["tan_delta"])

    def test_oshpark_rf_spec_needs_no_prior(self):
        spec = stackups.load("oshpark-4l-fr408hr").microstrip("F.Cu").rf_stackup_spec(10.0)
        self.assertEqual((spec["er"], spec["tan_delta"], spec["h_mm"]), (3.61, 0.009, 0.1999))
        self.assertEqual(spec["provenance"]["priors_used"], [])
        self.assertEqual(spec["provenance"]["er_freq_hz"], 1e9)

    def test_rogers_uses_the_design_dk_and_needs_no_prior(self):
        spec = stackups.load("pcbway-ro4003c-0.813").microstrip("F.Cu").rf_stackup_spec(10.0)
        self.assertEqual((spec["er"], spec["tan_delta"], spec["h_mm"]), (3.55, 0.0027, 0.813))
        self.assertEqual(spec["provenance"]["priors_used"], [])

    def test_nominal_widths_match_appendix_a(self):
        for sid, width in W50.items():
            got = stackups.load(sid).microstrip("F.Cu").w50_mm()
            self.assertAlmostEqual(got, width, delta=0.0051, msg=sid)

    def test_em528_detunes_a_line_designed_for_fr408hr(self):
        """Design §9: 48.3 ohm and 1.9 % shorter guided wavelength (HJ, uncoated)."""
        fr = stackups.load("oshpark-4l-fr408hr").microstrip("F.Cu")
        em = stackups.load("oshpark-4l-em528").microstrip("F.Cu")
        w = fr.w50_mm()
        z_em, eff_em = stackups.hammerstad_jensen(w, em.h_mm, em.er)
        _, eff_fr = stackups.hammerstad_jensen(w, fr.h_mm, fr.er)
        self.assertAlmostEqual(z_em, 48.3, delta=0.05)
        self.assertAlmostEqual((eff_em / eff_fr) ** 0.5 - 1, 0.019, delta=0.001)

    def test_cross_section_and_summary(self):
        s = stackups.load("oshpark-6l-fr408hr")
        xs = s.cross_section()
        self.assertEqual([x["name"] for x in xs if x["kind"] == "copper"], s.copper_layers())
        self.assertEqual(len(s.copper_layers()), 6)
        self.assertIn("FR408HR 106 75% 0.1107 mm Dk 3.23 @ 1 GHz", s.summary())

    def test_rf_rules_of_a_profile(self):
        self.assertEqual(
            stackups.rf_rules("oshpark-4l"), {"min_width_mm": 0.127, "min_space_mm": 0.127}
        )
        self.assertEqual(
            stackups.rf_rules("jlc-pofv"), {"min_width_mm": 0.127, "min_space_mm": 0.127}
        )

    def test_measured_overlays_are_reserved(self):
        with self.assertRaises(NotImplementedError):
            stackups.load("oshpark-4l-fr408hr", measured="coupons.json")


if __name__ == "__main__":
    unittest.main()
