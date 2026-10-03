"""Bundles: deterministic zips, the manifest, the README and the privacy pass.

Design §6.4, §6.6, §6.7. Zips are **stored** (not deflated: deflate output can differ between
zlib builds), with sorted entries, fixed times (1980-01-01), Unix mode 0644, no extra fields and
no comment, so the same inputs give the same bytes.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple, Union

ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MANIFEST_SCHEMA = "yapnr-fab-manifest-v1"

# --public: a minimal built-in privacy pass until tools/privacy_scan.py is packaged.
_PRIVATE = (
    (
        "absolute path",
        re.compile(r"(?<![\w.])/(?:Users|home|Volumes|private|var/folders|tmp|root)/"),
    ),
    ("absolute path", re.compile(r"\b[A-Za-z]:\\(?:Users|Documents and Settings)\\")),
    ("home directory", re.compile(r"(?<![\w/])~/")),
    (
        "e-mail address",
        re.compile(
            r"\b[A-Za-z0-9._%+-]*[A-Za-z0-9]@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
        ),
    ),
)
_ALLOWED_EMAIL = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


Member = Tuple[str, Union[bytes, Path]]


def write_zip(path: Path, members: Iterable[Member]) -> Dict[str, Any]:
    """A deterministic stored zip; returns its sha256, size and the members' hashes."""
    items = []
    for name, content in members:
        data = content if isinstance(content, bytes) else Path(content).read_bytes()
        items.append((name, data))
    items.sort(key=lambda item: item[0])
    names = [n for n, _ in items]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate zip members in {path.name}")
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in items:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3  # Unix, whatever the platform
            info.external_attr = (0o100644 & 0xFFFF) << 16
            zf.writestr(info, data)
    tmp.replace(path)
    return {
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "members": [{"name": n, "sha256": sha256_bytes(d), "bytes": len(d)} for n, d in items],
    }


def privacy_findings(files: Sequence[Path]) -> List[Tuple[str, str, str]]:
    """(file, kind, match) for absolute paths, home directories and e-mail addresses."""
    found = []
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for kind, pattern in _PRIVATE:
            for match in pattern.finditer(text):
                if kind == "e-mail address" and _ALLOWED_EMAIL.search(match.group(0)):
                    continue
                found.append((Path(path).name, kind, match.group(0)))
    return found


def relative_argv(argv: Sequence[str], base: Path) -> List[str]:
    """The command line with every absolute path made relative to ``base`` (else its name)."""
    out = []
    for arg in argv:
        head, sep, tail = arg.partition("=")
        value = tail if sep and tail.startswith("/") else arg
        if value.startswith("/") or value.startswith("~"):
            p = Path(value).expanduser()
            try:
                rel = str(p.resolve().relative_to(base.resolve()))
            except ValueError:
                rel = p.name
            out.append(f"{head}={rel}" if sep and tail.startswith("/") else rel)
        else:
            out.append(arg)
    return out


def readme(info: Dict[str, Any]) -> str:
    """The bundle README (§6.6)."""
    b, s, card = info["board"], info["stackup"], info["card"]
    lines = [
        f"# {b['name']}: fab bundle for {card['vendor']['title']}",
        "",
        f"Profile `{info['profile']}`, stackup `{s.id}`, built by yapnr {info['yapnr']} with "
        f"kicad-cli {info['kicad']}. Staging only: yapnr uploaded nothing and ordered nothing.",
        "",
        "## Board",
        "",
        f"- Size: {b['size_mm'][0]:.2f} x {b['size_mm'][1]:.2f} mm "
        f"({b['area_sq_in']:.3f} sq in), {len(b['layers'])} copper layers "
        f"({', '.join(b['layers'])}).",
        f"- Input id (board sha256 with UUIDs normalized): `{b['input_id']}`.",
        f"- Finish and mask: {card['options'].get('finish')} / {card['options'].get('mask_color')}; "
        f"via covering: {card['options'].get('via_covering')}; thickness "
        f"{card['options'].get('thickness_mm')} mm.",
        "",
        "## Stackup",
        "",
        f"`{s.id}` ({s.vendor_name}), {s.thickness_mm} mm. **At checkout:** {s.checkout}",
        "",
        "| Layer | Material | Thickness (mm) | Dk | Df | Source |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for layer in s.layers:
        name = layer.get("name") or (f"{layer.get('side')} mask" if layer["kind"] == "mask" else "")
        material = layer.get("material") or ("copper" if layer["kind"] == "copper" else "mask")
        t = layer.get("thickness_mm")
        er = layer.get("er")
        er_text = (
            f"{er:g}"
            if er is not None
            else (f"n/p (prior {layer['er_prior']:g})" if layer.get("er_prior") else "")
        )
        df = layer.get("df")
        df_text = (
            f"{df:g}"
            if df is not None
            else (f"n/p (prior {layer['df_prior']:g})" if layer.get("df_prior") else "")
        )
        if layer["kind"] == "copper":
            er_text = df_text = ""
        lines.append(
            f"| {name or layer['kind']} | {material} | {'' if t is None else f'{t:.4g}'} | "
            f"{er_text} | {df_text} | {layer.get('src', '')} |"
        )
    lines += ["", "## Impedance", "", f"{card['impedance']}.", ""]
    rf = info.get("rf") or []
    if rf:
        lines += [
            "| Item | Layer | Assumed er / h (mm) | Stackup er / h (mm) | Nominal W50 (mm) |",
            "| --- | --- | --- | --- | ---: |",
        ]
        for item in rf:
            lines.append(
                f"| {item['reference']} ({item['name']}) | {item['layer']} | "
                f"{item['assumed_er']:g} / {item['assumed_h_mm']:g} | {item['er']} / {item['h_mm']:g} | "
                f"{item['w50_mm']} |"
            )
        lines.append("")
    else:
        lines += ["No RF footprints and no netclass with a target impedance.", ""]
    findings = info["findings"]
    lines += [
        "## Fab check",
        "",
        f"KiCad DRC under `{info['profile']}` (zones refilled under the vendor's clearances): "
        f"{card['checks']['drc_errors']} errors, {card['checks']['drc_warnings']} warnings. "
        f"Details: `drc.json`, `fab-check.json`.",
        "",
    ]
    for f in findings:
        lines.append(f"- **{f['code']}** ({f['severity']}): {f['message']}")
    lines += [
        "",
        "## Files",
        "",
        f"- `{card['files']['upload']['name']}`: the file to upload "
        f"(sha256 `{card['files']['upload']['sha256']}`):",
    ]
    for m in info["zip_members"]:
        lines.append(f"  - `{m['name']}` ({m['bytes']} bytes)")
    for extra in info.get("extra_files", []):
        lines.append(f"- `{extra}`")
    lines += [
        "- `order-card.md`, `order-card.json`: what to choose on the vendor's page.",
        "- `drc.json`, `fab-check.json`, `manifest.json`: the evidence and every file's sha256.",
        "",
        "## Sources",
        "",
        "Vendor facts are data with sources and access dates (re-verify before relying on them):",
        "",
    ]
    for key, src in sorted(info["sources"].items()):
        lines.append(f"- {key}: <{src['url']}> (accessed {src['accessed']})")
    return "\n".join(lines) + "\n"
