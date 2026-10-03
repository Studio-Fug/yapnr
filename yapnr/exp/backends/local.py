"""The local backend: a niced process pool on the owner's machine, with the host's toolchain.

The store is a directory (``[local] store``, or ``private_results_root`` for private campaigns)
with the bucket layout; bundles live in its ``bundles/``. ``submit`` starts ``local_pool.py``
detached (``--wait`` runs it in the foreground) and records its pid and start time, so
``cancel`` signals only that process, never one it did not start.

The toolchain profile (``${PYTHON}``, ``${KICAD_CLI}``, ``${KICAD_PYTHON}``, ``${FOOTPRINTS}``)
comes from ``[local]`` in the owner config, else from the ``YAPNR_KICAD_*``/``PNR_KICAD_*``
environment variables; on macOS the footprints default to the KiCad bundle's ``SharedSupport``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from yapnr.exp.backends.base import Backend, Stores, SubmitError
from yapnr.exp.config import Config
from yapnr.exp.store import LocalStore

POOL = Path(__file__).resolve().parent / "local_pool.py"


def toolchain_profile(config: Config, environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = os.environ if environ is None else environ
    local = config.local

    def pick(value, *names):
        if value:
            return os.path.expanduser(value)
        for name in names:
            if env.get(name):
                return env[name]
        return None

    kicad_cli = pick(local.kicad_cli, "YAPNR_KICAD_CLI", "PNR_KICAD_CLI")
    footprints = pick(local.footprints, "YAPNR_KICAD_FOOTPRINTS", "PNR_KICAD_FOOTPRINTS")
    if footprints is None and kicad_cli:
        cli = Path(kicad_cli)
        if cli.parent.name == "MacOS" and cli.parent.parent.name == "Contents":
            footprints = str(cli.parent.parent / "SharedSupport" / "footprints")
    return {
        "PYTHON": pick(local.python, "YAPNR_PYTHON"),
        "KICAD_CLI": kicad_cli,
        "KICAD_PYTHON": pick(local.kicad_python, "YAPNR_KICAD_PYTHON", "PNR_KICAD_PYTHON"),
        "FOOTPRINTS": footprints,
        "launcher": [],
        "isolate_home": bool(local.isolate_home),
    }


def _process_command(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


class Local(Backend):
    name = "local"

    def root(self, plan, config: Config) -> Path:
        return config.local.store_path(plan.private)

    def stores(self, plan, config: Config, cloud=None) -> Stores:
        store = LocalStore(self.root(plan, config))
        return Stores(store, store)

    def render(self, plan, config, cls, submission, indices, deadline) -> Dict[str, str]:
        items = [[submission, n, cls.cpus, int(cls.max_wall_s)] for n in range(len(indices))]
        return {"%s.items.json" % cls.name: json.dumps(items) + "\n"}

    def preview(self, plan, config):
        written = super().preview(plan, config)
        path = plan.dir / "backend" / self.name / "toolchain.json"
        path.write_text(json.dumps(toolchain_profile(config), indent=2, sort_keys=True) + "\n")
        return written + [path]

    def launch(self, plan, config, cls, submission, files, stores, cloud=None, dry_run=False):
        profile = toolchain_profile(config)
        missing = [
            k for k in ("PYTHON", "KICAD_CLI", "KICAD_PYTHON", "FOOTPRINTS") if not profile[k]
        ]
        needed = {
            name
            for t in plan.tasks
            for arg in t["command"]
            for name in missing
            if "${%s}" % name in arg
        }
        if needed:
            raise SubmitError(
                "the local toolchain has no %s: set [local] in the owner config (or the "
                "YAPNR_KICAD_* variables)" % ", ".join(sorted(needed))
            )
        root = self.root(plan, config)
        directory = root / "campaigns" / plan.id / "local"
        directory.mkdir(parents=True, exist_ok=True)
        toolchain = directory / "toolchain.json"
        toolchain.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
        state = directory / ("pool-%d.json" % submission)
        argv = [
            sys.executable,
            str(POOL),
            "--store",
            str(root),
            "--campaign",
            plan.id,
            "--items",
            str(files["%s.items.json" % cls.name]),
            "--workers",
            str(config.local.workers),
            "--nice",
            str(config.local.nice),
            "--toolchain",
            str(toolchain),
            "--state",
            str(state),
        ]
        if dry_run:
            return {"pid": None, "summary": "dry run: %s" % " ".join(argv), "argv": argv}
        wait = getattr(self, "wait", False)
        log = directory / ("pool-%d.log" % submission)
        with open(log, "ab") as handle:
            proc = subprocess.Popen(
                argv,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        if wait:
            code = proc.wait()
            return {
                "pid": proc.pid,
                "exit": code,
                "log": str(log),
                "summary": "pool finished (%d)" % code,
            }
        return {
            "pid": proc.pid,
            "log": str(log),
            "summary": "pool pid %d (log %s)" % (proc.pid, log),
        }

    def state(self, plan_meta, record, config, cloud=None):
        pid = (record.get("job") or {}).get("pid")
        if not pid or "exit" in record.get("job", {}):
            return {"state": "FINISHED", "counts": {}, "preemptions": 0}
        command = _process_command(int(pid))
        alive = bool(command and plan_meta["id"] in command and "local_pool.py" in command)
        return {"state": "RUNNING" if alive else "FINISHED", "counts": {}, "preemptions": 0}

    def cancel(self, record, config, cloud=None, dry_run=False) -> str:
        pid = (record.get("job") or {}).get("pid")
        if not pid:
            return "submission %s has no pool" % record.get("submission")
        command = _process_command(int(pid))
        # Only the pool this campaign started: same pid, same script, same campaign id.
        if not command or "local_pool.py" not in command or record["campaign"] not in command:
            return "pool %s is not running (nothing signalled)" % pid
        if dry_run:
            return "would send SIGTERM to pool %s" % pid
        os.kill(int(pid), signal.SIGTERM)
        return "sent SIGTERM to pool %s (running tasks stop and record nothing)" % pid
