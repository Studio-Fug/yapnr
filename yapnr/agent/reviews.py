"""Content-bound artifact review and canonical feedback notes."""

import json
from pathlib import Path

from yapnr.agent import threads, workflow, workspace


def artifact(root, identifier):
    registry = workspace.read_json(
        Path(root) / ".yapnr/workspace/artifacts.json", {"artifacts": []}
    )
    item = next((a for a in registry["artifacts"] if a["id"] == identifier), None)
    if (
        not item
        or workspace.file_sha(workspace.relative_file(root, item["path"])) != item["sha256"]
    ):
        raise ValueError("Review requires an intact published artifact")
    return item


def read(root):
    return workspace.read_json(
        Path(root) / ".yapnr/workspace/reviews.json",
        {"schema": "yapnr-artifact-reviews-v1", "items": {}},
    )


def state(root):
    data = read(root)
    notes = {n["id"]: n for n in threads.notes(root).all()}
    for value in data["items"].values():
        value["feedback"] = [notes[i] for i in value.get("notes", []) if i in notes]
        value["unresolved"] = [
            n for n in value["feedback"] if n["status"] not in ("resolved", "applied", "rejected")
        ]
        value["questions"] = [n for n in value["unresolved"] if n["kind"] == "question"]
        if value["unresolved"] and value["status"] != "superseded":
            value["status"] = "feedback_pending"
    return data


def request(root, identifiers, stage="artifact", revision=None):
    root = Path(root).resolve()
    items = [artifact(root, i) for i in identifiers]
    if not items:
        raise ValueError("Select artifacts for review")
    with workspace.lock(root) as folder:
        data = read(root)
        for item in items:
            existing = data["items"].get(item["id"])
            if existing and existing.get("revision") == revision:
                continue
            notes = list(existing.get("notes", [])) if existing else []
            for identifier, old in data["items"].items():
                if identifier != item["id"] and old.get("source_path") == item["source_path"]:
                    old["status"] = "superseded"
                    notes.extend(old.get("notes", []))
            data["items"][item["id"]] = {
                "artifact": item["id"],
                "sha256": item["sha256"],
                "source_path": item["source_path"],
                "title": item["title"],
                "stage": stage,
                "revision": revision,
                "status": "pending_review",
                "notes": list(dict.fromkeys(notes)),
            }
        workspace.atomic(folder / "reviews.json", workspace.encoded(data))
    workspace.record(
        root,
        {
            "source": "artifact-review-requested",
            "artifacts": identifiers,
            "stage": stage,
            "revision": revision,
        },
    )
    return state(root)


def feedback(root, identifier, text, question=False, annotation=None):
    item = artifact(root, identifier)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Write review feedback or a question")
    note = threads.notes(root).create(
        {
            "title": "Review: " + item["title"][:170],
            "body": text,
            "kind": "question" if question else "observation",
            "tags": ["artifact-review"],
            "targets": [],
            "provenance": {"view": "artifact-review"},
        },
        {"kind": "user"},
    )
    with workspace.lock(root) as folder:
        data = read(root)
        value = data["items"].setdefault(
            identifier,
            {
                "artifact": identifier,
                "sha256": item["sha256"],
                "source_path": item["source_path"],
                "title": item["title"],
                "stage": "artifact",
                "status": "pending_review",
                "notes": [],
            },
        )
        value["notes"].append(note["id"])
        value["status"] = "feedback_pending"
        if annotation:
            artifact(root, annotation)
            value.setdefault("annotations", []).append(annotation)
        workspace.atomic(folder / "reviews.json", workspace.encoded(data))
    workspace.record(
        root, {"source": "user-artifact-feedback", "artifact": identifier, "note": note}
    )
    return note


def resolve(root, identifier, note, revision):
    value = read(root)["items"].get(identifier)
    if not value or note not in value["notes"]:
        raise ValueError("Feedback does not belong to this review")
    result = threads.notes(root).update(
        note, {"status": "resolved"}, {"kind": "user"}, expect_rev=revision
    )
    workspace.record(
        root, {"source": "user-review-feedback-resolved", "artifact": identifier, "note": note}
    )
    return result


def approve(root, identifiers):
    root = Path(root).resolve()
    items = [artifact(root, i) for i in identifiers]
    if not items:
        raise ValueError("Select artifacts for approval")
    for item in items:
        if item.get("metadata", {}).get("assembly_handoff"):
            from yapnr.agent import manufacturing

            release = next(
                (r for r in manufacturing.state(root)["releases"] if r["artifact"] == item["id"]),
                None,
            )
            if not release or not release["current"]:
                raise ValueError("Assembly inputs changed; prepare and review a new package")
    with workspace.lock(root) as folder:
        current = state(root)
        data = read(root)
        for item in items:
            value = current["items"].get(item["id"])
            if not value or value["status"] == "superseded" or value["unresolved"]:
                raise ValueError("Resolve review feedback and questions before approving")
        contract_items = [
            current["items"][item["id"]]
            for item in items
            if current["items"][item["id"]].get("stage") == "requirements_review"
        ]
        if contract_items:
            controller = workflow.query(root)
            required = {
                i
                for i, v in current["items"].items()
                if v.get("stage") == "requirements_review"
                and v.get("revision") == controller["revision"]
                and v["status"] != "superseded"
            }
            if not controller["contract_current"] or required != {item["id"] for item in items}:
                raise ValueError(
                    "Review all artifacts of the current unchanged requirements revision"
                )
            if controller["accepted_revision"] != controller["revision"]:
                receipt = {
                    **controller["evidence_template"],
                    "event": "user-accepted-requirements-specification",
                    "source": "user",
                    "reviewer": "workspace user",
                    "note": "Explicit Approve action for the complete content-bound requirements review",
                    "artifacts": [{"path": i["path"], "sha256": i["sha256"]} for i in items],
                }
                path = (
                    root / "reports/reviews" / (workspace.sha(workspace.encoded(receipt)) + ".json")
                )
                workspace.atomic(path, workspace.encoded(receipt))
                workflow.next_state(root, receipt["event"], path.relative_to(root).as_posix())
        for item in items:
            data["items"][item["id"]].update(status="approved", approved_by="workspace user")
        workspace.atomic(folder / "reviews.json", workspace.encoded(data))
    workspace.record(
        root,
        {
            "source": "user-artifact-approval",
            "artifacts": identifiers,
            "hashes": {i["id"]: i["sha256"] for i in items},
        },
    )
    return state(root)


def run(args):
    try:
        result = (
            state(args.project)
            if args.action == "query"
            else request(args.project, args.artifact, args.stage, args.revision)
        )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


def register(commands):
    parser = commands.add_parser("review", help="Request content-bound artifact review")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("request", "query"):
        action = actions.add_parser(name)
        action.add_argument("--project", default=".")
        if name == "request":
            action.add_argument("--artifact", action="append", required=True)
            action.add_argument("--stage", default="artifact")
            action.add_argument("--revision", type=int)
        action.set_defaults(func=run)
