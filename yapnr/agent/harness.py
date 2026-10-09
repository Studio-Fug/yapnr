"""Journaled continuation of a design session until a user checkpoint or pause."""

import hashlib
import http.client
import threading
import time
import uuid
from urllib.parse import quote

from yapnr.agent import requirements, reviews, workspace
from yapnr.agent.workflow import query

CHECKPOINTS = {"requirements_review", "complete", "blocked", "exhausted", "cancelled"}
INSTRUCTIONS = (
    "Follow the installed yapnr engineering workflow. Read `yapnr workflow query` before acting, "
    "and advance with genuine evidence through `yapnr workflow next`. Produce YAML requirements and "
    "risk analysis first in requirements/*.yaml using rules_requirements schemas. Keep user needs, "
    "requirements, risks, mitigations, methods and all their traces in that model. Markdown is "
    "derived prose. Use `yapnr requirements query`; publish reports as artifacts, link actual "
    "native requirements/evidence/*.rr.yaml cases with `yapnr requirements link-evidence`, "
    "and preserve their actual results, levels and requirements/DUT stamps. Publish and present "
    "their review artifacts before recording the "
    "requirements_review transition; the harness pauses there for the designer. Do not start "
    "speculative design past that checkpoint until the designer replies. Reconcile user "
    "refinements and invalidate affected evidence. Continue authorized bounded implementation "
    "until the next user-review checkpoint. During schematic capture, write atopile sources in "
    "the article and run bounded incremental builds/export after meaningful valid edits. Export "
    "the actual current netlist as graph.json; if multiple exports exist, point "
    ".yapnr/workspace/design.json at the active project-relative graph path. Keep last valid "
    "exports on build failure and report the actual failure. Never invent schematic connectivity. "
    " Completion pauses for PCB review before fabrication; "
    "never upload, order or pay. Harness messages are automation, not user approval. Inspect "
    "existing jobs and artifacts before retrying interrupted work; do not duplicate workers."
)


