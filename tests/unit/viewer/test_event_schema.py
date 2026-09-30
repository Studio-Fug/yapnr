import copy
import unittest
from event_schema import phase_frame

class PhaseFrameTest(unittest.TestCase):
    def setUp(self):
        self.event = dict(id="event-01", board_sha256="a" * 64,
                          data=dict(name="power", opens=0, violations=0))

    def test_named_phase_preserves_zero_counts_and_identity(self):
        frame = phase_frame(self.event)
        self.assertEqual(frame, dict(name="power", label_source="name",
                         board_sha256="a" * 64, event_id="event-01", opens=0, violations=0,
                         metric_sources=dict(opens=["data.opens"], violations=["data.violations"])))

    def test_probe_without_name_keeps_native_results(self):
        del self.event["data"]["name"]
        self.event["data"].update(opens=181, scope="Signal-only screening")
        before = copy.deepcopy(self.event)
        frame = phase_frame(self.event)
        self.assertEqual(frame["name"], "Completed phase (label unavailable)")
        self.assertEqual(frame["label_source"], "unavailable")
        self.assertEqual(frame["opens"], 181)
        self.assertEqual(self.event, before)

    def test_legacy_phase_alias_and_empty_label(self):
        self.event["data"].update(name="  ", phase="signal")
        self.assertEqual(phase_frame(self.event)["name"], "signal")
        self.assertEqual(phase_frame(self.event)["label_source"], "phase")

    def test_missing_native_counts_remain_errors(self):
        for field in ("opens", "violations"):
            event = copy.deepcopy(self.event)
            del event["data"][field]
            with self.assertRaises(KeyError):
                phase_frame(event)

    def test_missing_board_identity_remains_error(self):
        del self.event["board_sha256"]
        with self.assertRaises(KeyError):
            phase_frame(self.event)

class NativeMetricAliasTest(unittest.TestCase):
    def event(self, **data):
        return dict(id="diagnostic", board_sha256="a" * 64, data=data)

    def test_hash_bound_nested_counts_preserve_rejection(self):
        event = self.event(name="Rejected experiment", accepted=False, native_opens=46,
                           result=dict(sha256="a" * 64, after_opens=46, violations=3))
        before = copy.deepcopy(event)
        frame = phase_frame(event)
        self.assertEqual((frame["opens"], frame["violations"], frame["accepted"]), (46, 3, False))
        self.assertEqual(frame["metric_sources"]["opens"], ["data.native_opens", "data.result.after_opens"])
        self.assertEqual(event, before)

    def test_after_violations_not_baseline(self):
        frame = phase_frame(self.event(native_opens=265, result=dict(sha256="a" * 64,
                              after_opens=265, before_violations=0, after_violations=3)))
        self.assertEqual(frame["violations"], 3)

    def test_nested_zero_is_valid(self):
        frame = phase_frame(self.event(result=dict(sha256="a" * 64, after_opens=0, violations=0)))
        self.assertEqual((frame["opens"], frame["violations"]), (0, 0))
        self.assertNotIn("accepted", frame)

    def test_mismatched_or_null_hash_cannot_supply_counts(self):
        for sha in ("b" * 64, None):
            with self.subTest(sha=sha), self.assertRaises(KeyError):
                phase_frame(self.event(native_opens=46, result=dict(sha256=sha, after_opens=46, violations=0)))

    def test_inline_report_inherits_event_binding_without_duplicate_hash(self):
        event = self.event(name="ordinary plane leaf relocation", native_opens=40,
                           accepted=True, review="pending", result=dict(after_opens=40,
                           before_violations=0, after_violations=7, guards_pass=True))
        original = copy.deepcopy(event)
        frame = phase_frame(event)
        self.assertEqual((frame["opens"], frame["violations"], frame["accepted"]), (40, 7, True))
        self.assertEqual(frame["result_board_binding"], "event.board_sha256")
        self.assertEqual(frame["metric_sources"]["violations"], ["data.result.after_violations"])
        self.assertEqual(event, original)

    def test_inline_report_without_violations_never_defaults_to_zero(self):
        with self.assertRaises(KeyError):
            phase_frame(self.event(native_opens=39, result=dict(after_opens=39, guards_pass=True)))

    def test_inline_baseline_counts_never_substitute_after_counts(self):
        with self.assertRaises(KeyError):
            phase_frame(self.event(native_opens=39, result=dict(after_opens=39, before_violations=0)))

    def test_conflicting_inline_counts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting native opens"):
            phase_frame(self.event(native_opens=39, result=dict(after_opens=40, after_violations=0)))

    def test_conflicting_counts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting native opens"):
            phase_frame(self.event(opens=0, native_opens=46, violations=0))
        with self.assertRaisesRegex(ValueError, "Conflicting native violations"):
            phase_frame(self.event(opens=46, violations=0, result=dict(sha256="a" * 64, violations=3)))

    def test_invalid_counts_are_rejected(self):
        for value in (True, -1, 0.5, "0", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                phase_frame(self.event(opens=value, violations=0))

    def test_before_counts_are_not_final_counts(self):
        with self.assertRaises(KeyError):
            phase_frame(self.event(result=dict(sha256="a" * 64, before_opens=0, before_violations=0)))

if __name__ == "__main__":
    unittest.main()
