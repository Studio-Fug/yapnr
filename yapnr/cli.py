"""The ``yapnr`` command-line interface.

Run it with ``bazel run //:yapnr -- <args>`` or ``python -m yapnr <args>``.

PR0 ships two commands:

``yapnr --version``
    Print the package version.

``yapnr doctor [--json]``
    Report the Python, numpy and torch versions and whether a KiCad command-line
    tool is configured. This is a stub of the full toolchain report planned in
    docs/migration-plan.md (section 2.3). It only reads environment variables and
    checks the configured path on disk; it never starts KiCad, so it cannot
    trigger a GUI or a Dock icon.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
from typing import Any, Dict, List, Optional, Sequence

from yapnr import __version__

# Environment variables that name the KiCad CLI, in priority order.
# PNR_KICAD_CLI is the name the engine used inside Splanc; it stays accepted as
# an alias (docs/migration-plan.md, section 2.3).
KICAD_CLI_ENV_VARS = ("YAPNR_KICAD_CLI", "PNR_KICAD_CLI")


def _module_version(name: str) -> Optional[str]:
    """Return the installed version of ``name``, or None if it cannot be imported."""
    try:
        module = importlib.import_module(name)
    except Exception:  # ImportError, or a broken binary wheel
        return None
    return str(getattr(module, "__version__", "unknown"))


def kicad_cli_status(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Describe the configured KiCad CLI without running it.

    Only the environment is consulted in PR0. Discovery of installed KiCad
    bundles arrives with ``yapnr.kicad.toolchain`` (PR6a).
    """
    env = os.environ if environ is None else environ
    for var in KICAD_CLI_ENV_VARS:
        value = env.get(var, "").strip()
        if value:
            return {
                "configured": True,
                "source": var,
                "path": value,
                "exists": os.path.isfile(value),
                "executable": os.path.isfile(value) and os.access(value, os.X_OK),
            }
    return {
        "configured": False,
        "source": None,
        "path": None,
        "exists": False,
        "executable": False,
    }


def doctor_report(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Collect the facts that ``yapnr doctor`` prints."""
    return {
        "yapnr": __version__,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "numpy": _module_version("numpy"),
        "torch": _module_version("torch"),
        "kicad_cli": kicad_cli_status(environ),
    }


def _format_report(report: Dict[str, Any]) -> List[str]:
    kicad = report["kicad_cli"]
    if kicad["configured"]:
        state = "ok" if kicad["executable"] else "NOT EXECUTABLE"
        kicad_line = f"configured via {kicad['source']} ({state})"
    else:
        names = " or ".join(KICAD_CLI_ENV_VARS)
        kicad_line = f"not configured (set {names})"
    return [
        f"yapnr     {report['yapnr']}",
        f"python    {report['python']} ({report['python_implementation']}, {report['platform']})",
        f"numpy     {report['numpy'] or 'not installed'}",
        f"torch     {report['torch'] or 'not installed'}",
        f"kicad-cli {kicad_line}",
    ]


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(_format_report(report)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yapnr",
        description="yet another place and route: PCB placement and routing for KiCad.",
    )
    parser.add_argument("--version", action="version", version=f"yapnr {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = commands.add_parser("doctor", help="report the environment and toolchain")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
