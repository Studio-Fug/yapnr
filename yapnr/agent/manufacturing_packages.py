"""Discover published prototype exports and prepare local, explicitly unqualified quotes."""

import csv
import io
import json
import zipfile
from pathlib import Path

from yapnr.agent import requirements, reviews, workflow, workspace
from yapnr.fab import assembly, board, bundle, capability
from yapnr.order import vendors

PROTOTYPE_SCHEMAS = {"rgb-prototype-manufacturing-package-v1", "yapnr-prototype-manufacturing-v1"}


def contents(root, identifier):
    item = reviews.artifact(root, identifier)
    path = workspace.relative_file(root, item["path"])
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if (
                len(names) > 2500
                or len(set(names)) != len(names)
                or any(Path(n).is_absolute() or ".." in Path(n).parts or "\\" in n for n in names)
            ):
                raise ValueError("Invalid package archive members")
            if sum(i.file_size for i in archive.infolist()) > 256 * 1024**2:
                raise ValueError("Package exceeds the size budget")
            manifest = json.loads(archive.read("manifest.json"))
            schema = manifest.get("schema")
            if schema in PROTOTYPE_SCHEMAS:
                files = manifest["files"]
                if not isinstance(files, dict):
                    raise ValueError("Invalid prototype manifest files")
            elif schema == "yapnr-fab-manifest-v1":
                files = {f["name"]: f["sha256"] for f in manifest["files"]}
                files.update(
                    {"kicad/" + f["name"]: f["sha256"] for f in manifest.get("kicad_aux", [])}
                )
            else:
                raise ValueError("Unsupported package manifest")
            if set(names) != set(files) | {"manifest.json"}:
                raise ValueError("Archive does not match its manifest")
            data = {name: archive.read(name) for name in files}
            if any(workspace.sha(data[name]) != expected for name, expected in files.items()):
                raise ValueError("Package member hash changed")
            if schema in PROTOTYPE_SCHEMAS:
                cad = [n for n in files if n.startswith("cad/") and n.endswith(".kicad_pcb")]
                if len(cad) != 1 or workspace.sha(data[cad[0]]) != manifest["board_sha256"]:
                    raise ValueError("Packaged CAD differs from the source board binding")
            data["manifest.json"] = archive.read("manifest.json")
            return item, manifest, data
    except (zipfile.BadZipFile, KeyError, RuntimeError) as error:
        raise ValueError("Invalid prototype package archive") from error


def assembly_path(manifest, data, role):
    supplied = manifest.get("assembly", {}).get(role)
    if supplied:
        if supplied not in data:
            raise ValueError("Missing assembly file")
        return supplied
    prefix = "assembly/BOM-all-" if role == "full_bom" else "assembly/SMT-CPL-"
    names = [n for n in data if n.startswith(prefix) and n.endswith(".csv")]
    if len(names) != 1:
        raise ValueError("Declare the complete BOM and placement file in the manifest")
    return names[0]


def table(data):
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    if len(rows) > 1000:
        raise ValueError("Assembly table exceeds the row budget")
    return {"columns": rows[0], "rows": rows[1:]} if rows else {"columns": [], "rows": []}


