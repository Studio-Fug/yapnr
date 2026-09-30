"""KiCad tools for the viewer: the headless ``kicad-cli`` and KiCad's Python.

A small shim until ``yapnr.kicad.toolchain`` exists (PR6a); the rules are the repository's
(AGENTS.md, DEVELOPERS.md#kicad):

- Resolution order: the explicit value (a flag), then the environment (``YAPNR_KICAD_CLI`` with
  its alias ``PNR_KICAD_CLI``; ``YAPNR_KICAD_PYTHON``), then the machine config
  (``~/.config/yapnr/config.toml``, ``[kicad] cli`` and ``python``), then discovery.
- Discovery: on macOS only the headless copy ``~/Applications/KiCad-headless.app``; on Linux
  ``kicad-cli`` on ``PATH`` and a ``python3`` that imports ``pcbnew`` (probed with a timeout).
- Never the GUI application: anything inside ``KiCad.app`` or ``/Applications/KiCad`` is refused
  (every call from the stock bundle registers a Dock icon), and so is a program inside any
  ``.app`` bundle that is not background-only.

Without a tool the feature that needs it is off, with the reason; nothing falls back to the GUI.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Tuple

from yapnr.cli import KICAD_CLI_ENV_VARS, KICAD_PYTHON_ENV_VARS

HEADLESS_APP = Path("~/Applications/KiCad-headless.app")
HEADLESS_CLI = HEADLESS_APP / "Contents/MacOS/kicad-cli"
HEADLESS_PYTHON = HEADLESS_APP / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
RECIPE = "the headless copy: DEVELOPERS.md#kicad"
PROBE_TIMEOUT = 30.0


class Unavailable(Exception):
    """A KiCad tool that cannot or must not be used; the message says why."""


def app_bundle(path: Path) -> Optional[Path]:
    """The ``.app`` bundle folder a path lies in, else None."""
    for q in (path, *path.parents):
        if q.suffix == ".app":
            return q
    return None


def bundle_info(bundle: Path) -> dict:
    try:
        return plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError):
        return {}


def background_only(info: Mapping) -> bool:
    return any(
        v is True or v == 1 or str(v).strip().lower() in ("1", "yes", "true")
        for v in (info.get("LSBackgroundOnly"), info.get("LSUIElement"))
    )


def refuse_gui(path: Path, what: str = "KiCad program") -> None:
    """Raise Unavailable for a GUI application's program, or one in a bundle shown in the Dock.

    Checked for the given path and its symlink target: the stock bundle (anything inside
    ``KiCad.app`` or ``/Applications/KiCad``) and any ``.app`` whose Info.plist is not
    background-only (``LSBackgroundOnly`` / ``LSUIElement``), such as a renamed copy of the GUI
    application. LaunchServices registers such a bundle as a regular application, which puts an
    icon in the Dock for every call.
    """
    for q in (path, path.resolve()):
        if "KiCad.app" in q.parts or str(q).startswith("/Applications/KiCad/"):
            raise Unavailable(f"refusing the GUI KiCad {what} {path}: use {RECIPE}")
        bundle = app_bundle(q)
        if bundle and not background_only(bundle_info(bundle)):
            raise Unavailable(
                f"refusing {path}: {bundle.name} is not a background-only app bundle (no"
                f" LSBackgroundOnly or LSUIElement in its Info.plist), so its {what} would put an"
                f" icon in the Dock; use {RECIPE}"
            )


def _from_env(names, environ: Optional[Mapping[str, str]]) -> Optional[str]:
    env = os.environ if environ is None else environ
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _checked(path: Path, what: str) -> Path:
    refuse_gui(path, what)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Unavailable(f"{what} not found or not executable: {path}")
    return path


def kicad_cli(
    explicit: Optional[os.PathLike] = None,
    machine: Optional[os.PathLike] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """The headless ``kicad-cli`` (Unavailable when there is none that may be used)."""
    given = explicit or _from_env(KICAD_CLI_ENV_VARS, environ) or machine
    if given:
        return _checked(Path(given).expanduser(), "kicad-cli")
    if sys.platform == "darwin":
        cli = HEADLESS_CLI.expanduser()
        if cli.is_file():
            return _checked(cli, "kicad-cli")
        raise Unavailable(
            f"no headless kicad-cli: set --kicad-cli or {KICAD_CLI_ENV_VARS[0]} ({RECIPE});"
            " the GUI application's copy is never used"
        )
    found = shutil.which("kicad-cli")
    if found:
        return _checked(Path(found), "kicad-cli")
    raise Unavailable(f"kicad-cli not found: set --kicad-cli or {KICAD_CLI_ENV_VARS[0]}")


def probe_pcbnew(python: Path, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when ``python`` can import ``pcbnew`` (a child process, bounded by ``timeout``)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        done = subprocess.run(
            [str(python), "-c", "import pcbnew"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def kicad_python(
    explicit: Optional[os.PathLike] = None,
    machine: Optional[os.PathLike] = None,
    environ: Optional[Mapping[str, str]] = None,
    probe=probe_pcbnew,
) -> Path:
    """KiCad's Python, which imports ``pcbnew`` (Unavailable when there is none to use)."""
    given = explicit or _from_env(KICAD_PYTHON_ENV_VARS, environ) or machine
    if given:
        return _checked(Path(given).expanduser(), "KiCad Python")
    if sys.platform == "darwin":
        python = HEADLESS_PYTHON.expanduser()
        if python.is_file():
            return _checked(python, "KiCad Python")
        raise Unavailable(
            f"no KiCad Python: set --kicad-python or {KICAD_PYTHON_ENV_VARS[0]} ({RECIPE})"
        )
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if candidate and Path(candidate).is_file() and probe(Path(candidate)):
            return Path(candidate)
    raise Unavailable(
        f"no Python that imports pcbnew: set --kicad-python or {KICAD_PYTHON_ENV_VARS[0]}"
    )


class Toolchain:
    """Lazily resolved KiCad tools for one viewer; each is looked up once, on first use."""

    def __init__(self, cli=None, python=None, machine_cli=None, machine_python=None, environ=None):
        self._given = dict(cli=(cli, machine_cli), python=(python, machine_python))
        self._environ = environ
        self._found = {}

    def _get(self, name, resolve) -> Tuple[Optional[Path], Optional[str]]:
        if name not in self._found:
            explicit, machine = self._given[name]
            try:
                self._found[name] = (resolve(explicit, machine, self._environ), None)
            except Unavailable as ex:
                self._found[name] = (None, str(ex))
        return self._found[name]

    def cli(self) -> Tuple[Optional[Path], Optional[str]]:
        """(kicad-cli or None, why not)."""
        return self._get("cli", kicad_cli)

    def python(self) -> Tuple[Optional[Path], Optional[str]]:
        """(KiCad Python or None, why not)."""
        return self._get("python", kicad_python)
