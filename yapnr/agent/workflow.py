"""Evidence-gated, declarative workflow controller. Receipts are assertions, not proof."""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from yapnr.agent.cli import initialize


def machine():
    return json.loads(Path(__file__).with_name("workflow.json").read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_file(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("Evidence and artifacts must be existing project-relative files")
    path = root / name
    if not path.is_file():
        raise ValueError("Evidence and artifacts must be existing project-relative files")
    if root not in path.resolve().parents or path.is_symlink():
        raise ValueError("Evidence and artifacts must stay inside the project")
    return path


def contract(root):
    return {
        name: digest(local_file(root, "requirements/" + name + ".md").read_bytes())
        for name in ("manifest", "risks")
    }


@contextmanager
def locked(root, create=False):
    folder = root / ".yapnr/workflow"
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    elif not (folder / "state.json").is_file():
        raise ValueError("Run yapnr workflow init first")
    with (folder / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield folder


def save(folder, state):
    # One atomic record contains both current state and transition history.
    fd, name = tempfile.mkstemp(dir=folder, prefix="state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, folder / "state.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(folder):
    path = folder / "state.json"
    if not path.is_file():
        raise ValueError("Run yapnr workflow init first")
    state = json.loads(path.read_text())
    if state.get("schema") != "yapnr-workflow-state-v1":
        raise ValueError("Unsupported workflow state schema")
    if state.get("machine_sha256") != digest(
        Path(__file__).with_name("workflow.json").read_bytes()
    ):
        raise ValueError("Workflow definition changed; explicit migration is required")
    return state


def init(project, directive=""):
    root = Path(project).resolve()
    with locked(root, create=True) as folder:
        if (folder / "state.json").exists():
            return load(folder)
        if not (root / ".git").exists():
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
        initialized = initialize(root, directive)
        state = {
            "schema": "yapnr-workflow-state-v1",
            "state": machine()["initial"],
            "machine_sha256": digest(Path(__file__).with_name("workflow.json").read_bytes()),
            "revision": 0,
            "contract": None,
            "accepted_revision": None,
            "bootstrap_contract": {
                name: value if "requirements/" + name + ".md" in initialized["created"] else None
                for name, value in contract(root).items()
            },
            "requirements": [],
            "artifacts": [],
            "history": [],
        }
        save(folder, state)
        return state


def query(project):
    root = Path(project).resolve()
    with locked(root) as folder:
        state = load(folder)
        result = dict(state)
        try:
            current = contract(root)
        except (OSError, ValueError):
            current = None
        result["contract_current"] = current is not None and state["contract"] == current
        result["speculative"] = state["accepted_revision"] != state["revision"]
        result["available_events"] = [
            t["event"] for t in machine()["transitions"] if state["state"] in t["from"]
        ]
        result["evidence_template"] = {
            "schema": "yapnr-workflow-evidence-v1",
            "event": "<available event>",
            "revision": state["revision"],
            "contract": current,
            "source": "agent",
            "note": "<reason and evidence provenance>",
            "artifacts": [],
        }
        return result


def snapshot(folder, data):
    name = digest(data)
    target = folder / "evidence" / name
    target.parent.mkdir(exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError("Evidence store integrity failure")
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(data)
    return name


def next_state(project, event, evidence):
    root = Path(project).resolve()
    with locked(root) as folder:
        state = load(folder)
        transition = next(
            (
                t
                for t in machine()["transitions"]
                if t["event"] == event and state["state"] in t["from"]
            ),
            None,
        )
        if transition is None:
            raise ValueError("Event is not permitted from the current state")
        raw = local_file(root, evidence).read_bytes()
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise ValueError("Evidence receipt must be an object")
        if (
            receipt.get("schema") != "yapnr-workflow-evidence-v1"
            or receipt.get("event") != event
            or receipt.get("revision") != state["revision"]
            or type(receipt.get("revision")) is not int
            or receipt.get("source") not in ("user", "agent", "tool")
            or not isinstance(receipt.get("note"), str)
            or not receipt["note"].strip()
        ):
            raise ValueError("Evidence needs matching schema, event, revision, source and note")
        current = contract(root)
        if receipt.get("contract") != current:
            raise ValueError("Evidence does not match the current requirements and risks")
        revising = event == "user-revised-requirements-specification"
        submitting = event == "requirements-specification-ready"
        if (
            not (revising or submitting)
            and state["contract"] is not None
            and current != state["contract"]
        ):
            raise ValueError("Contract changed; record a requirements revision before continuing")
        artifacts = receipt.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError("Artifacts must be a list")
        contents = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("Artifact entries must be objects")
            data = local_file(root, artifact["path"]).read_bytes()
            if digest(data) != artifact.get("sha256"):
                raise ValueError("Artifact hash mismatch")
            contents.append(data)
        target = transition["to"]
        if submitting or revising:
            ids = receipt.get("requirements")
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(i, str) or not i for i in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("Supply the complete unique requirement ID list")
            if revising and (
                receipt["source"] != "user"
                or not isinstance(receipt.get("reviewer"), str)
                or not receipt["reviewer"].strip()
                or current == state["contract"]
            ):
                raise ValueError("A user revision needs a reviewer and changed contract documents")
            if submitting:
                # Untouched bootstrap templates cannot count as a captured contract.
                if (
                    b"No requirements are approved by this template."
                    in local_file(root, "requirements/manifest.md").read_bytes()
                ):
                    raise ValueError("Replace the bootstrap requirements template before review")
                if any(
                    current[name] == state["bootstrap_contract"][name]
                    for name in ("manifest", "risks")
                ):
                    raise ValueError("Write both requirements and risk analysis before review")
            state.update(
                revision=state["revision"] + 1,
                contract=current,
                accepted_revision=None,
                requirements=ids,
                artifacts=[],
            )
            if state["state"] in ("blocked", "exhausted", "cancelled"):
                state["resume_state"] = "requirements_review"
                target = state["state"]
            for name in ("manifest", "risks"):
                snapshot(folder, local_file(root, "requirements/" + name + ".md").read_bytes())
        elif event == "user-accepted-requirements-specification":
            if (
                receipt["source"] != "user"
                or not isinstance(receipt.get("reviewer"), str)
                or not receipt["reviewer"].strip()
            ):
                raise ValueError("Acceptance needs a user review receipt and reviewer")
            state["accepted_revision"] = state["revision"]
            target = "schematic" if state["state"] == "requirements_review" else state["state"]
        elif event in (
            "schematic-ready",
            "routing-finished",
            "fixup-applied",
            "engine-repair-ready",
        ):
            if not artifacts:
                raise ValueError("This transition requires hashed implementation artifacts")
            state["artifacts"] = artifacts
        elif event in ("verification-failed", "engine-repair-required"):
            if not artifacts:
                raise ValueError(
                    "Save failure reports or engine repair reproducers as hashed artifacts"
                )
        elif event == "verification-passed":
            if state["accepted_revision"] != state["revision"]:
                raise ValueError("Speculative work cannot complete without user acceptance")
            board = receipt.get("final_artifact_sha256")
            if board not in {a["sha256"] for a in state["artifacts"]}:
                raise ValueError("Verification must reference the current routed artifact")
            for artifact in state["artifacts"]:
                if digest(local_file(root, artifact["path"]).read_bytes()) != artifact["sha256"]:
                    raise ValueError(
                        "Current artifact changed after routing; rerun affected stages"
                    )
            checks = receipt.get("checks", [])
            if (
                not isinstance(checks, list)
                or len(checks) != len(state["requirements"])
                or any(not isinstance(c, dict) for c in checks)
                or {c.get("requirement_id") for c in checks} != set(state["requirements"])
                or any(
                    c.get("status") != "pass" or c.get("artifact_sha256") != board for c in checks
                )
            ):
                raise ValueError(
                    "Every requirement needs a passing check on the same final artifact"
                )
            gates = receipt.get("gates", {})
            if not isinstance(gates, dict):
                raise ValueError("Verification gates must be an object")
            for gate in ("native_drc", "connectivity"):
                if gates.get(gate) != {"status": "pass", "artifact_sha256": board}:
                    raise ValueError("Native DRC and connectivity must pass on the final artifact")
            if not artifacts:
                raise ValueError(
                    "Save the underlying verification reports as hashed evidence artifacts"
                )
        elif event in ("blocked", "methods-exhausted", "cancelled"):
            state.setdefault("resume_state", state["state"])
        elif event == "resume":
            if (
                receipt["source"] != "user"
                or not isinstance(receipt.get("reviewer"), str)
                or not receipt["reviewer"].strip()
            ):
                raise ValueError("Resume requires an explicit user receipt")
            target = state.pop("resume_state")
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "from": state["state"],
            "to": target,
            "revision": state["revision"],
            "receipt_sha256": snapshot(folder, raw),
            "artifact_sha256s": [snapshot(folder, data) for data in contents],
        }
        state["state"] = target
        state["history"].append(record)
        save(folder, state)
        return query_unlocked(state)


def query_unlocked(state):
    return {**state, "speculative": state["accepted_revision"] != state["revision"]}


def run(args):
    try:
        if args.action == "init":
            result = init(args.project, args.directive)
        elif args.action == "query":
            result = query(args.project)
        else:
            result = next_state(args.project, args.event, args.evidence)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Avoid printing receipt contents, artifact paths or external command output.
        print(
            "Workflow transition rejected: "
            + type(error).__name__
            + ": "
            + (
                str(error)
                if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError)
                else "invalid or unavailable workflow input"
            ),
            file=sys.stderr,
        )
        return 2


def register(commands):
    parser = commands.add_parser("workflow", help="evidence-gated PCB iteration state machine")
    subs = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "query", "next"):
        child = subs.add_parser(action)
        child.add_argument("--project", default=".")
        if action == "init":
            child.add_argument("--directive", default="")
        if action == "next":
            group = child.add_mutually_exclusive_group(required=True)
            for transition in machine()["transitions"]:
                group.add_argument(
                    "--" + transition["event"],
                    dest="event",
                    action="store_const",
                    const=transition["event"],
                )
            child.add_argument("--evidence", required=True, help="project-relative JSON receipt")
        child.set_defaults(func=run)