def identity(root):
    stat = root.stat()
    return hashlib.sha256(f"{root.resolve()}:{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()


def read(root):
    return workspace.read_json(
        root / ".yapnr/workspace/harness.json", {"status": "off", "enabled": False}
    )


class Manager:
    def __init__(self, project, directory, remote, names, clock=time.time):
        self.project, self.directory, self.remote, self.names = project, directory, remote, names
        self.clock = clock
        self.stop_event = threading.Event()
        self.locks = {}
        self.lock = threading.Lock()
        self.idle = {}
        self.thread = None

    def mutex(self, name):
        with self.lock:
            return self.locks.setdefault(name, threading.RLock())

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)

    def write(self, root, value):
        with workspace.lock(root) as folder:
            workspace.atomic(folder / "harness.json", workspace.encoded(value))

    def update(self, root, epoch, **fields):
        with workspace.lock(root) as folder:
            value = read(root)
            if value.get("epoch") != epoch:
                return
            value.update(fields)
            workspace.atomic(folder / "harness.json", workspace.encoded(value))

    def arm(self, name, session, model, agent="build", initial_request=None):
        root = self.project(name)
        query(root)  # Never infer a controller state from conversational text.
        with self.mutex(name):
            value = {
                "schema": "yapnr-harness-v1",
                "enabled": True,
                "session": session,
                "workspace_identity": identity(root),
                "model": model,
                "agent": agent,
                "epoch": uuid.uuid4().hex,
                "status": "running",
                "continuations": 0,
                "armed_checkpoint": query(root)["state"] + ":" + str(query(root)["revision"]),
                "started": self.clock(),
                "last_request": initial_request,
                "armed_reviews": [
                    r["artifact"]
                    for r in reviews.state(root)["items"].values()
                    if r["status"] in ("pending_review", "feedback_pending")
                ],
            }
            self.write(root, value)
            workspace.record(root, {"source": "harness-armed", "harness": value})
            self.idle[name] = 0
            return value

    def stop(self, name, session=None):
        root = self.project(name)
        with self.mutex(name):
            value = read(root)
            if session and value.get("session") != session:
                return value
            value.update(enabled=False, status="stopped")
            self.write(root, value)
            workspace.record(root, {"source": "user-harness-stop", "session": value.get("session")})
            return value

    def request(self, root, path, data=None, method=None):
        if data is not None:
            workspace.record(
                root,
                {
                    "source": "harness-request",
                    "path": path,
                    "method": method or "POST",
                    "request": data,
                },
            )
        return self.remote(path, data, method)

    def review_artifacts(self, root, state, revision=None):
        published = []
        if state == "requirements_review":
            models = requirements.files(root)
            sources = (
                [(p, "Requirements model: " + p) for p in models]
                if models
                else [
                    ("requirements/manifest.md", "Requirements specification"),
                    ("requirements/risks.md", "Risk analysis"),
                ]
            )
            for file, title in sources:
                if (root / file).is_file():
                    published.append(workspace.publish(root, file, "document", title)["id"])
        elif state == "complete":
            registry = workspace.read_json(
                root / ".yapnr/workspace/artifacts.json", {"artifacts": []}
            )
            published = [a["id"] for a in registry["artifacts"] if a["kind"] == "board"]
        if published:
            reviews.request(root, published, state, revision)

    def tick(self, name):
        with self.mutex(name):
            root = self.project(name)
            value = read(root)
            if not value.get("enabled"):
                return
            if self.clock() < value.get("retry_after", 0):
                return
            epoch, session = value["epoch"], value["session"]
            if value.get("workspace_identity") != identity(root):
                self.update(root, epoch, enabled=False, status="resume_required")
                return  # Moving/importing a workspace does not authorize paid turns.
            suffix = "?directory=" + quote(self.directory(name), safe="")
            info = self.remote("/session/" + session + suffix, None, None)
            if info.get("directory") != self.directory(name):
                raise ValueError("Harness session belongs to another project")
            workflow = query(root)
            statuses = self.remote("/session/status" + suffix, None, None)
            busy = statuses.get(session, {}).get("type", "idle") != "idle"
            for resource in ("question", "permission"):
                requests = self.remote("/" + resource + suffix, None, None)
                if any(item.get("sessionID") == session for item in requests):
                    self.update(root, epoch, status="waiting_" + resource)
                    return  # Preserve the native pending request, including its tool call.
            if value.get("status") in ("waiting_question", "waiting_permission"):
                value.update(started=self.clock())
                self.update(
                    root,
                    epoch,
                    started=value["started"],
                )
                workspace.record(
                    root, {"source": "harness-user-request-answered", "session": session}
                )
            pending_reviews = [
                r
                for r in reviews.state(root)["items"].values()
                if r["status"] in ("pending_review", "feedback_pending")
            ]
            if pending_reviews and workflow["state"] not in CHECKPOINTS:
                if busy and any(
                    r["artifact"] not in value.get("armed_reviews", []) for r in pending_reviews
                ):
                    self.request(root, "/session/" + session + "/abort" + suffix, {})
                if not busy or any(
                    r["artifact"] not in value.get("armed_reviews", []) for r in pending_reviews
                ):
                    self.update(root, epoch, status="waiting_artifact_review")
                    return
            stage = workflow["state"]
            if stage in CHECKPOINTS:
                checkpoint = stage + ":" + str(workflow["revision"])
                if value.get("checkpoint") != checkpoint:
                    self.review_artifacts(root, stage, workflow["revision"])
                    workspace.record(
                        root,
                        {
                            "source": "harness-checkpoint",
                            "state": stage,
                            "revision": workflow["revision"],
                            "session": session,
                        },
                    )
                # A genuine user reply starts one turn at the existing review checkpoint.
                # A newly reached checkpoint stops the current autonomous turn immediately.
                if busy and checkpoint != value.get("armed_checkpoint"):
                    self.request(root, "/session/" + session + "/abort" + suffix, {})
                self.update(
                    root,
                    epoch,
                    status=(
                        "waiting_review"
                        if stage in ("requirements_review", "complete")
                        else "paused_" + stage
                    ),
                    checkpoint=checkpoint,
                )
                return
            self.update(root, epoch, checkpoint=None)
            now = self.clock()
            messages = self.remote("/session/" + session + "/message" + suffix, None, None)
            if busy:
                self.idle[name] = 0
                # A healthy runtime may execute many tools in a single long turn.
                # Its tools retain their own deadlines; elapsed turn time is not a stall.
                self.update(root, epoch, status="running")
                return
            self.idle[name] = self.idle.get(name, 0) + 1
            if self.idle[name] < 2:
                return  # Avoid racing a just-started runtime turn.
            request_ids = {
                part.get("metadata", {}).get("yapnr_request")
                for m in messages
                for part in m.get("parts", [])
            }
            if value.get("last_request") and value["last_request"] not in request_ids:
                self.update(root, epoch, status="delivery_uncertain")
                return  # Never blindly replay a possibly accepted model request.
            last_user = next((m for m in reversed(messages) if m["info"]["role"] == "user"), None)
            last_assistant = next(
                (m for m in reversed(messages) if m["info"]["role"] == "assistant"), None
            )
            if (
                last_assistant
                and last_assistant["info"].get("error")
                and (
                    not last_user
                    or last_assistant["info"]["time"]["created"]
                    >= last_user["info"]["time"]["created"]
                )
            ):
                error_id = last_assistant["info"]["id"]
                if value.get("last_model_error") != error_id:
                    delay = min(300, max(30, value.get("retry_delay", 15) * 2))
                    self.update(
                        root,
                        epoch,
                        status="model_error",
                        last_model_error=error_id,
                        retry_after=now + delay,
                        retry_delay=delay,
                    )
                    workspace.record(
                        root,
                        {
                            "source": "harness-model-retry",
                            "session": session,
                            "message": error_id,
                            "delay_seconds": delay,
                        },
                    )
                    return

            if last_user and not last_user["parts"]:
                return
            # Repair only abandoned tool calls: runtime idle twice, no user request,
            # and the declared timeout has elapsed. Preserve input and record failure.
            for message in messages:
                for part in message.get("parts", []):
                    state = part.get("state", {})
                    if part.get("type") != "tool" or state.get("status") not in (
                        "running",
                        "pending",
                    ):
                        continue
                    start = (
                        state.get("time", {}).get("start", message["info"]["time"]["created"])
                        / 1000
                    )
                    timeout = state.get("input", {}).get("timeout", 60000) / 1000
                    if now - start < max(timeout, 60) + 30:
                        return
                    repaired = {
                        **part,
                        "state": {
                            "status": "error",
                            "input": state.get("input", {}),
                            "error": (
                                "Interrupted tool: runtime is idle and no result was captured. "
                                "Inspect existing work before retrying."
                            ),
                            "metadata": {**state.get("metadata", {}), "interrupted": True},
                            "time": {"start": int(start * 1000), "end": int(now * 1000)},
                        },
                    }
                    self.request(
                        root,
                        "/session/"
                        + session
                        + "/message/"
                        + message["info"]["id"]
                        + "/part/"
                        + part["id"]
                        + suffix,
                        repaired,
                        "PATCH",
                    )
            # One persisted claim across processes, written before sending a paid request.
            with workspace.lock(root):
                current = read(root)
                if current.get("epoch") != epoch or not current.get("enabled"):
                    return
                if (
                    current.get("status") == "sending"
                    and current.get("last_request") not in request_ids
                ):
                    return
                count = current["continuations"] + 1
                request_id = hashlib.sha256((epoch + str(count)).encode()).hexdigest()
                current.update(status="sending", continuations=count, last_request=request_id)
                workspace.atomic(root / ".yapnr/workspace/harness.json", workspace.encoded(current))
            request = {
                "model": value["model"],
                "agent": value["agent"],
                "system": INSTRUCTIONS,
                "parts": [
                    {
                        "type": "text",
                        "synthetic": True,
                        "metadata": {"yapnr_harness": True, "yapnr_request": request_id},
                        "text": "Workflow harness continuation. Continue from the recorded state "
                        + stage
                        + " to the next genuine user-review checkpoint. "
                        + INSTRUCTIONS,
                    }
                ],
            }
            try:
                self.request(root, "/session/" + session + "/prompt_async" + suffix, request)
            except Exception:
                self.update(root, epoch, status="delivery_uncertain")
                raise
            self.update(root, epoch, status="running")
            self.idle[name] = 0

    def run(self):
        while not self.stop_event.wait(5):
            for name in self.names():
                try:
                    self.tick(name)
                except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
                    root = self.project(name)
                    value = read(root)
                    if value.get("enabled"):
                        self.update(
                            root,
                            value.get("epoch"),
                            status="connection_error",
                            retry_after=self.clock() + 30,
                        )
