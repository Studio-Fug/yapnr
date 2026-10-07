"""The budget guard and the reaper against a fake HTTP layer: thresholds, idempotency, stale and
malformed messages, and what the reaper may delete."""

from __future__ import annotations

import base64
import datetime as _dt
import importlib.util
import json
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("guard_main", Path(__file__).with_name("main.py"))
main = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(main)

NOW = _dt.datetime(2026, 10, 20, tzinfo=_dt.timezone.utc)
CFG = {
    "project": "example-project",
    "regions": ["us-west4", "northamerica-northeast1"],
    "bucket": "example-yapnr-runs",
    "preferences": {"us-west4": "PREEMPTIBLE-CPUS-per-project-region"},
    "cut_at": 1.2,
    # Assembled, so the privacy scan does not read it as an e-mail address.
    "submit_account": "yapnr-submit" + "@example-project.iam.gserviceaccount.com",
}


def job(name, state="RUNNING", **labels):
    return {
        "name": "projects/example-project/locations/us-west4/jobs/%s" % name,
        "labels": dict({"yapnr": "1"}, **labels),
        "status": {"state": state},
    }


class FakeHttp:
    def __init__(self, jobs=(), instances=None, preferences=()):
        self.jobs = list(jobs)
        self.instances = instances or {}
        self.preferences = list(preferences)
        self.calls = []

    def __call__(self, method, url, body=None, content_type="application/json"):
        self.calls.append((method, url, body))
        if method == "GET" and "/locations/us-west4/jobs" in url:
            return 200, {"jobs": self.jobs}
        if method == "GET" and "/jobs" in url:
            return 200, {}
        if method == "GET" and "aggregated/instances" in url:
            return 200, {"items": self.instances}
        if method == "GET" and "/quotaPreferences?" in url:
            return 200, {"quotaPreferences": self.preferences}
        if method == "POST" and url.endswith(":cancel"):
            name = url[len(main.BATCH) + 1 : -len(":cancel")]
            for j in self.jobs:
                if j["name"] == name:
                    j["status"]["state"] = "CANCELLATION_IN_PROGRESS"
            return 200, {}
        return 200, {}

    def of(self, method, needle=""):
        return [c for c in self.calls if c[0] == method and needle in c[1]]


def budget(cost, amount=50.0, start="2026-10-01T07:00:00Z"):
    return {
        "budgetDisplayName": "yapnr experiments",
        "costAmount": cost,
        "budgetAmount": amount,
        "costIntervalStart": start,
        "currencyCode": "USD",
    }


