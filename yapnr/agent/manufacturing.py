"""Content-bound turnkey assembly handoff; no vendor requests or order submission."""

import json
import zipfile
from pathlib import Path

from yapnr.agent import requirements, reviews, workflow, workspace
from yapnr.order import stage, vendors


def prepare(project, bundle, board):
    root = Path(project).resolve()
    directory = root / bundle
    manifest_file = workspace.relative_file(root, str(Path(bundle) / "manifest.json"))
    source = workspace.relative_file(root, board)
    manifest = json.loads(manifest_file.read_bytes())
    for entry in manifest.get("files", []):
        workspace.relative_file(root, str(Path(bundle) / entry["name"]))
    try:
        card, manifest = stage.load_bundle(directory)
    except stage.StageError as error:
        raise ValueError(str(error)) from error
    vendor = card["vendor"]["id"]
    if vendor not in ("jlcpcb", "pcbway") or not card.get("assembly"):
        raise ValueError("Build a JLCPCB or PCBWay assembly bundle first")
    if workspace.file_sha(source) != manifest["board"]["sha256"]:
        raise ValueError("Assembly bundle belongs to a different board revision")
    required_files = {
        "order-card.json",
        "order-card.md",
        "README.md",
        manifest["gerber_zip"]["name"],
        card["assembly"]["bom"],
        card["assembly"]["cpl"],
    }
    if not required_files.issubset({entry["name"] for entry in manifest["files"]}):
        raise ValueError("Assembly downloads must be bound to the checked manifest")
    checks = card["checks"]
    if checks["summary"]["error"] or checks["drc_errors"]:
        raise ValueError("Resolve fabrication and native DRC errors before package review")
    controller = workflow.query(root)
    if (
        not controller["contract_current"]
        or controller["accepted_revision"] != controller["revision"]
    ):
        raise ValueError("Review and accept the current requirements before assembly handoff")
    archive_name = f"{card['board']['name']}-{vendor}-bundle.zip"
    archive = workspace.relative_file(root, str(Path(bundle) / archive_name))
    # The complete bundle zip is outside manifest.files; verify its contents independently.
    try:
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            if len(names) != len(set(names)) or any(
                Path(n).is_absolute() or ".." in Path(n).parts for n in names
            ):
                raise ValueError("Invalid assembly archive members")
            expected = {entry["name"] for entry in manifest["files"]} | {"manifest.json"}
            expected |= {"kicad/" + entry["name"] for entry in manifest.get("kicad_aux", [])}
            if (
                set(names) != expected
                or sum(info.file_size for info in zipped.infolist()) > 256 * 1024**2
            ):
                raise ValueError("Unexpected or oversized assembly archive members")
            for entry in manifest.get("kicad_aux", []):
                if workspace.sha(zipped.read("kicad/" + entry["name"])) != entry["sha256"]:
                    raise ValueError("Assembly archive native inputs changed")
            for entry in manifest["files"]:
                if workspace.sha(zipped.read(entry["name"])) != entry["sha256"]:
                    raise ValueError("Assembly archive does not match the checked bundle")
            if zipped.read("manifest.json") != manifest_file.read_bytes():
                raise ValueError("Assembly archive manifest changed")
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("Invalid assembly archive") from error
    downloads = []
    files = [
        ("Complete assembly package", archive_name),
        ("Gerbers and drills", manifest["gerber_zip"]["name"]),
        ("BOM", card["assembly"]["bom"]),
        ("Placement / CPL", card["assembly"]["cpl"]),
        ("Assembly instructions", "README.md"),
        ("Order card", "order-card.md"),
    ]
    for label, name in files:
        path = str(Path(bundle) / name)
        item = workspace.publish(root, path, "report", f"{vendor}: {label}")
        downloads.append({"label": label, "artifact": item["id"], "sha256": item["sha256"]})
    release = {
        "schema": "yapnr-assembly-handoff-v1",
        "card": card,
        "board": {"path": board, "sha256": workspace.file_sha(source)},
        "contract": requirements.hashes(root),
        "accepted_revision": controller["accepted_revision"],
        "downloads": downloads,
        "vendor_page": vendors.manual_page(vendor, assembly=True)["url"],
        "purpose": "Package review and manual vendor handoff; no upload, order or payment",
        "open_verification": "Physical and other outstanding requirement checks remain open.",
    }
    path = "reports/manufacturing/" + vendor + "-handoff.json"
    workspace.atomic(root / path, workspace.encoded(release))
    item = workspace.publish(
        root,
        path,
        "report",
        f'{card["vendor"]["title"]} turnkey assembly handoff',
        metadata={"assembly_handoff": True, "workspace_view": "manufacturing"},
    )
    reviews.request(root, [item["id"]], stage="assembly_handoff", revision=controller["revision"])
    workspace.record(
        root,
        {
            "source": "assembly-handoff-prepared",
            "artifact": item["id"],
            "board_sha256": release["board"]["sha256"],
            "vendor": vendor,
        },
    )
    return item


def state(project):
    root = Path(project).resolve()
    registry = workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})
    review = reviews.state(root)["items"]
    releases = []
    for item in registry["artifacts"]:
        if not item.get("metadata", {}).get("assembly_handoff"):
            continue
        record = review.get(item["id"], {})
        if record.get("status") == "superseded":
            continue
        published = reviews.artifact(root, item["id"])
        data = json.loads(workspace.relative_file(root, published["path"]).read_bytes())
        current = False
        try:
            controller = workflow.query(root)
            current = (
                controller["contract_current"]
                and controller["accepted_revision"]
                == controller["revision"]
                == data["accepted_revision"]
                and workspace.file_sha(workspace.relative_file(root, data["board"]["path"]))
                == data["board"]["sha256"]
                and requirements.hashes(root) == data["contract"]
            )
            for download in data["downloads"]:
                artifact = reviews.artifact(root, download["artifact"])
                current = current and artifact["sha256"] == download["sha256"]
        except (OSError, ValueError):
            current = False
        releases.append(
            {
                **data,
                "artifact": item["id"],
                "current": current,
                "review": record.get("status", "pending_review"),
                "feedback": record.get("unresolved", []),
                "handoff_ready": current and record.get("status") == "approved",
            }
        )
    return {"releases": releases}
