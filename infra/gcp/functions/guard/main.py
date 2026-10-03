"""Budget kill switch and reaper for yapnr experiments (Cloud Run functions, Python, stdlib only).

``budget_guard`` receives every budget notification (Pub/Sub, several times a day, at least once,
possibly out of order) and acts on the spend ratio ``costAmount / budgetAmount`` of the current
budget period, so a repeated or late message never does harm:

- ratio >= 1.0: disable the ``yapnr-submit`` service account (``YAPNR_SUBMIT_ACCOUNT``), so no
  new job can be submitted through it whatever the client does; write ``control/frozen`` in the
  runs bucket (``yapnr exp submit`` and every task check it); and cancel every queued, scheduled
  or running Batch job labelled ``yapnr=1`` in the enabled regions. Only the owner re-enables the
  account (docs/cloud-experiments.md, "Kill-switch drill");
- ratio >= ``YAPNR_QUOTA_CUT_AT`` (1.2 by default): also set the preemptible CPU quota preferences
  in ``YAPNR_QUOTA_PREFERENCES`` to 0, which stops Spot VMs from being created at all.

``reaper`` runs every 15 minutes: it cancels yapnr jobs whose ``deadline`` label (Unix seconds) has
passed, and deletes VMs labelled ``yapnr=1`` whose deadline passed more than an hour ago.

Configuration comes from environment variables set by infra/gcp/modules/guard. The functions call
the REST APIs with the function's own service account token from the metadata server.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # the runtime provides the Functions Framework; the unit tests do not need it
    import functions_framework

    cloud_event = functions_framework.cloud_event
except ImportError:  # pragma: no cover

    def cloud_event(func):
        return func


TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
BATCH = "https://batch.googleapis.com/v1"
STORAGE_UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
QUOTAS = "https://cloudquotas.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
IAM = "https://iam.googleapis.com/v1"
ACTIVE = ("QUEUED", "SCHEDULED", "RUNNING")
VM_GRACE_S = 3600
FROZEN = "control/frozen"


class Http:
    """JSON over HTTPS with a metadata-server token (replaced by a fake in the tests)."""

    def __init__(self):
        self._token = None
        self._expires = 0.0

    def token(self):
        if self._token is None or time.time() > self._expires - 60:
            request = urllib.request.Request(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode())
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 300))
        return self._token

    def __call__(self, method, url, body=None, content_type="application/json"):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self.token())
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
                return response.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as err:
            return err.code, {"error": err.read().decode(errors="replace")[:500]}


def settings(environ=None):
    env = os.environ if environ is None else environ
    preferences = {}
    for item in filter(None, env.get("YAPNR_QUOTA_PREFERENCES", "").split(",")):
        region, _, quota = item.partition("=")
        preferences[region.strip()] = quota.strip()
    return {
        "project": env["YAPNR_PROJECT"],
        "regions": [r for r in env.get("YAPNR_REGIONS", "").split(",") if r],
        "bucket": env["YAPNR_RUNS_BUCKET"],
        "preferences": preferences,
        "cut_at": float(env.get("YAPNR_QUOTA_CUT_AT", "1.2")),
        "submit_account": env.get("YAPNR_SUBMIT_ACCOUNT", ""),
    }


def message_data(event):
    """The JSON payload of a Pub/Sub CloudEvent (``event.data`` or a plain dict)."""
    data = getattr(event, "data", None)
    if data is None and isinstance(event, dict):
        data = event.get("data", event)
    try:
        raw = base64.b64decode(data["message"]["data"])
        return json.loads(raw.decode())
    except (KeyError, TypeError, ValueError):
        return None


def spend_ratio(budget, now=None):
    """costAmount / budgetAmount of the current period, or None for stale or other messages."""
    try:
        cost, amount = float(budget["costAmount"]), float(budget["budgetAmount"])
    except (KeyError, TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    start = budget.get("costIntervalStart")
    if start:
        now = now or _dt.datetime.now(_dt.timezone.utc)
        begun = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        # A monthly budget: a message from an earlier month arrived late; ignore it.
        if (begun.year, begun.month) < (now.year, now.month) and (now - begun).days > 31:
            return None
    return cost / amount


def yapnr_jobs(http, cfg):
    for region in cfg["regions"]:
        token = ""
        while True:
            query = {"pageSize": "100"}
            if token:
                query["pageToken"] = token
            url = "%s/projects/%s/locations/%s/jobs?%s" % (
                BATCH,
                cfg["project"],
                region,
                urllib.parse.urlencode(query),
            )
            status, page = http("GET", url)
            if status != 200:
                print(json.dumps({"guard": "list-failed", "region": region, "status": status}))
                break
            for job in page.get("jobs", []):
                if (job.get("labels") or {}).get("yapnr") == "1":
                    yield job
            token = page.get("nextPageToken")
            if not token:
                break


def cancel(http, job):
    status, _ = http("POST", "%s/%s:cancel" % (BATCH, job["name"]), {})
    return status


def freeze(http, cfg, reason):
    url = "%s/b/%s/o?%s" % (
        STORAGE_UPLOAD,
        cfg["bucket"],
        urllib.parse.urlencode({"uploadType": "media", "name": FROZEN}),
    )
    status, _ = http("POST", url, reason.encode(), "text/plain")
    return status


def disable_submit(http, cfg):
    """Disable the account every submit runs as; None when none is configured."""
    email = cfg.get("submit_account")
    if not email:
        return None
    url = "%s/projects/%s/serviceAccounts/%s:disable" % (IAM, cfg["project"], email)
    status, _ = http("POST", url, {})
    return status


def cut_quota(http, cfg):
    statuses = {}
    for region, quota in sorted(cfg["preferences"].items()):
        name = "yapnr-preemptible-cpus-%s" % region
        query = urllib.parse.urlencode(
            [
                ("allowMissing", "true"),
                ("ignoreSafetyChecks", "QUOTA_DECREASE_BELOW_USAGE"),
                ("ignoreSafetyChecks", "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH"),
            ]
        )
        url = "%s/projects/%s/locations/global/quotaPreferences/%s?%s" % (
            QUOTAS,
            cfg["project"],
            name,
            query,
        )
        body = {
            "service": "compute.googleapis.com",
            "quotaId": quota,
            "dimensions": {"region": region},
            "quotaConfig": {"preferredValue": "0"},
            "justification": "yapnr budget guard: spend above the cut threshold",
        }
        statuses[region], _ = http("PATCH", url, body)
    return statuses


def handle_budget(budget, http, cfg, now=None):
    """What one notification does; returns a summary (logged, and asserted by the tests)."""
    ratio = spend_ratio(budget, now)
    if ratio is None:
        return {"action": "ignored"}
    if ratio < 1.0:
        return {"action": "none", "ratio": round(ratio, 3)}
    reason = "budget %s at %.0f%% (%s of %s %s)\n" % (
        budget.get("budgetDisplayName", "?"),
        ratio * 100,
        budget.get("costAmount"),
        budget.get("budgetAmount"),
        budget.get("currencyCode", ""),
    )
    # Block new submits first, so nothing started after the job listing below survives it.
    summary = {"action": "freeze", "ratio": round(ratio, 3)}
    summary["submit_disabled"] = disable_submit(http, cfg)
    summary["frozen"] = freeze(http, cfg, reason)
    summary["cancelled"] = [
        job["name"]
        for job in yapnr_jobs(http, cfg)
        if (job.get("status") or {}).get("state") in ACTIVE and cancel(http, job) < 300
    ]
    if ratio >= cfg["cut_at"]:
        summary["action"] = "freeze+quota"
        summary["quota"] = cut_quota(http, cfg)
    return summary


def handle_reap(http, cfg, now=None):
    now = time.time() if now is None else now
    summary = {"cancelled": [], "deleted": []}
    for job in yapnr_jobs(http, cfg):
        deadline = (job.get("labels") or {}).get("deadline", "")
        state = (job.get("status") or {}).get("state")
        if deadline.isdigit() and int(deadline) < now and state in ACTIVE:
            if cancel(http, job) < 300:
                summary["cancelled"].append(job["name"])
    url = "%s/projects/%s/aggregated/instances?%s" % (
        COMPUTE,
        cfg["project"],
        urllib.parse.urlencode({"filter": 'labels.yapnr="1"'}),
    )
    status, page = http("GET", url)
    if status != 200:
        return summary
    for scope, items in (page.get("items") or {}).items():
        for vm in items.get("instances", []):
            deadline = (vm.get("labels") or {}).get("deadline", "")
            if deadline.isdigit() and int(deadline) + VM_GRACE_S < now:
                zone = scope.split("/", 1)[-1]
                url = "%s/projects/%s/zones/%s/instances/%s" % (
                    COMPUTE,
                    cfg["project"],
                    zone,
                    vm["name"],
                )
                if http("DELETE", url)[0] < 300:
                    summary["deleted"].append(vm["name"])
    return summary


_HTTP = None


def _http():
    global _HTTP
    if _HTTP is None:
        _HTTP = Http()
    return _HTTP


@cloud_event
def budget_guard(event):
    budget = message_data(event)
    summary = handle_budget(budget or {}, _http(), settings())
    print(json.dumps({"guard": "budget", **summary}, sort_keys=True))


@cloud_event
def reaper(event):
    summary = handle_reap(_http(), settings())
    print(json.dumps({"guard": "reaper", **summary}, sort_keys=True))