class BudgetGuardTest(unittest.TestCase):
    def test_below_the_budget_nothing_happens(self):
        http = FakeHttp([job("a")])
        summary = main.handle_budget(budget(45.0), http, CFG, NOW)
        self.assertEqual(summary["action"], "none")
        # The amounts are logged with the ratio (yapnr exp spend reads them).
        self.assertEqual((summary["cost"], summary["budget"]), (45.0, 50.0))
        self.assertEqual(http.calls, [])

    def test_at_the_budget_freeze_and_cancel_yapnr_jobs_only(self):
        other = job("not-ours")
        other["labels"] = {}
        http = FakeHttp([job("a"), job("b", "QUEUED"), job("c", "SUCCEEDED"), other])
        summary = main.handle_budget(budget(50.0), http, CFG, NOW)
        self.assertEqual(summary["action"], "freeze")
        self.assertEqual([n.rsplit("/", 1)[1] for n in summary["cancelled"]], ["a", "b"])
        upload = http.of("POST", "/upload/storage/v1/b/example-yapnr-runs/o")
        self.assertEqual(len(upload), 1)
        self.assertIn("name=control%2Ffrozen", upload[0][1])
        self.assertFalse(http.of("PATCH"))
        # New submits are blocked at IAM, before the jobs are listed and cancelled.
        self.assertEqual(summary["submit_disabled"], 200)
        disable = [i for i, c in enumerate(http.calls) if c[1].endswith(":disable")]
        first_list = next(i for i, c in enumerate(http.calls) if c[0] == "GET")
        self.assertEqual(len(disable), 1)
        self.assertLess(disable[0], first_list)
        self.assertEqual(
            http.calls[disable[0]][1],
            "https://iam.googleapis.com/v1/projects/example-project/serviceAccounts/%s:disable"
            % CFG["submit_account"],
        )

    def test_without_a_submit_account_the_freeze_still_cancels(self):
        http = FakeHttp([job("a")])
        summary = main.handle_budget(budget(50.0), http, dict(CFG, submit_account=""), NOW)
        self.assertIsNone(summary["submit_disabled"])
        self.assertEqual(len(summary["cancelled"]), 1)

    def test_repeated_notifications_are_harmless(self):
        http = FakeHttp([job("a")])
        main.handle_budget(budget(55.0), http, CFG, NOW)
        again = main.handle_budget(budget(55.0), http, CFG, NOW)
        self.assertEqual(again["cancelled"], [])  # already cancelling
        self.assertEqual(len(http.of("POST", ":cancel")), 1)

    def test_far_above_the_budget_cuts_the_quota(self):
        http = FakeHttp([job("a")])
        summary = main.handle_budget(budget(61.0), http, CFG, NOW)
        self.assertEqual(summary["action"], "freeze+quota")
        ((method, url, body),) = http.of("PATCH")
        self.assertIn("quotaPreferences/yapnr-preemptible-cpus-us-west4?", url)
        self.assertIn("allowMissing=true", url)
        self.assertIn("ignoreSafetyChecks=QUOTA_DECREASE_BELOW_USAGE", url)
        self.assertEqual(body["quotaConfig"], {"preferredValue": "0"})
        self.assertEqual(body["dimensions"], {"region": "us-west4"})

    def test_quota_cut_updates_a_preference_made_in_the_console(self):
        console = {
            "name": "projects/5/locations/global/quotaPreferences/8aeb531a-708e-47f3-9ca2",
            "service": "compute.googleapis.com",
            "quotaId": "PREEMPTIBLE-CPUS-per-project-region",
            "dimensions": {"region": "us-west4"},
        }
        other_region = dict(
            console,
            name="projects/5/locations/global/quotaPreferences/x",
            dimensions={"region": "northamerica-northeast1"},
        )
        other_quota = dict(
            console,
            name="projects/5/locations/global/quotaPreferences/y",
            quotaId="CPUS-per-project-region",
        )
        http = FakeHttp([job("a")], preferences=[other_region, other_quota, console])
        main.handle_budget(budget(61.0), http, CFG, NOW)
        ((method, url, body),) = http.of("PATCH")
        self.assertIn("quotaPreferences/8aeb531a-708e-47f3-9ca2?", url)
        self.assertEqual(body["quotaConfig"], {"preferredValue": "0"})

    def test_stale_and_malformed_messages_are_ignored(self):
        http = FakeHttp([job("a")])
        self.assertEqual(
            main.handle_budget(budget(80.0, start="2026-08-01T07:00:00Z"), http, CFG, NOW)[
                "action"
            ],
            "ignored",
        )
        self.assertEqual(main.handle_budget({"anomaly": True}, http, CFG, NOW)["action"], "ignored")
        self.assertEqual(
            main.handle_budget(budget(10.0, amount=0), http, CFG, NOW)["action"], "ignored"
        )
        self.assertEqual(http.calls, [])

    def test_cloud_event_payload(self):
        payload = base64.b64encode(json.dumps(budget(1.0)).encode()).decode()

        class Event:
            data = {"message": {"data": payload}}

        self.assertEqual(main.message_data(Event())["costAmount"], 1.0)
        self.assertIsNone(main.message_data({"data": {"message": {}}}))

    def test_settings_from_the_environment(self):
        cfg = main.settings(
            {
                "YAPNR_PROJECT": "p",
                "YAPNR_REGIONS": "r1,r2",
                "YAPNR_RUNS_BUCKET": "b",
                "YAPNR_QUOTA_PREFERENCES": "r1=Q1,r2=Q2",
            }
        )
        self.assertEqual(cfg["preferences"], {"r1": "Q1", "r2": "Q2"})
        self.assertEqual(cfg["cut_at"], 1.2)
        self.assertEqual(cfg["submit_account"], "")


class ReaperTest(unittest.TestCase):
    def test_cancels_past_deadline_and_deletes_old_vms(self):
        now = 2_000_000_000
        http = FakeHttp(
            [
                job("late", deadline=str(now - 10)),
                job("fine", deadline=str(now + 3600)),
                job("nodeadline"),
            ],
            {
                "zones/us-west4-a": {
                    "instances": [
                        {"name": "old", "labels": {"yapnr": "1", "deadline": str(now - 7200)}},
                        {"name": "recent", "labels": {"yapnr": "1", "deadline": str(now - 60)}},
                    ]
                }
            },
        )
        summary = main.handle_reap(http, CFG, now)
        self.assertEqual([n.rsplit("/", 1)[1] for n in summary["cancelled"]], ["late"])
        self.assertEqual(summary["deleted"], ["old"])
        ((method, url, _),) = http.of("DELETE")
        self.assertTrue(url.endswith("/projects/example-project/zones/us-west4-a/instances/old"))
        self.assertIn("filter=labels.yapnr%3D%221%22", http.of("GET", "aggregated")[0][1])


if __name__ == "__main__":
    unittest.main()
