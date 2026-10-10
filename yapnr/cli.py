"""The ``yapnr`` command-line interface.

Run it with ``bazel run //:yapnr -- <args>`` or ``python -m yapnr <args>``.

The commands:

``yapnr --version``
    Print the package version.

``yapnr doctor [--json]``
    Report the Python, numpy and torch versions, whether the KiCad command-line
    tool and KiCad's Python are configured, and the source revision the
    container image was built from. This is a stub of the full toolchain report
    planned in docs/migration-plan.md (section 2.3). It only reads environment
    variables and checks the configured paths on disk; it never starts KiCad,
    so it cannot trigger a GUI or a Dock icon.

``yapnr atopile setup|info|build|lock-parts|materialize``
    The atopile toolchain without Nix (docs/frontends/atopile.md).

``yapnr picker serve|catalog``
    The offline atopile part picker and its catalogs.

``yapnr part-cache ...``
    The part cache: local directory or server (docs/part-cache.md).

``yapnr exp plan|submit|status|logs|fetch|cancel|doctor|prices|unfreeze|calibration``
    Experiment campaigns on a local pool, Google Cloud Batch or Slurm
    (docs/cloud-experiments.md).

``yapnr fab profiles|show|check|build|preview``
    Vendor profiles and stackups, the fab check (KiCad DRC under a vendor's rules), the
    per-vendor fab bundle and a preview of its gerbers (docs/fab-and-ordering.md).

``yapnr order stage|vendors``
    The order card and the vendor's upload page. Staging only: yapnr never uploads, orders or
    pays (docs/fab-and-ordering.md).
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

# The Python interpreter that can `import pcbnew`, for KiCad-side workers. The
# container image sets it to /usr/bin/python3 (docs/containers.md).
KICAD_PYTHON_ENV_VARS = ("YAPNR_KICAD_PYTHON",)

# The commit a container image was built from (AGPL section 13: the image and
# the viewer it serves name their exact source). Set by the image build.
SOURCE_REVISION_ENV_VAR = "YAPNR_SOURCE_REVISION"


def _module_version(name: str) -> Optional[str]:
    """Return the installed version of ``name``, or None if it cannot be imported."""
    try:
        module = importlib.import_module(name)
    except Exception:  # ImportError, or a broken binary wheel
        return None
    return str(getattr(module, "__version__", "unknown"))


def _tool_status(env_vars: Sequence[str], environ: Optional[Dict[str, str]]) -> Dict[str, Any]:
    env = os.environ if environ is None else environ
    for var in env_vars:
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


def kicad_cli_status(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Describe the configured KiCad CLI without running it.

    Only the environment is consulted in PR0. Discovery of installed KiCad
    bundles arrives with ``yapnr.kicad.toolchain`` (PR6a).
    """
    return _tool_status(KICAD_CLI_ENV_VARS, environ)


def kicad_python_status(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Describe the configured KiCad Python (the one with ``pcbnew``) without running it."""
    return _tool_status(KICAD_PYTHON_ENV_VARS, environ)


def doctor_report(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Collect the facts that ``yapnr doctor`` prints."""
    env = os.environ if environ is None else environ
    return {
        "yapnr": __version__,
        "source_revision": env.get(SOURCE_REVISION_ENV_VAR, "").strip() or None,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "numpy": _module_version("numpy"),
        "torch": _module_version("torch"),
        "kicad_cli": kicad_cli_status(environ),
        "kicad_python": kicad_python_status(environ),
        "atopile": atopile_status(environ),
    }


def atopile_status(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The atopile environment, found as a build would find it, without running it."""
    from yapnr.frontends.atopile import toolchain

    try:
        return toolchain.static_status(environ)
    except Exception as err:  # a broken lock or config must not break the doctor
        return {"configured": False, "ok": False, "error": str(err)}


def _atopile_line(status: Dict[str, Any]) -> str:
    if not status.get("configured"):
        return "not set up (run `yapnr atopile setup`)"
    state = "ok" if status.get("ok") else f"NEEDS {status.get('pinned')}"
    return f"{status.get('atopile') or 'missing'} via {status.get('source')} ({state})"


def _tool_line(status: Dict[str, Any], env_vars: Sequence[str]) -> str:
    if status["configured"]:
        state = "ok" if status["executable"] else "NOT EXECUTABLE"
        return f"configured via {status['source']} ({state})"
    return f"not configured (set {' or '.join(env_vars)})"


def _format_report(report: Dict[str, Any]) -> List[str]:
    lines = [f"yapnr     {report['yapnr']}"]
    if report["source_revision"]:
        lines.append(f"revision  {report['source_revision']}")
    lines += [
        f"python    {report['python']} ({report['python_implementation']}, {report['platform']})",
        f"numpy     {report['numpy'] or 'not installed'}",
        f"torch     {report['torch'] or 'not installed'}",
        f"kicad-cli {_tool_line(report['kicad_cli'], KICAD_CLI_ENV_VARS)}",
        f"kicad-py  {_tool_line(report['kicad_python'], KICAD_PYTHON_ENV_VARS)}",
        f"atopile   {_atopile_line(report['atopile'])}",
    ]
    return lines


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

    from yapnr.agent.cli import register as register_agent
    from yapnr.agent.requirements import register as register_requirements
    from yapnr.agent.reviews import register as register_review
    from yapnr.agent.web import register as register_web
    from yapnr.agent.workflow import register as register_workflow
    from yapnr.agent.workspace import register as register_workspace
    from yapnr.exp.cli import register as register_exp
    from yapnr.experiments import register as register_experiments
    from yapnr.fab.cli import register as register_fab
    from yapnr.frontends.atopile.cli import register_atopile, register_picker
    from yapnr.order.cli import register as register_order
    from yapnr.partcache.cli import register as register_part_cache

    register_experiments(commands)
    register_agent(commands)
    register_workflow(commands)
    register_workspace(commands)
    register_requirements(commands)
    register_review(commands)
    register_web(commands)
    register_atopile(commands)
    register_picker(commands)
    register_part_cache(commands)
    register_exp(commands)
    register_fab(commands)
    register_order(commands)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The command line as given (for the fab manifest, which keeps relative paths only).
    args._argv = list(argv) if argv is not None else sys.argv[1:]
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
