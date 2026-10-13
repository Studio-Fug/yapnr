"""Parent-owned, executable feedback resolution with independently verified closure.

This is an evidence protocol for trusted workspace processes, not a sandbox against
an operator who can rewrite the workspace or replace git/gh/interpreters.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

LOCK = threading.RLock()
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
HEAD = re.compile(r"[a-f0-9]{40,64}\Z")
PR = re.compile(r"https://github.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)\Z")


class Conflict(ValueError):
    """An optimistic revision or active execution changed."""


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe(root, name, *, exists=True):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Expected a project-relative path")
    path = Path(name)
    if any(part in ("..", ".git") for part in path.parts):
        raise ValueError("Unsafe path")
    current = Path(root)
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not evidence")
    if not current.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Path escaped project")
    if exists and not current.is_file():
        raise ValueError("Evidence file is unavailable: " + name)
    return current


def command(argv, cwd=None, timeout=60):
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise ValueError("Command failed: " + argv[0])
    return result.stdout.strip()


def picture(raw, suffix):
    if suffix == ".svg":
        return b"<svg" in raw[:2000] and b"</svg>" in raw
    if suffix == ".png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    return suffix in (".jpg", ".jpeg") and raw.startswith(b"\xff\xd8")


class Feedback:
    def __init__(self, project):
        self.root = Path(project).resolve()
        self.folder = self.root / ".yapnr" / "feedback"
        # Never follow a substituted store, including an intermediate directory.
        safe(self.root, ".yapnr/feedback", exists=False)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log = safe(self.root, ".yapnr/feedback/events.jsonl", exists=False)

    @contextlib.contextmanager
    def locked(self):
        with LOCK, safe(self.folder, "store.lock", exists=False).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def events(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n")
        ]

    def replay(self):
        return {event["item"]["id"]: event["item"] for event in self.events()}

    def blob(self, raw):
        sha = digest(raw)
        folder = safe(self.folder, "objects", exists=False)
        folder.mkdir(exist_ok=True)
        path = safe(folder, sha, exists=False)
        if path.exists() and path.read_bytes() != raw:
            raise ValueError("Immutable evidence object changed")
        if not path.exists():
            path.write_bytes(raw)
        return sha

    def object(self, sha):
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid evidence digest")
        raw = safe(self.folder, "objects/" + sha).read_bytes()
        if digest(raw) != sha:
            raise ValueError("Immutable evidence object changed")
        return raw

    def append(self, item, event, **details):
        item["revision"] = item.get("revision", 0) + 1
        record = dict(event=event, time=time.time(), item=item, **details)
        with self.log.open("a+b") as stream:
            stream.seek(0)
            prior = stream.read()
            if prior and not prior.endswith(b"\n"):
                cut = prior.rfind(b"\n") + 1
                self.blob(prior[cut:])  # Preserve crash residue before recovering the journal.
                stream.truncate(cut)
                stream.seek(0, 2)
            stream.write(encoded(record))
            stream.flush()
            os.fsync(stream.fileno())
        manifest = {
            "schema": "yapnr-feedback-v1",
            "journal": ".yapnr/feedback/events.jsonl",
            "artifacts": [
                {"sha256": p.name, "path": ".yapnr/feedback/objects/" + p.name}
                for p in sorted((self.folder / "objects").glob("*"))
            ],
        }
        safe(self.folder, "manifest.json", exists=False).write_bytes(encoded(manifest))
        # The normal workspace exporter indexes this entire project-relative directory.
        return item

    def get(self, identity, revision=None):
        if not isinstance(identity, str) or not ID.fullmatch(identity):
            raise ValueError("Invalid feedback ID")
        item = self.replay().get(identity)
        if item is None:
            raise ValueError("Unknown feedback item")
        if revision is not None and item["revision"] != revision:
            raise Conflict("Feedback changed; query and retry")
        return item

    def add(self, text, source=None, identity=None, revision=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("Expected actionable feedback text")
        source = source or {}
        raw = encoded(dict(text=text, source=source))
        identity = identity or "F" + uuid.uuid4().hex
        if not ID.fullmatch(identity):
            raise ValueError("Invalid feedback ID")
        with self.locked():
            item = self.replay().get(identity)
            if item:
                if item.get("source_sha256", item["feedback_sha256"]) == digest(raw):
                    return item  # Replaying a saved review is idempotent.
                if revision is None or item["revision"] != revision:
                    raise Conflict("Feedback changed; query and retry")
                item.update(
                    state="needs_plan",
                    plan=None,
                    submission=None,
                    verification=None,
                    clarifications=[],
                )
            else:
                item = dict(id=identity, revision=0, attempts=[], state="needs_plan", paused=False)
            item.update(
                text=text, source=source, source_sha256=digest(raw), feedback_sha256=self.blob(raw)
            )
            return self.append(item, "feedback")

    def _fresh(self, item):
        plan = item.get("plan")
        if not plan:
            return
        self.object(plan["sha256"])
        for entry in plan["files"].values():
            self.object(entry["sha256"])
            if digest(safe(self.root, entry["source"]).read_bytes()) != entry["sha256"]:
                raise ValueError("Pinned plan input changed: " + entry["source"])
        if plan["feedback_sha256"] != item["feedback_sha256"]:
            raise ValueError("Feedback changed after planning")

    def query(self, identity=None):
        with self.locked():
            items = [self.get(identity)] if identity else list(self.replay().values())
            for item in items:
                try:
                    self._fresh(item)
                    if item["state"] == "addressed":
                        self._pr(item["plan"], item["submission"])
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    if item["state"] != "stale":
                        item.update(state="stale", invalidation=str(error))
                        self.append(item, "invalidated")
                item["next_action"] = (
                    "resume"
                    if item.get("paused")
                    else {
                        "needs_plan": "parent: lock validator and failing worked example",
                        "stale": "parent: refresh inputs/PR and lock a new plan",
                        "clarification": "user: answer the recorded question",
                        "ready": "parent: dispatch or explicitly run worker",
                        "implementing": "worker: submit PR; parent: recover only if worker stopped",
                        "verifying": "parent: recover interrupted verification",
                        "submitted": "parent: independently verify exact PR head",
                        "retry": "parent: dispatch next attempt with failed criteria",
                        "exhausted": "parent: explicitly resume with a new budget",
                        "addressed": "none: independently verified",
                    }.get(item["state"], "query")
                )
            return items[0] if identity else {"items": items}

    def _materialize(self, plan, directory):
        for name, entry in plan["files"].items():
            target = safe(directory, name, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.object(entry["sha256"]))

    def _checks(self, plan, files, candidate_root, outputs, baseline=False):
        reports = []
        criteria = plan["definition"]["criteria"]
        for check in plan["definition"]["checks"]:
            expected = {c["id"] for c in criteria if c["check"] == check["id"]}
            result = {"check": check["id"], "cases": [], "passed": False}
            try:
                name = check["baseline"] if baseline else outputs[check["id"]]
                candidate = safe(files if baseline else candidate_root, name)
                replacements = {
                    "{validator}": str(safe(files, check["validator"])),
                    "{candidate}": str(candidate),
                    "{checkout}": str(candidate_root),
                }
                replacements.update(
                    {"{input:" + k + "}": str(safe(files, k)) for k in plan["files"]}
                )
                argv = [replacements.get(arg, arg) for arg in check["argv"]]
                run = subprocess.run(
                    argv,
                    cwd=candidate_root,
                    capture_output=True,
                    timeout=check["timeout_seconds"],
                    check=False,
                )
                result.update(
                    returncode=run.returncode,
                    stdout_sha256=self.blob(run.stdout),
                    stderr_sha256=self.blob(run.stderr),
                    candidate_sha256=digest(candidate.read_bytes()),
                )
                report = json.loads(run.stdout)
                cases = report["cases"]
                if (
                    not isinstance(cases, list)
                    or len(cases) != len(expected)
                    or {c["id"] for c in cases} != expected
                    or any(type(c.get("passed")) is not bool for c in cases)
                ):
                    raise ValueError("Missing, duplicate or malformed criterion results")
                result["cases"] = cases
                result["passed"] = run.returncode == 0 and all(c["passed"] for c in cases)
                if baseline:
                    wanted = {
                        c["id"]: c["baseline"] == "pass"
                        for c in criteria
                        if c["check"] == check["id"]
                    }
                    result["baseline_matches"] = (
                        run.returncode in (0, 1)
                        and all(c["passed"] == wanted[c["id"]] for c in cases)
                        and (run.returncode == 0) == all(c["passed"] for c in cases)
                    )
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                result["error"] = str(error)
            reports.append(result)
        return reports

    def plan(self, identity, revision, definition):
        """CLI-only: compiling a plan runs parent-selected baseline commands."""
        with self.locked():
            item = self.get(identity, revision)
            if item["state"] in ("implementing", "verifying"):
                raise Conflict("An attempt is active; pause and recover it first")
            if item["state"] == "clarification":
                raise ValueError("Answer the recorded clarification before planning")
            checks, criteria = definition["checks"], definition["criteria"]
            check_ids = {c["id"] for c in checks}
            if (
                not checks
                or len(check_ids) != len(checks)
                or not criteria
                or len({c["id"] for c in criteria}) != len(criteria)
                or not any(c["baseline"] == "fail" for c in criteria)
                or any(
                    c["check"] not in check_ids or c["baseline"] not in ("pass", "fail")
                    for c in criteria
                )
                or any(not any(c["check"] == k for c in criteria) for k in check_ids)
            ):
                raise ValueError("Declare every unique criterion and a failing worked baseline")
            if (
                not isinstance(definition.get("settings"), dict)
                or "seed" not in definition["settings"]
            ):
                raise ValueError("Record reproducibility settings including seed")
            files = {}
            for name in definition["files"]:
                raw = safe(self.root, name).read_bytes()
                files[name] = dict(source=name, sha256=self.blob(raw))
            for check in checks:
                if (
                    check["validator"] not in files
                    or check["baseline"] not in files
                    or not isinstance(check["argv"], list)
                    or not check["argv"]
                    or "{validator}" not in check["argv"]
                    or "{candidate}" not in check["argv"]
                    or not 0 < check["timeout_seconds"] <= 3600
                ):
                    raise ValueError(
                        "Each check requires pinned validator/baseline and bounded argv"
                    )
                if any(not isinstance(arg, str) for arg in check["argv"]):
                    raise ValueError("Command argv must contain strings")
            visual = definition["before_visual"]
            if visual not in files or not picture(
                self.object(files[visual]["sha256"]), Path(visual).suffix
            ):
                raise ValueError("Pin an actual worked baseline visual")
            repository = definition["repository"]
            if not isinstance(repository, str) or not repository or repository.startswith("-"):
                raise ValueError("Expected trusted repository URL")
            plan = dict(
                definition=definition,
                files=files,
                feedback_sha256=item["feedback_sha256"],
                sha256=self.blob(encoded(definition)),
            )
            with tempfile.TemporaryDirectory(prefix="yapnr-feedback-plan-") as folder:
                self._materialize(plan, Path(folder))
                baseline = self._checks(plan, Path(folder), Path(folder), {}, baseline=True)
            if not all(r.get("baseline_matches") for r in baseline):
                self.append(item, "plan_rejected", baseline=baseline)
                raise ValueError(
                    "Worked baseline did not demonstrate exactly the declared outcomes"
                )
            plan["baseline"] = baseline
            item.update(
                plan=plan, state="ready", submission=None, verification=None, invalidation=None
            )
            return self.append(item, "plan_locked")

    def control(self, identity, revision, action, text=""):
        with self.locked():
            item = self.get(identity, revision)
            if action == "pause":
                item["paused"] = True
            elif action == "resume":
                if item["state"] == "clarification":
                    raise ValueError("Answer the outstanding question first")
                item["paused"] = False
                if item["state"] == "exhausted":
                    item["state"] = "retry"
            elif action == "clarify":
                if not text.strip():
                    raise ValueError("Record an actual clarifying question")
                item.update(state="clarification", question=text)
            elif action == "answer":
                if item["state"] != "clarification" or not text.strip():
                    raise ValueError("An actual answer to the pending question is required")
                item.setdefault("clarifications", []).append(
                    {"question": item["question"], "answer": text}
                )
                item.update(answer=text, state="needs_plan", plan=None, verification=None)
                item["feedback_sha256"] = self.blob(encoded([item["feedback_sha256"], text]))
            elif action == "recover":
                if not item.get("paused") or item["state"] not in ("implementing", "verifying"):
                    raise ValueError("Pause an interrupted attempt before explicitly recovering")
                with safe(self.folder, identity + ".runner.lock", exists=False).open("a") as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise Conflict("The local worker controller is still running") from error
                item.update(state="retry", submission=None, verification=None)
            else:
                raise ValueError("Unknown feedback control")
            return self.append(item, action, text=text)

    def dispatch(self, identity, revision, worker):
        with self.locked():
            item = self.get(identity, revision)
            self._fresh(item)
            if item.get("paused") or item["state"] not in ("ready", "retry"):
                raise Conflict("Feedback is not ready for a new worker")
            if not isinstance(worker, str) or not worker.strip():
                raise ValueError("Record worker identity")
            attempt = dict(
                id=uuid.uuid4().hex,
                worker=worker,
                plan_sha256=item["plan"]["sha256"],
                feedback_sha256=item["feedback_sha256"],
            )
            bundle = dict(
                schema="yapnr-feedback-task-v1",
                feedback=item["text"],
                source=item["source"],
                clarifications=item.get("clarifications", []),
                attempt=attempt,
                plan=item["plan"],
                previous_failures=item.get("verification"),
                instructions=(
                    "Implement every criterion. Return a real PR and committed "
                    "before/after visuals. Do not edit the parent validator. "
                    "Worker reports never close feedback."
                ),
            )
            attempt["bundle_sha256"] = self.blob(encoded(bundle))
            item["attempts"].append(attempt)
            item.update(state="implementing", submission=None)
            return self.append(item, "dispatch")

    def bundle(self, item, destination):
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=False)
        task = self.object(item["attempts"][-1]["bundle_sha256"])
        (target / "task.json").write_bytes(task)
        (target / "validator").mkdir()
        self._materialize(item["plan"], target / "validator")
        (target / "TASK.md").write_text(
            "# Delegated feedback implementation\n\nRead task.json and the pinned validator files. "
            "Implement all criteria in a separate checkout, create a PR with committed before/after "
            "visuals in its description, and write submission.json using the documented receipt "
            "schema. Include this attempt ID. The parent independently verifies the exact PR head.\n"
        )
        return target

    def submit(self, identity, revision, receipt):
        with self.locked():
            item = self.get(identity, revision)
            self._fresh(item)
            if item["state"] != "implementing":
                raise Conflict("No active worker accepts this submission")
            required = {"attempt", "pr", "head", "outputs", "before_visual", "after_visual"}
            if set(receipt) != required or receipt["attempt"] != item["attempts"][-1]["id"]:
                raise ValueError("Expected exact submission schema and active attempt")
            if not PR.fullmatch(receipt["pr"]) or not HEAD.fullmatch(receipt["head"]):
                raise ValueError("Expected a real GitHub PR URL and immutable head")
            if set(receipt["outputs"]) != {c["id"] for c in item["plan"]["definition"]["checks"]}:
                raise ValueError("Provide a candidate for every check")
            for name in [
                *receipt["outputs"].values(),
                receipt["before_visual"],
                receipt["after_visual"],
            ]:
                safe(self.root, name, exists=False)
            item.update(submission=receipt, state="submitted", verification=None)
            return self.append(item, "submitted", receipt_sha256=self.blob(encoded(receipt)))

    def _pr(self, plan, receipt):
        match = PR.fullmatch(receipt["pr"])
        metadata = json.loads(
            command(
                ["gh", "pr", "view", receipt["pr"], "--json", "headRefOid,body,url,state"],
                timeout=30,
            )
        )
        if metadata.get("url") != receipt["pr"] or metadata.get("headRefOid") != receipt["head"]:
            raise ValueError("PR current head changed or is unavailable")
        if metadata.get("state") not in ("OPEN", "MERGED"):
            raise ValueError("PR is closed without merge")
        repository = plan["definition"]["repository"]
        if "github.com" in repository:
            repo_name = (
                repository.split("github.com", 1)[1].lstrip(":/").removesuffix(".git").rstrip("/")
            )
            if match[1].lower() != repo_name.lower():
                raise ValueError("PR belongs to a different repository")
        ref = "refs/pull/" + match[2] + "/head"
        remote = command(["git", "ls-remote", "--", repository, ref], timeout=30)
        if not remote or remote.split()[0] != receipt["head"]:
            raise ValueError("PR git ref no longer matches submitted head")
        targets = re.findall(r"!?\[[^\]]*\]\(([^\s)]+)\)", metadata.get("body", ""))
        for key in ("before_visual", "after_visual"):
            expected = "/" + match[1] + "/" + receipt["head"] + "/" + receipt[key]
            linked = False
            for target in targets:
                url = urlparse(target.strip("<>"))
                path = unquote(url.path)
                linked |= (
                    url.scheme == "https"
                    and not url.query
                    and not url.fragment
                    and (
                        (url.netloc == "raw.githubusercontent.com" and path == expected)
                        or (
                            url.netloc == "github.com"
                            and path
                            == expected.replace(
                                "/" + receipt["head"] + "/", "/blob/" + receipt["head"] + "/", 1
                            )
                        )
                    )
                )
            if not linked:
                raise ValueError(
                    "PR description must link actual visuals at the exact submitted commit"
                )
        return ref

    def verify(self, identity, revision):
        with self.locked():
            item = self.get(identity, revision)
            self._fresh(item)
            if item.get("paused") or item["state"] != "submitted":
                raise Conflict("No submitted attempt is ready for independent verification")
            item["state"] = "verifying"
            self.append(item, "verification_started")
            claimed_revision = item["revision"]
        report = dict(
            passed=False,
            plan_sha256=item["plan"]["sha256"],
            feedback_sha256=item["feedback_sha256"],
            head=item["submission"]["head"],
            checks=[],
        )
        try:
            receipt, plan = item["submission"], item["plan"]
            ref = self._pr(plan, receipt)
            with tempfile.TemporaryDirectory(prefix="yapnr-feedback-verify-") as folder:
                checkout, files = Path(folder) / "checkout", Path(folder) / "validator"
                files.mkdir()
                self._materialize(plan, files)
                command(
                    [
                        "git",
                        "clone",
                        "--no-checkout",
                        "--",
                        plan["definition"]["repository"],
                        str(checkout),
                    ],
                    timeout=180,
                )
                command(["git", "-C", str(checkout), "fetch", "origin", ref], timeout=180)
                command(["git", "-C", str(checkout), "checkout", "--detach", receipt["head"]])

                def clean():
                    if command(["git", "-C", str(checkout), "rev-parse", "HEAD"]) != receipt[
                        "head"
                    ] or command(
                        [
                            "git",
                            "-C",
                            str(checkout),
                            "status",
                            "--porcelain",
                            "--untracked-files=all",
                        ]
                    ):
                        raise ValueError("Verification checkout is dirty or HEAD changed")

                clean()
                for name in [
                    *receipt["outputs"].values(),
                    receipt["before_visual"],
                    receipt["after_visual"],
                ]:
                    safe(checkout, name)
                    command(["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", name])
                for key in ("before_visual", "after_visual"):
                    path = safe(checkout, receipt[key])
                    raw = path.read_bytes()
                    if not picture(raw, path.suffix):
                        raise ValueError("Actual before/after visual is missing")
                    report[key + "_sha256"] = self.blob(raw)
                if (
                    report["before_visual_sha256"]
                    != plan["files"][plan["definition"]["before_visual"]]["sha256"]
                ):
                    raise ValueError("Before visual does not match parent worked example")
                if report["before_visual_sha256"] == report["after_visual_sha256"]:
                    raise ValueError("After visual must show the changed worked example")
                report["checks"] = self._checks(plan, files, checkout, receipt["outputs"])
                clean()
                for name, entry in plan["files"].items():
                    if digest(safe(files, name).read_bytes()) != entry["sha256"]:
                        raise ValueError("Pinned validator/input changed during verification")
                report["passed"] = bool(report["checks"]) and all(
                    c["passed"] for c in report["checks"]
                )
            self._pr(plan, receipt)  # Detect head races during a lengthy validator run.
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            report.update(passed=False, error=str(error))
        with self.locked():
            latest = self.get(identity)
            if latest["revision"] != claimed_revision or latest.get("paused"):
                # Preserve the result but never close a superseded/paused attempt.
                if (
                    latest["state"] == "verifying"
                    and latest.get("submission") == item["submission"]
                ):
                    latest["state"] = "submitted"  # Resume re-verifies; it never reruns the worker.
                self.append(
                    latest, "verification_superseded", report_sha256=self.blob(encoded(report))
                )
                return latest
            self._fresh(latest)
            latest["attempts"][-1]["verification"] = report
            latest.update(verification=report, state="addressed" if report["passed"] else "retry")
            return self.append(
                latest,
                "verified" if report["passed"] else "verification_failed",
                report_sha256=self.blob(encoded(report)),
            )

    def run_loop(self, identity, argv, *, worker="command", max_attempts=3, timeout=1800):
        """Explicit CLI authorization launches bounded workers; recording notes never does."""
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("Worker command must be a JSON argv array")
        if not 1 <= max_attempts <= 100 or not 0 < timeout <= 86400:
            raise ValueError("Declare a bounded attempt budget and worker timeout")
        lease = safe(self.folder, identity + ".runner.lock", exists=False)
        with lease.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Conflict("A feedback controller is already running") from error
            for _ in range(max_attempts):
                item = self.query(identity)
                if item.get("paused") or item["state"] in (
                    "addressed",
                    "clarification",
                    "needs_plan",
                    "stale",
                    "exhausted",
                    "implementing",
                    "verifying",
                ):
                    return item
                if item["state"] == "submitted":
                    item = self.verify(identity, item["revision"])
                    if item["state"] != "retry":
                        return item
                    continue
                item = self.dispatch(identity, item["revision"], worker)
                with tempfile.TemporaryDirectory(prefix="yapnr-feedback-worker-") as folder:
                    bundle = self.bundle(item, Path(folder) / "task")
                    attempt = item["attempts"][-1]["id"]
                    try:
                        # Worker receives only the copied task; no mutable parent store path.
                        result = subprocess.run(
                            [arg.replace("{bundle}", str(bundle)) for arg in argv],
                            cwd=bundle,
                            capture_output=True,
                            timeout=timeout,
                            check=False,
                        )
                        with self.locked():
                            latest = self.get(identity)
                            superseded = (
                                not latest["attempts"]
                                or latest["attempts"][-1]["id"] != attempt
                                or (latest.get("plan") or {}).get("sha256")
                                != item["plan"]["sha256"]
                                or latest["feedback_sha256"] != item["feedback_sha256"]
                            )
                            self.append(
                                latest,
                                "worker_superseded" if superseded else "worker_returned",
                                attempt=attempt,
                                stdout_sha256=self.blob(result.stdout),
                                stderr_sha256=self.blob(result.stderr),
                                returncode=result.returncode,
                            )
                            if superseded:
                                return latest
                        if result.returncode:
                            raise ValueError("Worker exited unsuccessfully")
                        receipt = json.loads(safe(bundle, "submission.json").read_text())
                        if latest["state"] != "implementing":
                            return latest
                        submitted = self.submit(identity, latest["revision"], receipt)
                        if submitted.get("paused"):
                            return submitted
                        item = self.verify(identity, submitted["revision"])
                        if item["state"] != "retry":
                            return item
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        subprocess.SubprocessError,
                    ) as error:
                        with self.locked():
                            latest = self.get(identity)
                            if (
                                not latest["attempts"]
                                or latest["attempts"][-1]["id"] != attempt
                                or (latest.get("plan") or {}).get("sha256")
                                != item["plan"]["sha256"]
                                or latest["feedback_sha256"] != item["feedback_sha256"]
                            ):
                                self.append(
                                    latest, "worker_superseded", attempt=attempt, error=str(error)
                                )
                                return latest
                            if latest["state"] != "implementing":
                                return latest
                            latest.update(
                                state="retry", verification={"passed": False, "error": str(error)}
                            )
                            self.append(latest, "worker_failed")
            with self.locked():
                item = self.get(identity)
                if not item.get("paused") and item["state"] == "retry":
                    item["state"] = "exhausted"
                    self.append(item, "budget_exhausted", max_attempts=max_attempts)
                return item

    def api(self, body):
        """Data-only HTTP surface. Process execution is intentionally absent."""
        action = body.get("action")
        if action == "query":
            return self.query(body.get("id"))
        if action == "add":
            return self.add(
                body["text"], body.get("source"), body.get("id"), body.get("expect_revision")
            )
        if action in ("pause", "resume", "clarify", "answer"):
            if "expect_revision" not in body:
                raise ValueError("A revision guard is required")
            return self.control(body["id"], body["expect_revision"], action, body.get("text", ""))
        raise ValueError(
            "Execution is CLI-only; HTTP supports query/add/pause/resume/clarify/answer"
        )


def run(args):
    try:
        store = Feedback(args.project)
        action = args.action
        if action == "query":
            result = store.query(args.id)
        elif action == "add":
            result = store.add(args.text, identity=args.id, revision=args.expect_revision)
        elif action in ("plan", "submit"):
            receipt = json.loads(safe(store.root, args.receipt).read_text())
            result = getattr(store, action)(args.id, args.expect_revision, receipt)
        elif action == "dispatch":
            result = store.dispatch(args.id, args.expect_revision, args.worker)
            result["bundle"] = str(store.bundle(result, args.bundle))
        elif action == "verify":
            result = store.verify(args.id, args.expect_revision)
        elif action == "run":
            result = store.run_loop(
                args.id,
                json.loads(args.worker_command),
                worker=args.worker,
                max_attempts=args.max_attempts,
                timeout=args.timeout,
            )
        else:
            result = store.control(args.id, args.expect_revision, action, args.text)
        print(json.dumps(result, indent=2))
        return 0 if action not in ("verify", "run") or result["state"] == "addressed" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Feedback transition rejected: " + str(error), file=sys.stderr)
        return 2


def register(commands):
    parser = commands.add_parser(
        "feedback", help="delegate, independently verify and close review feedback"
    )
    subs = parser.add_subparsers(dest="action", required=True)
    for action in (
        "add",
        "query",
        "plan",
        "dispatch",
        "submit",
        "verify",
        "run",
        "pause",
        "resume",
        "recover",
        "clarify",
        "answer",
    ):
        child = subs.add_parser(action)
        child.add_argument("--project", default=".")
        child.add_argument("--id", required=action not in ("add", "query"))
        if action not in ("query", "run"):
            child.add_argument("--expect-revision", type=int, required=action != "add")
        if action in ("plan", "submit"):
            child.add_argument(
                "--receipt", required=True, help="project-relative JSON plan/submission"
            )
        if action in ("add", "pause", "resume", "recover", "clarify", "answer"):
            child.add_argument(
                "--text", default="", required=action in ("add", "clarify", "answer")
            )
        if action in ("dispatch", "run"):
            child.add_argument("--worker", default="command")
        if action == "dispatch":
            child.add_argument(
                "--bundle", required=True, help="new directory for immutable worker task copy"
            )
        if action == "run":
            child.add_argument(
                "--worker-command",
                required=True,
                help="explicitly authorized JSON argv; {bundle} is task directory",
            )
            child.add_argument("--max-attempts", type=int, default=3)
            child.add_argument("--timeout", type=int, default=1800)
        child.set_defaults(func=run)
