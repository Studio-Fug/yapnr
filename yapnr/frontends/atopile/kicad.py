"""The KiCad an atopile build may use: the headless ``kicad-cli`` and its stock footprints.

Rules (AGENTS.md, DEVELOPERS.md#kicad): never a program of the GUI application. On macOS only a
background-only bundle is accepted (the headless copy ``~/Applications/KiCad-headless.app``);
anything inside ``KiCad.app`` or ``/Applications/KiCad`` is refused. The rules are those of the
viewer's shim (``yapnr.viewer.toolchain``), which this module does not import: the viewer's
library carries the numerical stack. Both give way to ``yapnr.kicad.toolchain`` (PR6a).

atopile 0.15.8 targets KiCad 9 (``faebryk.libs.kicad.paths.KICAD_VERSION = "9.0"``) and resolves a
``"Library:Footprint"`` reference only through the project's own ``fp-lib-table``, never KiCad's
global table. ``write_fp_lib_table`` writes that table from the discovered KiCad's (10) stock
libraries, for the libraries the project's sources reference.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set

from yapnr.frontends.atopile import proc

CLI_ENV_VARS = ("YAPNR_KICAD_CLI", "PNR_KICAD_CLI")
FOOTPRINTS_ENV_VAR = "YAPNR_KICAD_FOOTPRINTS"
HEADLESS_APP = Path("~/Applications/KiCad-headless.app")
LINUX_FOOTPRINT_DIRS = (
    Path("/usr/share/kicad/footprints"),
    Path("/usr/local/share/kicad/footprints"),
)
TABLE_DESCR = "KiCad stock library (written by yapnr)"
_REFERENCE = re.compile(r'"([A-Za-z0-9_.+-]+):([^"\n:]+)"')


class Unavailable(Exception):
    """No KiCad program that may be used; the message says why."""


def _app_bundle(path: Path) -> Optional[Path]:
    for q in (path, *path.parents):
        if q.suffix == ".app":
            return q
    return None


def _background_only(bundle: Path) -> bool:
    try:
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError):
        return False
    return any(
        value is True or str(value).strip().lower() in ("1", "yes", "true")
        for value in (info.get("LSBackgroundOnly"), info.get("LSUIElement"))
    )


def refuse_gui(path: Path) -> None:
    """Raise Unavailable for a program of the GUI KiCad or of any bundle shown in the Dock."""
    for q in (path, path.resolve()):
        if "KiCad.app" in q.parts or str(q).startswith("/Applications/KiCad/"):
            raise Unavailable(f"refusing the GUI KiCad program {path} (DEVELOPERS.md#kicad)")
        bundle = _app_bundle(q)
        if bundle is not None and not _background_only(bundle):
            raise Unavailable(f"refusing {path}: {bundle.name} is not a background-only app bundle")


def find_cli(explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Path:
    """The headless ``kicad-cli``: the flag, the environment, then discovery."""
    env = os.environ if environ is None else environ
    given = explicit or next((env[v] for v in CLI_ENV_VARS if env.get(v, "").strip()), None)
    if given:
        path = Path(given).expanduser()
    elif sys.platform == "darwin":
        path = (HEADLESS_APP / "Contents/MacOS/kicad-cli").expanduser()
        if not path.is_file():
            raise Unavailable(
                "no headless kicad-cli: make ~/Applications/KiCad-headless.app"
                f" (DEVELOPERS.md#kicad) or set {CLI_ENV_VARS[0]}"
            )
    else:
        found = shutil.which("kicad-cli")
        if not found:
            raise Unavailable(f"kicad-cli not found: install KiCad 10 or set {CLI_ENV_VARS[0]}")
        path = Path(found)
    refuse_gui(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Unavailable(f"kicad-cli not found or not executable: {path}")
    return path


def find_footprints(cli: Optional[Path], environ: Optional[Mapping[str, str]] = None) -> Path:
    """The stock footprint directory (``*.pretty``) of the KiCad that ``cli`` belongs to."""
    env = os.environ if environ is None else environ
    candidates: List[Path] = []
    if env.get(FOOTPRINTS_ENV_VAR):
        candidates.append(Path(env[FOOTPRINTS_ENV_VAR]).expanduser())
    for name in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR"):
        if env.get(name):
            candidates.append(Path(env[name]).expanduser())
    if cli is not None:
        bundle = _app_bundle(cli.resolve())
        if bundle is not None:
            candidates.append(bundle / "Contents/SharedSupport/footprints")
        candidates.append(cli.resolve().parent.parent / "share/kicad/footprints")
    candidates.extend(LINUX_FOOTPRINT_DIRS)
    for path in candidates:
        if path.is_dir() and any(path.glob("*.pretty")):
            return path
    raise Unavailable(
        f"no KiCad stock footprint libraries found (set {FOOTPRINTS_ENV_VAR} to the directory"
        " holding the *.pretty folders)"
    )


def version(cli: Path, env: Optional[Dict[str, str]] = None, timeout: float = 60) -> str:
    """``kicad-cli --version`` (e.g. ``10.0.6``), run with a deadline."""
    return proc.output([str(cli), "--version"], timeout=timeout, env=env).strip()


def stock_libraries(footprints: Path) -> Dict[str, Path]:
    return {p.name[: -len(".pretty")]: p for p in sorted(footprints.glob("*.pretty")) if p.is_dir()}


def referenced_libraries(sources: Iterable[Path], known: Iterable[str]) -> Set[str]:
    """The stock libraries named in ``"Library:Footprint"`` strings of the given ``.ato`` files."""
    known = set(known)
    found: Set[str] = set()
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for library, _footprint in _REFERENCE.findall(text):
            if library in known:
                found.add(library)
    return found


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lib_line(name: str, path: Path) -> str:
    return (
        f'\t(lib (name {_quote(name)})(type "KiCad")(uri {_quote(str(path))})'
        f'(options "")(descr {_quote(TABLE_DESCR)}))'
    )


def fp_lib_table(libraries: Mapping[str, Path], existing: Optional[str] = None) -> str:
    """An ``fp-lib-table`` (KiCad's version 7 format) listing ``libraries`` by absolute path.

    With ``existing`` (the text of a table), its entries are kept and only libraries it does not
    name yet are added.
    """
    if existing and existing.strip().startswith("(fp_lib_table"):
        present = set(re.findall(r'\(name\s+"((?:[^"\\]|\\.)*)"\)', existing))
        added = [_lib_line(n, libraries[n]) for n in sorted(libraries) if n not in present]
        if not added:
            return existing
        body = existing.rstrip()
        assert body.endswith(")")
        return body[:-1].rstrip() + "\n" + "\n".join(added) + "\n)\n"
    lines = ["(fp_lib_table", "\t(version 7)"]
    lines += [_lib_line(name, libraries[name]) for name in sorted(libraries)]
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_fp_lib_table(
    table: Path, footprints: Path, project: Path, everything: bool = False
) -> List[str]:
    """Add the stock libraries the project's sources reference (or all) to ``table``.

    Returns the library names that are now in the table from the stock set; writes nothing when
    the sources reference none. Entries already in an existing table are kept (atopile adds its
    part libraries to the same table during the build).
    """
    stock = stock_libraries(footprints)
    if everything:
        chosen = set(stock)
    else:
        chosen = referenced_libraries(sorted(project.rglob("*.ato")), stock)
    if not chosen:
        return []
    existing = table.read_text(encoding="utf-8") if table.is_file() else None
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(fp_lib_table({n: stock[n] for n in chosen}, existing), encoding="utf-8")
    return sorted(chosen)
