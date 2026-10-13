"""JSON client and CLI for a running placement laboratory (stdlib only)."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "yapnr-placement-api-v1"
ACTIVE = {"running", "paused", "legalizing"}


class PlacementError(ValueError):
    """A rejected request, transport failure or bounded wait expiry."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlacementError("Placement endpoint redirected; configure its final URL explicitly")


class PlacementClient:
    """Drive the same server as the browser; no direct workspace mutation or solver imports."""

    def __init__(self, url=None, timeout=30):
        url = url or os.environ.get("YAPNR_PLACEMENT_URL", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise PlacementError("Set --url or YAPNR_PLACEMENT_URL to the viewer origin")
        if not 0 < timeout <= 60:
            raise PlacementError("Request timeout must be greater than zero and at most 60 seconds")
        self.url, self.timeout = url.rstrip("/"), timeout

    def call(self, action, **parameters):
        request = Request(
            self.url + "/api/placement-lab/v1",
            data=json.dumps(dict(action=action, **parameters), allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "Origin": self.url},
        )
        try:
            with build_opener(_NoRedirect).open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", str(exc))
            except (ValueError, AttributeError):
                message = str(exc)
            raise PlacementError(f"HTTP {exc.code}: {message}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PlacementError(f"Placement connection failed: {exc}") from exc
        if result.get("schema") != VERSION:
            raise PlacementError("Unsupported placement API response")
        return result["result"]

    def catalog(self):
        return self.call("catalog")

    def start(self, source, **settings):
        return self.call("start", source=source, **settings)

    def status(self, run):
        return self.call("status", id=run)

    def decision(self, run, *, step=None, ref=None):
        return self.call("decision", id=run, **({"ref": ref} if ref else {"step": step}))

    def control(self, run, action, **parameters):
        if action not in {"pause", "resume", "step", "apply", "annealing", "stop"}:
            raise PlacementError("Choose pause, resume, step, apply, annealing or stop")
        return self.call(action, id=run, **parameters)

    def wait(self, run, *, seconds=20, state=None, revision=None):
        """Bounded poll; timeout does NOT stop the solver. Inspect returned qualification."""
        if not 0 < seconds <= 60:
            raise PlacementError("Wait must be greater than zero and at most 60 seconds")
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise PlacementError("Wait expired; the run has not been stopped")
            result = PlacementClient(self.url, min(self.timeout, remaining)).status(run)
            status = result["status"]
            terminal = status["state"] not in ACTIVE
            if (state is None and revision is None and terminal) or (
                (state is not None or revision is not None)
                and (state is None or status["state"] == state)
                and (revision is None or status.get("control_revision", -1) >= revision)
            ):
                return result
            if terminal:
                raise PlacementError(f"Run ended {status['state']} before the requested checkpoint")
            time.sleep(min(0.2, max(0, end - time.monotonic())))

    def link(self, run, *, step=None, compare=None):
        q = {"run": run}
        if step is not None:
            q["step"] = step
        if compare:
            q["compare"] = compare
        return self.url + "/placement-lab.html?" + urlencode(q)


def register(commands):
    parser = commands.add_parser("placement", help="drive the live placement lab (JSON API)")
    parser.add_argument("--url", help="viewer origin; defaults to YAPNR_PLACEMENT_URL")
    parser.add_argument("--timeout", type=float, default=30, help="per-request seconds (max 60)")
    sub = parser.add_subparsers(dest="placement_action", required=True)
    for name in ("schema", "catalog"):
        sub.add_parser(name)
    source = sub.add_parser("source")
    source.add_argument("source")
    add = sub.add_parser("register-source")
    add.add_argument(
        "file", help="JSON graph and compiled constraints; content-addressed on server"
    )
    objective = sub.add_parser("validate-objective")
    objective.add_argument("expression")
    start = sub.add_parser("start")
    start.add_argument("source")
    start.add_argument("--seed", type=int, default=0)
    start.add_argument("--iterations", type=int, default=400)
    start.add_argument(
        "--run-forever",
        action="store_true",
        help="no iteration or wall-time limit; --iterations sets the annealing horizon",
    )
    start.add_argument(
        "--shove",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="transactional placement shove",
    )
    start.add_argument(
        "--placement-banks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="authored capacitor banks",
    )
    start.add_argument(
        "--legalize-only", action="store_true", help="replay legalization of the source poses"
    )
    start.add_argument("--frame-delay-ms", type=int, default=30)
    start.add_argument("--expression")
    start.add_argument("--annealing", type=json.loads, help="JSON hold or schedule settings")
    start.add_argument("--paused", action="store_true", help="pause before iteration zero")
    for name in (
        "run",
        "status",
        "decisions",
        "decision",
        "render",
        "history",
        "evidence",
        "notes",
        "note",
        "reply",
        "pause",
        "resume",
        "step",
        "apply",
        "annealing",
        "stop",
        "wait",
        "link",
        "compare",
        "surface",
        "surface-result",
        "surface-render",
    ):
        p = sub.add_parser(name)
        p.add_argument("id", help="run id")
        if name in ("decision", "note", "render"):
            selector = p.add_mutually_exclusive_group(required=name != "render")
            selector.add_argument("--step", type=int, help="zero-based decision index")
            selector.add_argument("--ref", help="component reference")
        if name in ("note", "render"):
            p.add_argument("--alternative", type=int, help="zero-based evaluated alternative")
        if name == "note":
            p.add_argument("--body", required=True)
        if name == "reply":
            p.add_argument("note")
            p.add_argument("--body", required=True)
        if name == "notes":
            p.add_argument("--ref")
        if name in ("pause", "resume", "step", "apply", "annealing", "stop"):
            p.add_argument("--expect-revision", type=int, help="reject stale concurrent edits")
        if name == "annealing":
            p.add_argument("annealing", type=json.loads, help="JSON hold or schedule settings")
        if name == "step":
            p.add_argument("--count", type=int, default=1)
        if name == "surface":
            p.add_argument("--ref", required=True)
            p.add_argument("--expression", default="objective")
            p.add_argument("--pitch", type=float, default=1.5)
        if name in ("surface-result", "surface-render"):
            p.add_argument("key")
        if name == "surface-render":
            p.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270])
        if name == "apply":
            p.add_argument("expression")
        if name == "history":
            p.add_argument(
                "--cursor", type=int, default=0, help="byte cursor returned by last page"
            )
            p.add_argument("--limit", type=int, default=25)
        if name == "evidence":
            p.add_argument("name", help="one of the names returned in status.evidence")
        if name == "compare":
            p.add_argument("other", help="baseline run id; deltas are this run minus baseline")
        if name == "link":
            p.add_argument("--step", type=int)
            p.add_argument("--compare")
        if name == "wait":
            p.add_argument("--seconds", type=float, default=20)
            p.add_argument(
                "--state",
                choices=sorted(
                    ACTIVE | {"complete", "failed", "stopped", "timed_out", "interrupted"}
                ),
            )
            p.add_argument("--revision", type=int)
    parser.set_defaults(func=_run)


def _run(args):
    try:
        client = PlacementClient(args.url, args.timeout)
        action = args.placement_action
        fields = {
            k: v
            for k, v in vars(args).items()
            if k
            in {
                "id",
                "count",
                "pitch",
                "rotation",
                "key",
                "source",
                "seed",
                "iterations",
                "run_forever",
                "shove",
                "placement_banks",
                "legalize_only",
                "annealing",
                "frame_delay_ms",
                "expression",
                "paused",
                "step",
                "ref",
                "body",
                "alternative",
                "note",
                "expect_revision",
                "cursor",
                "limit",
                "name",
                "other",
            }
            and v is not None
        }
        if action == "register-source":
            fields = {"document": json.loads(Path(args.file).read_text())}
        if action == "wait":
            result = client.wait(
                args.id, seconds=args.seconds, state=args.state, revision=args.revision
            )
        elif action == "link":
            result = {"url": client.link(args.id, step=args.step, compare=args.compare)}
        else:
            result = client.call(action, **fields)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