def candidates(project):
    root = Path(project).resolve()
    registry = workspace.read_json(root / ".yapnr/workspace/artifacts.json", {"artifacts": []})
    output = []
    for item in registry["artifacts"]:
        source = item.get("source_path", "")
        if not source.endswith(".zip") or not (
            source.startswith("manufacturing/")
            or source.endswith("-bundle.zip")
            or item.get("metadata", {}).get("manufacturing_package")
        ):
            continue
        try:
            _, manifest, data = contents(root, item["id"])
        except (OSError, ValueError, TypeError) as error:
            if (
                "package" in Path(source).name
                or source.endswith("-bundle.zip")
                or item.get("metadata", {}).get("manufacturing_package")
            ):
                output.append(
                    {
                        "artifact": item["id"],
                        "title": item["title"],
                        "current": False,
                        "error": "Cannot prepare this package: " + str(error),
                    }
                )
            continue  # A Gerber-only zip is a download, not a complete assembly package.
        try:
            qualified = manifest["schema"] == "yapnr-fab-manifest-v1"
            sha = manifest["board"]["sha256"] if qualified else manifest["board_sha256"]
            boards = []
            for article in reversed(registry["artifacts"]):
                name = article.get("source_path", "")
                if (
                    article["kind"] == "board"
                    and article["sha256"] == sha
                    and not any(b["path"] == name for b in boards)
                ):
                    try:
                        current = workspace.file_sha(workspace.relative_file(root, name)) == sha
                    except (OSError, ValueError):
                        current = False
                    boards.append({"path": name, "artifact": article["id"], "current": current})
            if qualified:
                card = json.loads(data["order-card.json"])
                if card["vendor"]["id"] not in ("jlcpcb", "pcbway") or not card.get("assembly"):
                    continue
                settings = {
                    "width_mm": card["board"]["size_mm"][0],
                    "height_mm": card["board"]["size_mm"][1],
                }
                errors, opens = card["checks"]["drc_errors"], 0
                full_bom, placement = card["assembly"]["bom"], card["assembly"]["cpl"]
                requirement_current = (
                    True  # Native fab checks bind the board; review binds the current contract.
                )
                qualification = (
                    "Vendor fabrication checks are recorded; part matching, placement preview "
                    "and physical verification still need review."
                )
            else:
                settings = json.loads(data["settings.json"])
                drc = json.loads(data["checks/drc.json"])
                errors = sum(v.get("severity") == "error" for v in drc.get("violations", []))
                opens = len(drc.get("unconnected_items", []))
                full_bom, placement = assembly_path(manifest, data, "full_bom"), assembly_path(
                    manifest, data, "cpl"
                )
                requirement_current = manifest["requirements_sha256"] == workspace.sha(
                    workspace.encoded(requirements.hashes(root))
                )
                qualification = "Prototype exports: vendor DFM, component matching and rotations remain unqualified."
            output.append(
                {
                    "artifact": item["id"],
                    "title": item["title"],
                    "sha256": item["sha256"],
                    "kind": "qualified" if qualified else "prototype",
                    "vendor": card["vendor"]["id"] if qualified else None,
                    "quantity": card["qty"]["value"] if qualified else None,
                    "finish": card["options"]["finish"] if qualified else None,
                    "board_sha256": sha,
                    "boards": boards,
                    "settings": settings,
                    "current": requirement_current and any(b["current"] for b in boards),
                    "drc_errors": errors,
                    "unconnected": opens,
                    "availability": availability_reports(root, item["id"]),
                    "bom": table(data[full_bom]),
                    "cpl": table(data[placement]),
                    "notes": data["README.md"].decode(),
                    "qualification": qualification,
                    "previews": [
                        a["id"]
                        for a in registry["artifacts"]
                        if a["kind"] == "image"
                        and a.get("metadata", {}).get("experiment")
                        == item.get("metadata", {}).get("experiment")
                        and a.get("source_path", "").startswith("manufacturing/gerber-preview")
                    ],
                }
            )
        except (KeyError, ValueError, TypeError, UnicodeError):
            output.append(
                {
                    "artifact": item["id"],
                    "title": item["title"],
                    "current": False,
                    "error": "Package metadata is incomplete; regenerate its manifest and assembly files",
                }
            )
    return output


