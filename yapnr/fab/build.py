"""``yapnr fab check`` and ``yapnr fab build``: the pipeline from a routed board to a vendor bundle.

Design §6.2. ``check_board`` copies the board to ``<out>/.work/<board>-<profile>/``, writes the
profile's rules into the copy, runs KiCad's DRC with a zone refill (saving the refilled copy) and
``kicad-cli pcb export stats``, and evaluates every ``FAB-*`` check. ``build`` stops on any error;
otherwise it exports the vendor's files from that refilled copy (so the files are exactly what
DRC judged), writes the assembly files, the README, the order card, the manifest and the zips into
``<out>/<board>-<profile>/``.

Each KiCad step is cached under ``.work`` by the sha256 of its inputs (board, project, rules,
profile/stackup/vendor data, kicad-cli and yapnr versions): re-running with an unchanged board
runs no KiCad at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from yapnr import __version__
from yapnr.fab import assembly as assembly_mod
from yapnr.fab import board as board_mod
from yapnr.fab import bundle, capability, check, export, kicad, profiles, stackups

SQ_IN_MM2 = 645.16


@dataclass
class Request:
    board: Path
    vendor: str
    profile: Optional[str] = None
    stackup: Optional[str] = None
    qty: Optional[int] = None
    service: Optional[str] = None
    assembly: bool = False
    parts_lock: Optional[Path] = None
    consign: Sequence[str] = ()
    out: Path = Path("yapnr-fab")
    name: Optional[str] = None
    source_date_epoch: Optional[int] = None
    public: bool = False
    allow_draft: bool = False
    no_project: bool = False
    kicad_cli: Optional[str] = None
    timeout: float = kicad.DEFAULT_TIMEOUT
    argv: Sequence[str] = ()


@dataclass
class Checked:
    request: Request
    name: str
    vendor: Dict[str, Any]
    profile: profiles.Profile
    stackup: stackups.Stackup
    facts: board_mod.Board
    stats: Dict[str, Any]
    drc: Dict[str, Any]
    findings: List[check.Finding]
    workdir: Path
    scratch: Path
    cli: kicad.Cli
    parts: List[assembly_mod.Part] = field(default_factory=list)
    skipped: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not check.errors(self.findings)


@dataclass
class Built:
    checked: Checked
    bundle_dir: Path
    gerber_zip: Optional[Path]
    card: Optional[Dict[str, Any]]
    manifest: Optional[Dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.gerber_zip is not None


class BuildStopped(RuntimeError):
    """The fab check found errors; ``findings`` says which."""

    def __init__(self, checked: Checked, bundle_dir: Path):
        self.checked = checked
        self.bundle_dir = bundle_dir
        errs = check.errors(checked.findings)
        super().__init__(
            f"fab check: {len(errs)} errors under {checked.profile.name}; nothing was built"
        )


def _epoch(request: Request) -> Optional[int]:
    if request.source_date_epoch is not None:
        return int(request.source_date_epoch)
    value = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    return int(value) if value.isdigit() else None


def _hash_inputs(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, Path):
            h.update(part.name.encode() + b"\0")
            h.update(part.read_bytes() if part.is_file() else b"<missing>")
        else:
            h.update(json.dumps(part, sort_keys=True, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()


def default_profile(vendor: str, facts: board_mod.Board) -> str:
    """The profile for a board when none is named (design §6.1).

    The profile the board was routed under, when its generated ``.kicad_dru`` names one of this
    vendor's profiles for its layer count; else the vendor's profile for the layer count, the
    via-in-pad one (JLCPCB ``jlc-pofv``) when a via sits on an SMD pad.
    """
    routed = facts.rules_profile
    if routed and routed in capability.names("profiles"):
        doc = capability.profile(routed)
        if doc["vendor"] == vendor and doc["copper_layers"] == facts.layer_count:
            return routed
    return profiles.for_vendor(vendor, facts.layer_count, bool(facts.vias_in_smd_pads()))


def resolve(request: Request):
    """(vendor doc, board facts, profile, stackup, board name) for a request."""
    vendor = capability.vendor(request.vendor)
    board_path = Path(request.board).resolve()
    if not board_path.is_file():
        raise FileNotFoundError(f"no board {request.board}")
    facts = board_mod.read(board_path)
    name = request.profile or default_profile(request.vendor, facts)
    profile = profiles.load(name)
    if profile.vendor != request.vendor:
        raise profiles.ProfileError(
            f"{profile.name} is a {profile.vendor} profile, not {request.vendor}'s"
        )
    stackup = stackups.load(profile.stackup_id(request.stackup))
    board_name = export.safe_name(request.name or board_path.stem)
    return vendor, facts, profile, stackup, board_name


def check_board(request: Request) -> Checked:
    """Prepare the scratch copy, run DRC and stats (cached), evaluate every check."""
    vendor, facts, profile, stackup, name = resolve(request)
    cli = kicad.Cli(kicad.find_cli(request.kicad_cli), request.timeout)
    workdir = Path(request.out).resolve() / ".work" / f"{name}-{profile.name}"
    workdir.mkdir(parents=True, exist_ok=True)
    board_path = Path(request.board).resolve()
    key = _hash_inputs(
        board_path,
        board_path.with_suffix(".kicad_pro"),
        board_path.with_suffix(".kicad_dru"),
        capability.file_path("profiles", profile.name),
        profile.dru_text(),
        profile.board_constraints(),
        cli.version(),
        __version__,
        request.no_project,
    )
    stamp = workdir / "check.key"
    scratch = workdir / f"{name}.kicad_pcb"
    routed_under = None
    cached = (
        stamp.is_file()
        and stamp.read_text().strip() == key
        and all((workdir / f).is_file() for f in ("drc.json", "stats.json", "prepare.json"))
        and scratch.is_file()
    )
    if cached:
        drc = json.loads((workdir / "drc.json").read_text())
        stats = json.loads((workdir / "stats.json").read_text())
        routed_under = json.loads((workdir / "prepare.json").read_text()).get("routed_under")
    else:
        if stamp.exists():
            stamp.unlink()
        prep = check.prepare(board_path, profile, workdir, name, request.no_project)
        routed_under = prep["routed_under"]
        drc = cli.drc(scratch, workdir / "drc.json", refill=True, save=True)
        stats = cli.stats(scratch, workdir / "stats.json")
        (workdir / "prepare.json").write_text(
            json.dumps({"routed_under": routed_under, "notes": prep["notes"]}, indent=2) + "\n"
        )
        stamp.write_text(key + "\n")
    # The facts of the refilled copy (the board that is exported).
    facts = board_mod.read(scratch, name)
    facts.input_id = board_mod.read(board_path).input_id
    extra: List[check.Finding] = []
    parts: List[assembly_mod.Part] = []
    skipped: Dict[str, List[str]] = {}
    if request.assembly:
        if not vendor.get("assembly"):
            extra.append(
                check.Finding(
                    "FAB-ASSEMBLY",
                    "error",
                    f"{vendor['title']} offers no assembly; build bare boards (--no-assembly)",
                )
            )
        else:
            parts, skipped = assembly_mod.collect(facts, request.consign)
            lock = None
            if request.parts_lock:
                doc = json.loads(Path(request.parts_lock).read_text(encoding="utf-8"))
                lock = [p.get("lcsc", "") for p in doc.get("parts", []) if p.get("lcsc")]
            extra += assembly_mod.findings(vendor, parts, skipped, lock)
    options = check.Options(
        qty=request.qty,
        assembly=request.assembly,
        allow_draft=request.allow_draft,
        stackup_explicit=bool(request.stackup),
    )
    findings = check.evaluate(
        facts, stats, drc, profile, stackup, vendor, options, extra, routed_under
    )
    return Checked(
        request,
        name,
        vendor,
        profile,
        stackup,
        facts,
        stats,
        drc,
        findings,
        workdir,
        scratch,
        cli,
        parts,
        skipped,
    )


# ----------------------------------------------------------------------------------- card


def _service(profile: profiles.Profile, requested: Optional[str]) -> Dict[str, Any]:
    services = profile.defaults["services"]
    sid = requested or profile.defaults["service"]
    if sid not in services:
        raise profiles.ProfileError(
            f"{profile.name} has no service {sid!r}; services: {', '.join(services)}"
        )
    return dict(services[sid], id=sid)


def estimate(profile: profiles.Profile, area_sq_in: float, qty: int, service: str):
    """OSH Park's public formula (price per square inch per set of 3); None elsewhere."""
    pricing = profile.pricing
    if not pricing or pricing.get("kind") != "per_sq_in":
        return None
    rate = (pricing.get("per_sq_in") or {}).get(service)
    if rate is None:
        return None
    sets = max(1, qty // pricing["boards_per_set"])
    src = profile.doc["sources"].get(capability.source_key(pricing["src"], profile.doc["sources"]))
    return {
        "usd": round(area_sq_in * rate * sets, 2),
        "formula": f"{area_sq_in:.3f} sq in x ${rate:g} per set of {pricing['boards_per_set']}"
        + (f" x {sets} sets" if sets > 1 else ""),
        "source": f"{src['url']} {src['accessed']}" if src else pricing["src"],
    }


def _impedance(checked: Checked) -> str:
    control = checked.profile.limits.get("impedance_control") or {}
    title = checked.vendor["title"]
    relevant = any(f.code == "FAB-IMPEDANCE" for f in checked.findings)
    if not control.get("value"):
        return f"not controlled ({title} has no impedance service); nominal stackup only"
    tol = control.get("tolerance_pct")
    if relevant:
        return f"order impedance control on {checked.stackup.vendor_name} (+-{tol} %)"
    return f"not requested (no impedance-relevant items); {title} offers +-{tol} %"


def make_card(
    checked: Checked,
    upload: Dict[str, Any],
    assembly_files: Optional[Dict[str, Any]] = None,
    service: Optional[str] = None,
    qty: Optional[int] = None,
) -> Dict[str, Any]:
    p, s, v, facts = checked.profile, checked.stackup, checked.vendor, checked.facts
    size = facts.outline.size_mm or (0.0, 0.0)
    area = size[0] * size[1] / SQ_IN_MM2
    svc = _service(p, service)
    qty_value = qty if qty is not None else (checked.request.qty or p.defaults.get("qty"))
    rule = p.limits.get("qty") or {}
    rule_text = (
        f"multiples of {rule['multiple']}"
        if rule.get("multiple", 1) > 1
        else f"{rule.get('min')} or more"
    )
    d = p.defaults
    options = {
        "thickness_mm": d.get("thickness_mm") or s.thickness_mm,
        "finish": d.get("finish"),
        "mask_color": d.get("mask_color"),
        "silk_color": d.get("silk_color"),
        "via_covering": d.get("via_covering"),
        "copper": d.get("copper"),
        "fixed": bool((p.limits.get("finish") or {}).get("fixed")),
        "remark": d.get("remark") or "",
    }
    if v["vendor"] == "jlcpcb":
        options["specify_stackup"] = s.vendor_name
        if d.get("via_option"):
            options["via_option"] = d["via_option"]
    if facts.rf_footprints() and v["vendor"] == "pcbway":
        options["remark"] = (
            options["remark"] + " "
        ).lstrip() + "No copper thieving or silkscreen on the RF structures on L1."
    summary = check.summary(checked.findings)
    drc_errors = sum(
        f.data.get("count", 1)
        for f in checked.findings
        if f.code == "FAB-DRC" and f.severity == "error"
    )
    drc_warnings = sum(
        f.data.get("count", 1)
        for f in checked.findings
        if f.code == "FAB-DRC" and f.severity == "warning"
    )
    notable = [
        f"{f.code} ({f.severity}): {f.message}"
        for f in checked.findings
        if f.code != "FAB-DRC"
        and (f.severity != "info" or f.code in ("FAB-ALTERNATE", "FAB-MARKING"))
    ]
    staging = v["staging"]["manual"]
    if checked.request.assembly and "assembly" in v["staging"]:
        staging = v["staging"]["assembly"]
    warnings = []
    if p.draft:
        warnings.append(f"{p.name} is a draft profile: its rules are not fully transcribed")
    from yapnr.order import vendors

    card = {
        "schema": "yapnr-order-card-v1",
        "vendor": {"id": v["vendor"], "title": v["title"], "country": v["country"]},
        "service": svc,
        "profile": {"name": p.name, "status": p.status, "title": p.doc["title"]},
        "board": {
            "name": checked.name,
            "size_mm": [round(size[0], 3), round(size[1], 3)],
            "area_mm2": round(size[0] * size[1], 3),
            "area_sq_in": round(area, 4),
            "input_id": facts.input_id,
            "layers": facts.copper_layers,
        },
        "stackup": {
            "id": s.id,
            "vendor_name": s.vendor_name,
            "summary": s.summary(),
            "checkout": s.checkout,
        },
        "options": options,
        "impedance": _impedance(checked),
        "qty": {"value": qty_value, "rule": rule_text},
        "estimate": estimate(p, area, qty_value or 0, svc["id"]),
        "checks": {
            "summary": summary,
            "drc_errors": drc_errors,
            "drc_warnings": drc_warnings,
            "notable": notable,
        },
        "files": {"upload": upload},
        "assembly": assembly_files,
        "staging": {
            "mechanism": "manual",
            "url": staging["url"],
            "steps": vendors.steps(staging, upload["name"], bool(assembly_files)),
        },
        "warnings": warnings,
    }
    return card


# ----------------------------------------------------------------------------------- build


def _source_revision() -> Optional[str]:
    value = os.environ.get("YAPNR_SOURCE_REVISION", "").strip()
    if value:
        return value
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "yapnr", "hardware/pnr/pnr"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        return out + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def _rf_rows(checked: Checked) -> List[Dict[str, Any]]:
    rows = []
    for fp in checked.facts.rf_footprints():
        rf = fp.rf
        layer = fp.layer if fp.layer in ("F.Cu", "B.Cu") else "F.Cu"
        try:
            m = checked.stackup.microstrip(layer)
            w50 = f"{m.w50_mm(use_prior=True):.3f}"
            er, h = m.er, m.h_mm
        except stackups.StackupError:
            w50, er, h = "n/a", None, 0.0
        rows.append(
            {
                "reference": fp.reference,
                "name": rf["name"],
                "layer": layer,
                "assumed_er": rf["er"],
                "assumed_h_mm": rf["h_mm"],
                "er": er,
                "h_mm": h,
                "w50_mm": w50,
            }
        )
    return rows


def build(request: Request) -> Built:
    """Check, then (with no errors) write the bundle. Raises BuildStopped on fab-check errors."""
    checked = check_board(request)
    out = Path(request.out).resolve()
    bundle_dir = out / f"{checked.name}-{checked.profile.name}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    findings = [f.to_dict() for f in checked.findings]
    drc_path = bundle_dir / "drc.json"
    drc_path.write_text(json.dumps(checked.drc, indent=2, sort_keys=True) + "\n")
    _normalize_drc(drc_path, export.stamp_time(_epoch(request)))
    check_doc = {
        "schema": "yapnr-fab-check-v1",
        "board": checked.name,
        "profile": checked.profile.name,
        "stackup": checked.stackup.id,
        "vendor": checked.vendor["vendor"],
        "summary": check.summary(checked.findings),
        "findings": findings,
    }
    (bundle_dir / "fab-check.json").write_text(json.dumps(check_doc, indent=2) + "\n")
    if not checked.ok:
        raise BuildStopped(checked, bundle_dir)

    when = export.stamp_time(_epoch(request))
    export_key = _hash_inputs(
        (checked.workdir / "check.key").read_text(),
        capability.file_path("vendors", checked.vendor["vendor"]),
        when.isoformat(),
    )
    stamp = checked.workdir / "export.key"
    listing = checked.workdir / "export.json"
    if stamp.is_file() and stamp.read_text().strip() == export_key and listing.is_file():
        entries = json.loads(listing.read_text())
        fab = [
            export.FabFile(e["name"], Path(e["path"]), e["kind"], e.get("layer"), e.get("function"))
            for e in entries["fab"]
        ]
        aux = [export.FabFile(e["name"], Path(e["path"]), e["kind"]) for e in entries["aux"]]
    else:
        fab, aux = export.export_fab_files(
            checked.cli,
            checked.scratch,
            checked.name,
            checked.vendor,
            checked.facts.copper_layers,
            checked.workdir,
            when,
        )
        listing.write_text(
            json.dumps(
                {
                    "fab": [
                        dict(
                            name=f.name,
                            path=str(f.path),
                            kind=f.kind,
                            layer=f.layer,
                            function=f.function,
                        )
                        for f in fab
                    ],
                    "aux": [dict(name=f.name, path=str(f.path), kind=f.kind) for f in aux],
                },
                indent=2,
            )
        )
        stamp.write_text(export_key + "\n")

    vendor_id = checked.vendor["vendor"]
    zip_name = f"{checked.name}-{vendor_id}-gerbers.zip"
    gerber_zip = bundle_dir / zip_name
    zinfo = bundle.write_zip(gerber_zip, [(f.name, f.path) for f in fab])
    upload = {
        "name": zip_name,
        "path": f"{bundle_dir.name}/{zip_name}",
        "sha256": zinfo["sha256"],
        "bytes": zinfo["bytes"],
        "members": len(zinfo["members"]),
    }
    assembly_files = None
    extra_files = []
    if request.assembly:
        texts = assembly_mod.outputs(checked.vendor, checked.parts)
        spec = checked.vendor["assembly"]
        bom_name = spec["bom"]["file"].format(board=checked.name)
        cpl_name = spec["cpl"]["file"].format(board=checked.name)
        (bundle_dir / bom_name).write_text(texts["bom"])
        (bundle_dir / cpl_name).write_text(texts["cpl"])
        extra_files += [bom_name, cpl_name]
        notes = [
            f"{f.message}"
            for f in checked.findings
            if f.code == "FAB-ASSEMBLY" and f.severity != "info"
        ]
        assembly_files = {
            "bom": bom_name,
            "cpl": cpl_name,
            "parts": sum(1 for p in checked.parts if not p.consigned),
            "lines": max(0, texts["bom"].count("\n") - 1),
            "notes": notes,
            "stock_and_price": "checked on the vendor's BOM page (not known offline)",
        }
    card = make_card(checked, upload, assembly_files, request.service, request.qty)
    from yapnr.order import card as card_mod

    (bundle_dir / "order-card.json").write_text(card_mod.to_json(card))
    (bundle_dir / "order-card.md").write_text(card_mod.markdown(card))
    sources: Dict[str, Any] = {}
    for doc in (checked.profile.doc, checked.stackup.doc, checked.vendor):
        sources.update(doc["sources"])
    readme = bundle.readme(
        {
            "board": card["board"],
            "stackup": checked.stackup,
            "card": card,
            "profile": checked.profile.name,
            "yapnr": __version__,
            "kicad": checked.cli.version(),
            "findings": findings,
            "zip_members": zinfo["members"],
            "extra_files": extra_files,
            "sources": sources,
            "rf": _rf_rows(checked),
        }
    )
    (bundle_dir / "README.md").write_text(readme)

    manifest = _manifest(checked, request, bundle_dir, zinfo, when, aux)
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if request.public:
        texts_ = [p for p in bundle_dir.iterdir() if p.is_file() and p.suffix != ".zip"]
        texts_ += [f.path for f in fab]
        leaks = bundle.privacy_findings(texts_)
        if leaks:
            checked.findings.append(
                check.Finding(
                    "FAB-PRIVACY",
                    "error",
                    "private data in the bundle: "
                    + "; ".join(f"{name}: {kind} {m!r}" for name, kind, m in leaks[:8]),
                )
            )
            gerber_zip.unlink()
            raise BuildStopped(checked, bundle_dir)
    members = [
        (p.name, p)
        for p in sorted(bundle_dir.iterdir())
        if p.is_file() and not p.name.endswith("-bundle.zip")
    ]
    members += [(f"kicad/{f.name}", f.path) for f in aux]
    bundle.write_zip(bundle_dir / f"{checked.name}-{vendor_id}-bundle.zip", members)
    return Built(checked, bundle_dir, gerber_zip, card, manifest)


def _normalize_drc(path: Path, when) -> None:
    doc = json.loads(path.read_text())
    if "date" in doc:
        doc["date"] = when.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _manifest(checked, request, bundle_dir, zinfo, when, aux) -> Dict[str, Any]:
    files = []
    for p in sorted(bundle_dir.iterdir()):
        if p.is_file() and p.name != "manifest.json" and not p.name.endswith("-bundle.zip"):
            files.append(
                {"name": p.name, "sha256": bundle.sha256_file(p), "bytes": p.stat().st_size}
            )
    board_path = Path(request.board).resolve()
    inputs = {
        "profile": {
            "name": checked.profile.name,
            "file": f"profiles/{checked.profile.name}.json",
            "sha256": bundle.sha256_file(capability.file_path("profiles", checked.profile.name)),
        },
        "stackup": {
            "id": checked.stackup.id,
            "file": f"stackups/{checked.stackup.id}.json",
            "sha256": bundle.sha256_file(capability.file_path("stackups", checked.stackup.id)),
        },
        "vendor": {
            "name": checked.vendor["vendor"],
            "file": f"vendors/{checked.vendor['vendor']}.json",
            "sha256": bundle.sha256_file(capability.file_path("vendors", checked.vendor["vendor"])),
        },
        "oldest_source": min(
            capability.oldest_access(d)
            for d in (checked.profile.doc, checked.stackup.doc, checked.vendor)
        ),
    }
    if request.parts_lock:
        inputs["parts_lock_sha256"] = bundle.sha256_file(Path(request.parts_lock))
    provenance = board_path.parent / "yapnr-provenance.json"
    run = None
    if provenance.is_file():
        try:
            doc = json.loads(provenance.read_text())
            run = {k: doc.get(k) for k in ("run_id", "candidate", "engine_id") if k in doc}
        except ValueError:
            run = None
    return {
        "schema": bundle.MANIFEST_SCHEMA,
        "board": {
            "name": checked.name,
            "file": board_path.name,
            "sha256": bundle.sha256_file(board_path),
            "input_id": checked.facts.input_id,
        },
        "run": run,
        "files": files,
        "gerber_zip": {
            "name": f"{checked.name}-{checked.vendor['vendor']}-gerbers.zip",
            "sha256": zinfo["sha256"],
            "members": zinfo["members"],
        },
        "kicad_aux": [{"name": f.name, "sha256": bundle.sha256_file(f.path)} for f in aux],
        "inputs": inputs,
        "tools": {
            "yapnr": __version__,
            "source_revision": _source_revision(),
            "kicad_cli": checked.cli.version(),
            "platform": f"{sys.platform}-{platform.machine().lower()}",
            "python": platform.python_version(),
        },
        "command": bundle.relative_argv(list(request.argv), Path.cwd()),
        "time": when.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }
