"""Full workspace conversation/tool records, serialized with workspace writers."""

import fcntl
import json
import os
from pathlib import Path


def record(project, event):
    folder = Path(project).resolve() / ".yapnr/workspace"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with (folder / "conversation.jsonl").open("ab") as output:
            output.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            )
            output.flush()
            os.fsync(output.fileno())
