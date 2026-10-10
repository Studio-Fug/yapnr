"""Publish assembly-stock evidence for an immutable manufacturing candidate."""

from datetime import datetime, timezone
from pathlib import Path

from yapnr.agent import manufacturing_packages as packages
from yapnr.agent import requirements, workspace
from yapnr.fab import assembly, board
from yapnr.frontends.atopile.picker import availability


def check(project, identifier, vendor, quantity, include_through_hole=False):
    root = Path(project).resolve()
    if type(include_through_hole) is not bool:
        raise ValueError("Through-hole selection must be boolean")
    candidate = next((c for c in packages.candidates(root) if c["artifact"] == identifier), None)
    if not candidate or not candidate["current"]:
        raise ValueError("Refresh the package: source board or requirements changed")
    source, manifest, data = packages.contents(root, identifier)
    native = board.read(
        workspace.relative_file(root, next(b["path"] for b in candidate["boards"] if b["current"]))
    )
    if native.sha256 != candidate["board_sha256"]:
        raise ValueError("Source board changed")
    if candidate["kind"] == "prototype":
        bom = data[packages.assembly_path(manifest, data, "full_bom")]
        parts = availability.bom_parts(bom, include_through_hole)
        native_parts = {p.reference: p for p in assembly.collect(native)[0]}
        if any(
            native_parts.get(ref) is None
            or native_parts[ref].mpn != p["mpn"]
            or native_parts[ref].lcsc != p["lcsc"]
            for p in parts
            for ref in p["refs"]
        ):
            raise ValueError("BOM identity differs from source board")
        if not include_through_hole:
            parts = [p for p in parts if all(native_parts[ref].mount == "SMD" for ref in p["refs"])]
    else:
        if vendor != candidate["vendor"] or quantity != candidate["quantity"]:
            raise ValueError("Supplier and quantity are bound to the qualified package")
        columns = candidate["cpl"]["columns"]
        index = columns.index("Designator")
        placed = {row[index] for row in candidate["cpl"]["rows"]}
        parts = [
            {"refs": [p.reference], "mpn": p.mpn, "lcsc": p.lcsc, "per_board": 1}
            for p in assembly.collect(native)[0]
            if p.reference in placed
        ]
    contract = requirements.hashes(root)
    report = availability.check(parts, vendor, quantity)
    if (
        contract != requirements.hashes(root)
        or workspace.file_sha(
            workspace.relative_file(
                root, next(b["path"] for b in candidate["boards"] if b["current"])
            )
        )
        != native.sha256
    ):
        raise ValueError("Inputs changed during inventory check; refresh")
    report.update(
        source_artifact=identifier,
        source_sha256=source["sha256"],
        board_sha256=native.sha256,
        contract=contract,
        include_through_hole=include_through_hole,
    )
    path = (
        "reports/manufacturing/availability/" + workspace.sha(workspace.encoded(report)) + ".json"
    )
    workspace.atomic(root / path, workspace.encoded(report))
    item = workspace.publish(
        root,
        path,
        "report",
        f"{vendor}: assembly availability for {quantity} boards",
        metadata={
            "assembly_availability": True,
            "workspace_view": "manufacturing",
            "source_artifact": identifier,
        },
    )
    workspace.record(
        root,
        {
            "source": "assembly-availability",
            "artifact": item["id"],
            "vendor": vendor,
            "quantity": quantity,
            "all_available": report["all_available"],
        },
    )
    return {**report, "artifact": item["id"]}


def reports(project, identifier):
    root = Path(project).resolve()
    registry = workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})
    reports = {}
    for item in registry["artifacts"]:
        if (
            not item.get("metadata", {}).get("assembly_availability")
            or item["metadata"].get("source_artifact") != identifier
        ):
            continue
        data = workspace.read_json(workspace.relative_file(root, item["path"]), {})
        try:
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(data["retrieved"])
            ).total_seconds()
            current = 0 <= age <= 3600 and data["contract"] == requirements.hashes(root)
        except (ValueError, KeyError, TypeError):
            current = False
        key = (data["vendor"], data["quantity"], data["include_through_hole"])
        reports[key] = {**data, "artifact": item["id"], "fresh": current}
    return list(reports.values())
