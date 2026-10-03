"""The boundary to Google Cloud: ``gcloud`` as a subprocess, and a fake for tests and dry runs.

Every call goes through ``Gcloud.run``: the exact argv (with ``--project``, the impersonated
submit account and ``--quiet`` appended), a timeout, JSON output where a result is parsed. No
Google client library is imported, so ``requirements.lock`` and the image stay as they are.

``FakeCloud`` builds the same argv, records it, and answers from an in-memory model of the two
buckets and of Batch jobs, so the whole plan-submit-status-fetch cycle runs offline and tests
assert the exact commands. ``yapnr exp submit --dry-run`` uses it and prints what it recorded.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT_S = 300
# Set (to anything) to make every real gcloud call fail before it runs: the tests set it, so a
# mistake in a test can never reach a cloud.
NO_CLOUD_ENV = "YAPNR_EXP_NO_CLOUD"


class CloudError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int, stderr: str):
        self.argv = list(argv)
        self.code = code
        self.stderr = stderr
        super().__init__("gcloud failed (%d): %s" % (code, stderr.strip()[:500]))

    @property
    def not_found(self) -> bool:
        text = self.stderr.lower()
        return any(s in text for s in ("not found", "notfound", "no urls matched", "404"))


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout or "null")


class Gcloud:
    """``gcloud`` with the owner's project and the impersonated submit account."""

    def __init__(
        self,
        project: str,
        impersonate: Optional[str],
        gcloud: str = "gcloud",
        configuration: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self.project = project
        self.impersonate = impersonate
        self.gcloud = gcloud
        self.configuration = configuration
        self.timeout = timeout
        self.calls: List[List[str]] = []

    def argv(self, args: Sequence[str]) -> List[str]:
        out = [self.gcloud, *args, "--project=%s" % self.project, "--quiet"]
        if self.impersonate:
            out.append("--impersonate-service-account=%s" % self.impersonate)
        if self.configuration:
            out.append("--configuration=%s" % self.configuration)
        return out

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: Optional[float] = None,
        check: bool = True,
        input_text: Optional[str] = None,
    ) -> Result:
        argv = self.argv(args)
        self.calls.append(argv)
        result = self._execute(argv, timeout or self.timeout, input_text)
        if check and result.code != 0:
            raise CloudError(argv, result.code, result.stderr)
        return result

    def json(self, args: Sequence[str], **kwargs) -> Any:
        return self.run(list(args) + ["--format=json"], **kwargs).json()

    def _execute(self, argv: List[str], timeout: float, input_text: Optional[str]) -> Result:
        if os.environ.get(NO_CLOUD_ENV):
            raise CloudError(argv, 126, "cloud calls are disabled (%s is set)" % NO_CLOUD_ENV)
        if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
            raise CloudError(argv, 127, "%s not found (install the gcloud CLI)" % argv[0])
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, input=input_text
            )
        except subprocess.TimeoutExpired as err:
            raise CloudError(argv, 124, "timed out after %.0f s" % timeout) from err
        return Result(done.returncode, done.stdout, done.stderr)

    def transcript(self) -> str:
        """The calls, one per line, as a shell would run them (``gcloud`` for the binary)."""
        return "\n".join(shlex.join(["gcloud"] + c[1:]) for c in self.calls)


GLOBAL_FLAGS = ("--project=", "--impersonate-service-account=", "--quiet", "--configuration=")


def _strip_globals(argv: List[str]) -> List[str]:
    return [a for a in argv[1:] if not a.startswith(GLOBAL_FLAGS)]


def _flag(args: List[str], name: str) -> Optional[str]:
    for arg in args:
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


