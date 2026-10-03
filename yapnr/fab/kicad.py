"""kicad-cli for the fab steps: headless only, every run time-bounded, always with ``TZ=UTC``.

The CLI is found as the atopile frontend finds it (``yapnr.frontends.atopile.kicad.find_cli``:
the flag, ``YAPNR_KICAD_CLI``/``PNR_KICAD_CLI``, then the headless copy on macOS or ``kicad-cli``
on ``PATH`` on Linux; programs of the GUI bundle are refused). ``yapnr.kicad.toolchain`` (PR6a)
takes over later.

kicad-cli 10.0.6 ignores ``SOURCE_DATE_EPOCH`` and stamps local time into gerbers and drill
files; ``TZ=UTC`` makes the stamps ``+00:00`` and ``yapnr.fab.export.normalize`` rewrites them.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from yapnr.frontends.atopile import kicad as atopile_kicad
from yapnr.frontends.atopile import proc

Unavailable = atopile_kicad.Unavailable
DEFAULT_TIMEOUT = 600.0


class KicadError(RuntimeError):
    """kicad-cli failed or timed out; the message carries its stderr."""


def find_cli(explicit: Optional[str] = None) -> Path:
    return atopile_kicad.find_cli(explicit)


def environment() -> Dict[str, str]:
    env = dict(os.environ)
    env["TZ"] = "UTC"
    return env


class Cli:
    """One kicad-cli with a deadline per call."""

    def __init__(self, path: Path, timeout: float = DEFAULT_TIMEOUT):
        self.path = Path(path)
        self.timeout = timeout
        self._version: Optional[str] = None

    def run(self, args: Sequence[str]) -> str:
        """kicad-cli with ``args`` (absolute paths), its stdout; KicadError on failure."""
        cmd = [str(self.path)] + [str(a) for a in args]
        try:
            return proc.output(cmd, timeout=self.timeout, env=environment())
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or err.stdout or "").strip().splitlines()[-5:]
            raise KicadError(
                f"kicad-cli {args[0] if args else ''} {args[1] if len(args) > 1 else ''} failed "
                f"({err.returncode}): " + " | ".join(detail)
            ) from None

    def version(self) -> str:
        if self._version is None:
            self._version = self.run(["--version"]).strip()
        return self._version

    def drc(self, board: Path, report: Path, refill: bool = True, save: bool = True) -> Dict:
        args = ["pcb", "drc", "--format", "json", "--severity-all", "-o", report]
        if refill:
            args.append("--refill-zones")
            if save:
                args.append("--save-board")
        self.run(args + [board])
        return json.loads(Path(report).read_text(encoding="utf-8"))

    def stats(self, board: Path, out: Path) -> Dict[str, Any]:
        self.run(["pcb", "export", "stats", "--format", "json", "--units", "mm", "-o", out, board])
        return json.loads(Path(out).read_text(encoding="utf-8"))

    def gerbers(
        self,
        board: Path,
        outdir: Path,
        layers: List[str],
        protel: bool = True,
        subtract_soldermask: bool = True,
        x2: bool = True,
    ) -> None:
        args = ["pcb", "export", "gerbers", "-o", str(outdir) + os.sep, "-l", ",".join(layers)]
        if subtract_soldermask:
            args.append("--subtract-soldermask")
        if not protel:
            args.append("--no-protel-ext")
        if not x2:
            args.append("--no-x2")
        self.run(args + [board])

    def drill(
        self,
        board: Path,
        outdir: Path,
        units: str,
        zeros: str,
        oval: str,
        origin: str = "absolute",
        separate_th: bool = False,
        map_format: Optional[str] = None,
    ) -> None:
        args = [
            "pcb",
            "export",
            "drill",
            "-o",
            str(outdir) + os.sep,
            "--format",
            "excellon",
            "-u",
            units,
            "--excellon-zeros-format",
            zeros,
            "--excellon-oval-format",
            oval,
            "--drill-origin",
            origin,
        ]
        if separate_th:
            args.append("--excellon-separate-th")
        if map_format:
            args += ["--generate-map", "--map-format", map_format]
        self.run(args + [board])

    def ipcd356(self, board: Path, out: Path) -> None:
        self.run(["pcb", "export", "ipcd356", "-o", out, board])
