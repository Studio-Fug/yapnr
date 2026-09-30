"""The environment of an atopile build: built from an allowlist, never inherited.

The child gets exactly these variables (anything else of the caller's environment, such as API
keys, cloud or vendor credentials, proxies or ``PYTHONPATH``, is left out):

- ``PATH`` (a minimal system path) and a private ``HOME``, ``TMPDIR`` and ``XDG_*`` directories
  under the build's work directory. atopile writes its config, telemetry id and build history
  there, and tries to install its KiCad plugin into the KiCad config it finds under ``HOME``;
  ``kicad-cli`` refuses to run without a ``HOME``. Nothing reaches the user's own directories.
- ``ATO_NON_INTERACTIVE=1``, ``CI=1`` (atopile skips datasheet downloads and defaults telemetry
  off in CI) and ``FBRK_TELEMETRY=0``.
- ``ATO_SERVICES_COMPONENTS_URL`` and ``ATO_SERVICES_PACKAGES_URL``: the runner's loopback picker,
  so the components API, its message of the day and the package registry are all answered
  locally (environment settings override ``ato.yaml``).
- ``OPENSSL_armcap=0`` on aarch64: OpenSSL's ARMv8 capability probe raises SIGILL inside the
  ``cryptography`` wheel under Apple's virtualization (the Nix wrapper set it too).
- ``GIT_TERMINAL_PROMPT=0``, and offline ``GIT_ALLOW_PROTOCOL=file``: git may read local
  repositories only (atopile clones a missing git dependency during every build; the hook
  refuses that too).
- ``PYTHONPATH`` = the hook directory, and the hook's ``YAPNR_ATO_*`` settings
  (``hook/yapnr_atopile_hook.py``).
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Dict, Optional

HOOK_DIR = Path(__file__).resolve().parent / "hook"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def private_dirs(work: Path) -> Dict[str, Path]:
    """The per-build HOME, TMPDIR and XDG directories (created)."""
    dirs = {
        "HOME": work / "home",
        "TMPDIR": work / "tmp",
        "XDG_CONFIG_HOME": work / "home" / ".config",
        "XDG_CACHE_HOME": work / "home" / ".cache",
        "XDG_DATA_HOME": work / "home" / ".local" / "share",
        "XDG_STATE_HOME": work / "home" / ".local" / "state",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def build_env(
    work: Path,
    picker_url: str,
    kicad_cli: Optional[Path] = None,
    footprints: Optional[Path] = None,
    hook_log: Optional[Path] = None,
    offline: bool = True,
    local_parts: bool = True,
    machine: Optional[str] = None,
) -> Dict[str, str]:
    env = {name: str(path) for name, path in private_dirs(work).items()}
    env.update(
        {
            "PATH": SYSTEM_PATH,
            "PYTHONPATH": str(HOOK_DIR),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "COLUMNS": "160",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "CI": "1",
            "ATO_NON_INTERACTIVE": "1",
            "FBRK_TELEMETRY": "0",
            "ATO_SERVICES_COMPONENTS_URL": picker_url,
            "ATO_SERVICES_PACKAGES_URL": picker_url,
            "GIT_TERMINAL_PROMPT": "0",
            "YAPNR_ATO_HOOK": "1",
            "YAPNR_ATO_PICKER_URL": picker_url,
            "YAPNR_ATO_OFFLINE": "1" if offline else "0",
            "YAPNR_ATO_LOCAL_PARTS": "1" if local_parts else "0",
        }
    )
    if offline:
        env["GIT_ALLOW_PROTOCOL"] = "file"
    if (machine or platform.machine()).lower() in ("arm64", "aarch64"):
        env["OPENSSL_armcap"] = "0"
    if kicad_cli is not None:
        env["YAPNR_ATO_KICAD_CLI"] = str(kicad_cli)
    if footprints is not None:
        env["YAPNR_ATO_KICAD_FOOTPRINTS"] = str(footprints)
    if hook_log is not None:
        env["YAPNR_ATO_HOOK_LOG"] = str(hook_log)
    return env