class FakeCloud(Gcloud):
    """Records every call and answers from an in-memory model (objects, Batch jobs).

    ``objects`` maps ``gs://bucket/path`` to bytes. ``jobs`` maps a job's full name to its
    description. ``handlers`` can override any command: a function of the stripped argv that
    returns a Result or None (fall through).
    """

    def __init__(self, project: str = "example-project", impersonate: Optional[str] = None, **kw):
        super().__init__(project, impersonate, **kw)
        self.objects: Dict[str, bytes] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, List[Dict[str, Any]]] = {}
        self.handlers: List[Callable[[List[str]], Optional[Result]]] = []

    def _execute(self, argv: List[str], timeout: float, input_text: Optional[str]) -> Result:
        args = _strip_globals(argv)
        for handler in self.handlers:
            result = handler(args)
            if result is not None:
                return result
        if args[:2] == ["storage", "cp"]:
            return self._cp(args[2:])
        if args[:2] == ["storage", "cat"]:
            hits = sorted(k for k in self.objects if fnmatch.fnmatchcase(k, args[2]))
            if not hits:
                return Result(1, "", "ERROR: No URLs matched: %s" % args[2])
            return Result(0, "".join(self.objects[h].decode() for h in hits), "")
        if args[:3] == ["storage", "objects", "describe"]:
            if args[3] in self.objects:
                return Result(
                    0, json.dumps({"name": args[3], "size": len(self.objects[args[3]])}), ""
                )
            return Result(1, "", "ERROR: NotFound: %s" % args[3])
        if args[:2] == ["storage", "ls"]:
            pattern = args[2]
            hits = sorted(
                k for k in self.objects if fnmatch.fnmatchcase(k, pattern.replace("**", "*"))
            )
            if not hits:
                return Result(1, "", "ERROR: One or more URLs matched no objects.")
            return Result(0, "".join(h + "\n" for h in hits), "")
        if args[:2] == ["storage", "rm"]:
            self.objects.pop(args[2], None)
            return Result(0, "", "")
        if args[:2] == ["storage", "rsync"]:
            return self._rsync(args[2:])
        if args[:3] == ["batch", "jobs", "submit"]:
            return self._submit(args[3:])
        if args[:3] == ["batch", "jobs", "describe"]:
            name = self._job_name(args[3], _flag(args, "--location"))
            if name not in self.jobs:
                return Result(1, "", "ERROR: NOT_FOUND: %s" % name)
            return Result(0, json.dumps(self.jobs[name]), "")
        if args[:3] in (["batch", "jobs", "cancel"], ["batch", "jobs", "delete"]):
            name = self._job_name(args[3], _flag(args, "--location"))
            if name in self.jobs:
                self.jobs[name]["status"]["state"] = (
                    "CANCELLATION_IN_PROGRESS" if args[2] == "cancel" else "DELETION_IN_PROGRESS"
                )
            return Result(0, "{}", "")
        if args[:3] == ["batch", "tasks", "list"]:
            name = self._job_name(_flag(args, "--job"), _flag(args, "--location"))
            return Result(0, json.dumps(self.tasks.get(name, [])), "")
        if args[:2] == ["logging", "read"]:
            return Result(0, "[]", "")
        return Result(0, "[]" if "--format=json" in args else "", "")

    def _job_name(self, job: Optional[str], location: Optional[str]) -> str:
        return "projects/%s/locations/%s/jobs/%s" % (self.project, location, job)

    def _cp(self, args: List[str]) -> Result:
        paths = [a for a in args if not a.startswith("--")]
        src, dest = paths[0], paths[1]
        no_clobber = "--no-clobber" in args
        if src.startswith("gs://"):
            if src not in self.objects:
                return Result(1, "", "ERROR: No URLs matched: %s" % src)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(self.objects[src])
            return Result(0, "", "")
        if no_clobber and dest in self.objects:
            return Result(0, "", "Skipping existing destination item (no-clobber): %s" % dest)
        self.objects[dest] = Path(src).read_bytes()
        return Result(0, "", "")

    def _rsync(self, args: List[str]) -> Result:
        paths = [a for a in args if not a.startswith("--")]
        src, dest = paths[0].rstrip("/") + "/", Path(paths[1])
        exclude = _flag(args, "--exclude")
        for key, data in sorted(self.objects.items()):
            if not key.startswith(src):
                continue
            rel = key[len(src) :]
            if exclude and re.search(exclude, rel):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return Result(0, "", "")

    def _submit(self, args: List[str]) -> Result:
        job_id = args[0]
        location = _flag(args, "--location")
        config = json.loads(Path(_flag(args, "--config")).read_text())
        name = self._job_name(job_id, location)
        if name in self.jobs:
            return Result(1, "", "ERROR: ALREADY_EXISTS: %s" % name)
        job = dict(config)
        job.update(
            name=name,
            uid="%s-%08d" % (job_id[:50], len(self.jobs)),
            status={"state": "QUEUED", "statusEvents": [], "taskGroups": {}},
            createTime="2026-10-02T00:00:00Z",
        )
        self.jobs[name] = job
        return Result(0, json.dumps(job), "")
