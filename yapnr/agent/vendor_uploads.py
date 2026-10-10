"""Human-clicked file staging through PCBWay's official KiCad plugin handoff.

Protocol source: https://github.com/pcbway/PCBWay-Plug-in-for-Kicad
No account cookies, checkout or payment operations are used.
"""

import csv
import fcntl
import io
import json
import secrets
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from yapnr.agent import reviews, workspace
from yapnr.fab import assembly, bundle

ENDPOINT = "https://www.pcbway.com/Common/KiCadUpFile/"


def payload(root, downloads):
    def read(label):
        entry = next(d for d in downloads if d["label"] in label)
        item = reviews.artifact(root, entry["artifact"])
        if item["sha256"] != entry["sha256"]:
            raise ValueError("Manufacturing download changed")
        return workspace.relative_file(root, item["path"]).read_bytes()

    bom = list(csv.DictReader(io.StringIO(read(("BOM", "Vendor BOM")).decode("utf-8-sig"))))
    cpl = list(csv.DictReader(io.StringIO(read(("Placement / CPL",)).decode("utf-8-sig"))))
    identities = {}
    for row in bom:
        for ref in row["Designator"].split(","):
            ref = ref.strip()
            if ref in identities:
                raise ValueError("Duplicate assembly identity")
            identities[ref] = row
    columns = [
        "Designator",
        "Quantity",
        "Value",
        "Footprint",
        "Package",
        "MPN",
        "Manufacturer",
        "Mount_Type",
    ]
    rows = [
        [
            r["Designator"],
            r["Qty"],
            r["Description / Value"],
            r["Footprint"],
            r["Footprint"],
            r["Mfg Part #"],
            r["Manufacturer"],
            "smt" if r["Type"] == "SMD" else "tht",
        ]
        for r in bom
    ]
    positions = []
    for row in cpl:
        identity = identities[row["Designator"]]
        if row["Layer"] not in ("Top", "Bottom"):
            raise ValueError("Invalid placement side")
        positions.append(
            [
                row["Mid X"],
                row["Mid Y"],
                row["Rotation"],
                row["Layer"].lower(),
                row["Designator"],
                identity["Mfg Part #"],
                identity["Footprint"],
                identity["Footprint"],
                identity["Description / Value"],
                "smt" if identity["Type"] == "SMD" else "tht",
            ]
        )
    members = []
    with zipfile.ZipFile(io.BytesIO(read(("Gerbers and drills",)))) as archive:
        names = archive.namelist()
        if (
            len(names) > 2500
            or len(set(names)) != len(names)
            or sum(m.file_size for m in archive.infolist()) > 256 * 1024**2
            or any(Path(n).name != n or "\\" in n for n in names)
        ):
            raise ValueError("Invalid Gerber archive")
        members = [(n, archive.read(n)) for n in names]
    if not members or {n for n, _ in members} & {"PCBWay_bom.csv", "PCBWay_positions.csv"}:
        raise ValueError("Invalid manufacturing archive members")
    members.extend(
        [
            ("PCBWay_bom.csv", assembly.csv_text(columns, rows).encode("utf-8-sig")),
            (
                "PCBWay_positions.csv",
                assembly.csv_text(
                    [
                        "pos_x",
                        "pos_y",
                        "rotation",
                        "side",
                        "designator",
                        "mpn",
                        "pack",
                        "footprint",
                        "value",
                        "mount_type",
                    ],
                    positions,
                ).encode("utf-8-sig"),
            ),
        ]
    )
    return members


def prepare(root, downloads, card):
    if card["vendor"]["id"] != "pcbway":
        return None
    try:
        members = payload(root, downloads)
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeError) as error:
        raise ValueError(
            "Cannot prepare the PCBWay upload archive; check its Gerber ZIP, BOM and CPL"
        ) from error
    digest = workspace.sha(workspace.encoded([(n, workspace.sha(data)) for n, data in members]))
    name = f"reports/manufacturing/pcbway-upload/{digest}.zip"
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_zip(path, members)
    item = workspace.publish(root, name, "report", "PCBWay upload package")
    return {"label": "PCBWay upload package", "artifact": item["id"], "sha256": item["sha256"]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(data, card):
    boundary = "yapnr-" + secrets.token_hex(16)
    width, height = card["board"]["size_mm"]
    fields = {
        "boardWidth": width,
        "boardHeight": height,
        "boardLayer": len(card["board"]["layers"]),
    }
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.extend(
        [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="upload[file]"; '
                'filename="yapnr-pcbway.zip"\r\nContent-Type: application/zip\r\n\r\n'
            ).encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        ENDPOINT,
        data=b"".join(chunks),
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "yapnr-manufacturing",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ValueError("Unexpected vendor upload response")
    return json.loads(body)


def redirect_url(value):
    url = value.get("redirect") if isinstance(value, dict) else None
    if not isinstance(url, str):
        raise ValueError("Vendor did not return a quote link")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ("pcbway.com", "www.pcbway.com", "member.pcbway.com")
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("Vendor returned an untrusted quote link")
    return url


def upload(project, identifier):
    root = Path(project).resolve()
    folder = root / ".yapnr/workspace"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "vendor-upload.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("A vendor upload is already in progress") from error
        return _upload(root, identifier)


def _upload(project, identifier):
    from yapnr.agent import manufacturing

    root = Path(project).resolve()
    release = next(
        (r for r in manufacturing.state(root)["releases"] if r["artifact"] == identifier), None
    )
    if not release or not release["handoff_ready"]:
        raise ValueError("Approve the current manufacturing package before uploading")
    if release["card"]["vendor"]["id"] != "pcbway":
        raise ValueError(
            "Automatic JLCPCB upload requires approved API access; use the manual quote link"
        )
    download = next(
        (d for d in release["downloads"] if d["label"] == "PCBWay upload package"), None
    )
    if download is None:
        raise ValueError("Prepare a new supplier packet to include its reviewed upload archive")
    item = reviews.artifact(root, download["artifact"])
    if item["sha256"] != download["sha256"]:
        raise ValueError("Upload archive changed")
    path = root / f"reports/manufacturing/pcbway-upload/{item['sha256']}-receipt.json"
    previous = workspace.read_json(path, {})
    if previous.get("handoff_artifact") == identifier and previous.get("status") == "uploaded":
        return {**previous, "redirect": redirect_url(previous)}
    workspace.record(
        root,
        {
            "source": "vendor-upload-started",
            "vendor": "pcbway",
            "artifact": identifier,
            "upload_artifact": item["id"],
            "sha256": item["sha256"],
        },
    )
    try:
        result = post(workspace.relative_file(root, item["path"]).read_bytes(), release["card"])
        url = redirect_url(result)
    except (OSError, ValueError, urllib.error.URLError) as error:
        workspace.record(root, {"source": "vendor-upload-unconfirmed", "artifact": identifier})
        raise ValueError(
            "PCBWay upload was not confirmed. No automatic retry; use downloads or retry explicitly."
        ) from error
    result = {
        "schema": "yapnr-vendor-upload-v1",
        "status": "uploaded",
        "vendor": "pcbway",
        "redirect": url,
        "handoff_artifact": identifier,
        "upload_artifact": item["id"],
        "sha256": item["sha256"],
    }
    workspace.atomic(path, workspace.encoded(result))
    receipt = workspace.publish(
        root, str(path.relative_to(root)), "report", "PCBWay upload receipt"
    )
    workspace.record(
        root,
        {"source": "vendor-upload-confirmed", "artifact": identifier, "receipt": receipt["id"]},
    )
    return result