def prepare(project, identifier, vendor, quantity, finish, include_through_hole=False):
    root = Path(project).resolve()
    if (
        type(include_through_hole) is not bool
        or vendor not in ("jlcpcb", "pcbway")
        or type(quantity) is not int
        or not 1 <= quantity <= 10000
    ):
        raise ValueError("Choose a vendor and a quantity from 1 to 10000")
    candidate = next((c for c in candidates(root) if c["artifact"] == identifier), None)
    if not candidate or not candidate["current"]:
        raise ValueError("Package inputs changed or source board is unavailable")
    if candidate["drc_errors"] or candidate["unconnected"]:
        raise ValueError("Resolve packaged DRC errors and unconnected items before review")
    controller = workflow.query(root)
    if (
        not controller["contract_current"]
        or controller["accepted_revision"] != controller["revision"]
    ):
        raise ValueError("Accept the current requirements before preparing a handoff")
    source, manifest, data = contents(root, identifier)
    board_source = next(b for b in candidate["boards"] if b["current"])
    if candidate["kind"] == "qualified":
        if (vendor, quantity, finish) != (
            candidate["vendor"],
            candidate["quantity"],
            candidate["finish"],
        ):
            raise ValueError(
                "Order settings are bound to this qualified package; rebuild to change them"
            )
        from yapnr.agent import manufacturing

        directory = root / "reports/manufacturing/imported" / source["sha256"]
        directory.mkdir(parents=True, exist_ok=True)
        for name, payload in data.items():
            workspace.atomic(directory / name, payload)
        card = json.loads(data["order-card.json"])
        archive_name = f"{card['board']['name']}-{vendor}-bundle.zip"
        if Path(archive_name).name != archive_name or "\\" in archive_name:
            raise ValueError("Invalid bundle archive name")
        workspace.atomic(
            directory / archive_name, workspace.relative_file(root, source["path"]).read_bytes()
        )
        return manufacturing.prepare(
            root, str(directory.relative_to(root)), board_source["path"], source_artifact=identifier
        )
    if finish not in ("LeadFree HASL", "ENIG"):
        raise ValueError("Choose the requested surface finish")

    native = board.read(workspace.relative_file(root, board_source["path"]))
    if native.sha256 != manifest["board_sha256"]:
        raise ValueError("Source board changed")
    parts, skipped = assembly.collect(native)
    full_bom = list(
        csv.DictReader(
            io.StringIO(data[assembly_path(manifest, data, "full_bom")].decode("utf-8-sig"))
        )
    )
    identities = {row["Reference"]: row for row in full_bom}
    if len(identities) != len(full_bom) or set(identities) != {p.reference for p in parts}:
        raise ValueError("Complete BOM identities do not match the board")
    for part in parts:
        row = identities[part.reference]
        if row["MPN"] != part.mpn or row["LCSC"] != part.lcsc:
            raise ValueError("BOM identities differ from the source board")
        part.value = row["Value"]  # Retain the reviewed corrections, without changing copper.
        part.consigned = part.mount != "SMD" and not (include_through_hole and part.mount == "THT")
        if include_through_hole and part.mount == "THT" and not part.in_pos:
            raise ValueError(
                "Through-hole placement is excluded; prepare it before requesting turnkey assembly"
            )
    vendor_doc = capability.vendor(vendor)
    findings = assembly.findings(vendor_doc, parts, skipped)
    if any(f.severity == "error" for f in findings):
        raise ValueError("Resolve assembly part identity errors")
    rendered = assembly.outputs(vendor_doc, parts)
    directory = (
        root
        / "reports/manufacturing"
        / source["sha256"][:16]
        / f"{vendor}-{quantity}-{finish.replace(' ', '-')}-{'allparts' if include_through_hole else 'smt'}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "bom.csv": rendered["bom"].encode(),
        "cpl.csv": rendered["cpl"].encode(),
        "assembly-notes.md": data["README.md"],
        "full-bom.csv": data[assembly_path(manifest, data, "full_bom")],
        "drc.json": data["checks/drc.json"],
    }
    for name, payload in outputs.items():
        workspace.atomic(directory / name, payload)
    bundle.write_zip(
        directory / "gerbers.zip",
        [(Path(n).name, payload) for n, payload in data.items() if n.startswith("fabrication/")],
    )
    manual = [p.reference for p in parts if p.consigned]
    through_hole = [p.reference for p in parts if p.mount == "THT" and not p.consigned]
    card = {
        "vendor": {"id": vendor, "title": vendor_doc["title"]},
        "board": {
            "name": native.name,
            "size_mm": list(native.outline.size_mm or (0, 0)),
            "layers": native.copper_layers,
        },
        "qty": {"value": quantity},
        "profile": {"title": "Prototype exports · vendor DFM unqualified"},
        "stackup": {
            "summary": f"FR-4 baseline · {native.thickness_mm} mm · supplier confirmation pending"
        },
        "options": {"finish": finish},
        "checks": {
            "drc_errors": candidate["drc_errors"],
            "drc_warnings": sum(
                v.get("severity") == "warning"
                for v in json.loads(data["checks/drc.json"]).get("violations", [])
            ),
            "summary": {"error": 0, "warning": len(findings)},
            "notable": [f.message for f in findings],
        },
        "assembly": {
            "notes": [
                f"Separate through-hole/hand assembly: {', '.join(manual) or 'none'}.",
                f"Requested vendor through-hole assembly: {', '.join(through_hole) or 'none'}; "
                "confirm service and pricing in the quote.",
            ]
        },
    }
    request = {
        "schema": "yapnr-prototype-quote-packet-v1",
        "include_through_hole": include_through_hole,
        "source_artifact": identifier,
        "source_sha256": source["sha256"],
        "card": card,
        "qualification": candidate["qualification"],
    }
    workspace.atomic(directory / "quote-request.json", workspace.encoded(request))
    bundle.write_zip(
        directory / "quote-package.zip",
        [(name, directory / name) for name in [*outputs, "gerbers.zip", "quote-request.json"]],
    )
    labels = [
        ("Gerbers and drills", "gerbers.zip"),
        ("Vendor BOM", "bom.csv"),
        ("Placement / CPL", "cpl.csv"),
        ("All parts / separate THT", "full-bom.csv"),
        ("Assembly instructions", "assembly-notes.md"),
        ("Native DRC report", "drc.json"),
        ("Complete quote package", "quote-package.zip"),
    ]
    downloads = [
        {
            "label": "Original prototype package / CAD",
            "artifact": source["id"],
            "sha256": source["sha256"],
        }
    ]
    for label, name in labels:
        item = workspace.publish(
            root,
            str((directory / name).relative_to(root)),
            "report",
            label,
            metadata={"manufacturing_download": True, "source_artifact": identifier},
        )
        downloads.append({"label": label, "artifact": item["id"], "sha256": item["sha256"]})
    from yapnr.agent import vendor_uploads

    upload_archive = vendor_uploads.prepare(root, downloads, card)
    if upload_archive:
        downloads.append(upload_archive)
    release = {
        "schema": "yapnr-assembly-handoff-v1",
        "prototype": True,
        "source_artifact": identifier,
        "source_sha256": source["sha256"],
        "card": card,
        "board": {"path": board_source["path"], "sha256": native.sha256},
        "contract": requirements.hashes(root),
        "accepted_revision": controller["accepted_revision"],
        "downloads": downloads,
        "vendor_page": vendors.manual_page(vendor, assembly=True)["url"],
        "previews": candidate["previews"],
        "bom": table(outputs["bom.csv"]),
        "cpl": table(outputs["cpl.csv"]),
        "qualification": candidate["qualification"],
        "open_verification": (
            "Vendor DFM/rotation and physical functional validation remain open. "
            "Approval covers prototype file handoff for quoting only; "
            "it is not design qualification or order authorization."
        ),
    }
    path = directory / "handoff.json"
    workspace.atomic(path, workspace.encoded(release))
    item = workspace.publish(
        root,
        str(path.relative_to(root)),
        "report",
        vendor_doc["title"] + " prototype quote handoff",
        metadata={"assembly_handoff": True, "workspace_view": "manufacturing"},
    )
    reviews.request(root, [item["id"]], stage="assembly_handoff", revision=controller["revision"])
    workspace.record(
        root,
        {
            "source": "prototype-quote-prepared",
            "include_through_hole": include_through_hole,
            "artifact": item["id"],
            "source_artifact": identifier,
            "vendor": vendor,
            "quantity": quantity,
            "finish": finish,
        },
    )
    return item


def availability_reports(root, identifier):
    from yapnr.agent import assembly_availability

    return assembly_availability.reports(root, identifier)
