"""Lifetime for a private native host; does not change global KiCad preferences."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class DrcSession:
    def __init__(self, board, directory):
        self.board = Path(board).resolve()
        self.directory = Path(directory).resolve()
        self.process = None

    def __enter__(self):
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("launch_host.py")),
                    str(self.directory),
                    "--board",
                    str(self.board),
                ],
                check=True,
                timeout=30,
                capture_output=True,
                text=True,
            )
            self.process = json.loads((self.directory / "process.json").read_text())
            if not (self.directory / "ready.json").exists():
                raise RuntimeError("Native warm host not ready")
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        path = self.directory / "process.json"
        if not path.exists():
            return
        record = json.loads(path.read_text())
        pid = record["pid"]
        from pnr.proc import run_output

        current = run_output(["ps", "-p", str(pid), "-o", "command="], timeout=30, check=False)
        if current.returncode == 0 and current.stdout.strip() == " ".join(record["command"]):
            os.kill(pid, signal.SIGTERM)
            (self.directory / "session-closed.json").write_text(
                json.dumps(dict(pid=pid, closed=time.time()))
            )

    def __exit__(self, *args):
        self.close()

    @property
    def environment(self):
        return dict(os.environ, PNR_DRC_SERVICE=str(self.directory))
