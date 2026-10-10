"""Native YAML review using pinned rules_requirements trace/evidence semantics."""

import json
from pathlib import Path

from rules_requirements import case_keys, graph, ingest
from rules_requirements.server.workspace import Workspace, entity_payload, summary_rows

from yapnr.agent import workspace

PIN = "aabecadd6e8710a11e99ccb366e63d858d92f6c6"


def files(root, evidence=False):
    root = Path(root).resolve()
    folder = root / "requirements"
    result = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        is_evidence = path.name.endswith((".rr.yaml", ".rr.yml", ".rr.json", ".xml"))
        if is_evidence != evidence or (not evidence and path.suffix not in (".yaml", ".yml")):
            continue
        relative = path.relative_to(root).as_posix()
        workspace.relative_file(root, relative)
        if path.stat().st_size > 4 * 1024**2:
            raise ValueError("Requirements input exceeds the per-file size budget")
        result.append(relative)
    if len(result) > 128:
        raise ValueError("Requirements input exceeds the file-count budget")
    return result


def hashes(root):
    return {name: workspace.file_sha(Path(root) / name) for name in files(root)}


def model_workspace(root):
    root = Path(root).resolve()
    models = files(root)
    current = {"requirements_sha256": workspace.sha(workspace.encoded(hashes(root)))}
    state = workspace.read_json(root / ".yapnr/workflow/state.json", {})
    if state.get("artifacts"):
        current["dut_sha256"] = state["artifacts"][0]["sha256"]
    ws = Workspace(str(root), models, current_build=current, scan=False)
    evidence = ingest.collect([str(root / name) for name in files(root, True)])
    registry = workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})
    artifacts = {a["id"]: a for a in registry["artifacts"]}
    links = {}
    for case in evidence.cases:
        source = Path(case.source).relative_to(root).as_posix()
        record = workspace.publish(
            root, source, "report", "Verification evidence: " + Path(source).name
        )
        linked = [record]
        report = case.properties.get("yapnr.report_artifact")
        if report:
            artifact = artifacts.get(report)
            try:
                if not artifact or case.properties.get("yapnr.report_sha256") != artifact["sha256"]:
                    raise ValueError("Report link does not match its published artifact")
                path = workspace.relative_file(root, artifact["path"])
                if workspace.file_sha(path) != artifact["sha256"]:
                    raise ValueError("Report artifact bytes changed")
                linked.append(artifact)
            except (OSError, ValueError):
                case.status = "error"
                case.message = (
                    "Linked report artifact is missing or its content hash does not match"
                )
        links[str(case_keys.key_of(case))] = linked
    ws._evidence = evidence
    return ws, links, current


def review(root, entity=None):
    models = files(root)
    if not models:
        return {
            "schema": "yapnr-requirements-review-v1",
            "models": [],
            "entities": [],
            "issues": [],
            "empty": True,
            "pin": PIN,
        }
    ws, links, current = model_workspace(root)
    snap = ws.snapshot()
    entities = []
    for row in summary_rows(ws):
        payload = entity_payload(ws, row["id"])
        for case in payload["evidence"]:
            case["artifacts"] = links.get(case.get("target", "") + "#" + case.get("name", ""), [])
        # Members include missing/quarantined cases too; retain upstream's verdicts.
        for member in payload["members"]:
            member["artifacts"] = links.get(member.get("case"), [])
        entities.append({**row, **payload})
    statuses = {key: value.status for key, value in snap.matrix.verdicts.items()}
    nodes, edges = graph.build(snap.model, statuses, include_methods=True)
    return {
        "schema": "yapnr-requirements-review-v1",
        "pin": PIN,
        "models": models,
        "model_hashes": hashes(root),
        "current_build": current,
        "project": dict(snap.model.project),
        "entities": entities,
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "entity": i.entity,
                "path": i.location.path,
                "line": i.location.line,
            }
            for i in snap.issues
        ],
        "warnings": snap.warnings,
        "counts": snap.matrix.counts(),
        "graph": {
            "svg": graph.to_svg(nodes, edges, link_prefix="#requirement:"),
            "nodes": [n.__dict__ for n in nodes],
            "edges": [e.__dict__ for e in edges],
        },
    }


def link_evidence(project, record, report, case_name, target):
    """Bind an existing test result to an immutable report, without changing its outcome."""
    from rules_requirements._vendor import yaml

    root = Path(project).resolve()
    if record not in files(root, True) or not record.endswith((".rr.yaml", ".rr.yml")):
        raise ValueError("Select a native requirements evidence YAML record")
    registry = workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})
    artifact = next((a for a in registry["artifacts"] if a["id"] == report), None)
    if (
        not artifact
        or workspace.file_sha(workspace.relative_file(root, artifact["path"])) != artifact["sha256"]
    ):
        raise ValueError("Publish the intact report artifact before linking evidence")
    with workspace.lock(root):
        path = workspace.relative_file(root, record)
        data = yaml.safe_load(path.read_text())
        matches = [
            c
            for c in data.get("evidence", [])
            if (c.get("classname", "") + "::" if c.get("classname") else "") + c.get("name", "")
            == case_name
            and c.get("target", data.get("target")) == target
        ]
        if len(matches) != 1:
            raise ValueError("Select exactly one existing evidence case and target")
        matches[0].setdefault("properties", {}).update(
            {"yapnr.report_artifact": report, "yapnr.report_sha256": artifact["sha256"]}
        )
        workspace.atomic(path, yaml.safe_dump(data, sort_keys=False).encode())
    workspace.record(
        root,
        {
            "source": "evidence-report-linked",
            "record": record,
            "report": report,
            "case": case_name,
            "target": target,
        },
    )
    return {"record": record, "report": report, "case": case_name, "target": target}


def run(args):
    try:
        result = (
            review(args.project)
            if args.action == "query"
            else link_evidence(
                args.project, args.record, args.report_artifact, args.case, args.target
            )
        )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


def register(commands):
    parser = commands.add_parser(
        "requirements", help="YAML requirements, risk and evidence traceability"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("query", "link-evidence"):
        action = actions.add_parser(name)
        action.add_argument("--project", default=".")
        if name == "link-evidence":
            action.add_argument("--record", required=True)
            action.add_argument("--report-artifact", required=True)
            action.add_argument("--case", required=True)
            action.add_argument("--target", required=True)
        action.set_defaults(func=run)
