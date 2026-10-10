"""Install a pinned OpenCode binary; authenticate the archive before reading it."""

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path


def install(architecture, destination):
    lock = json.loads(Path(__file__).with_name("opencode.lock.json").read_text())
    entry = lock["platforms"][architecture]
    with urllib.request.urlopen(entry["url"], timeout=60) as response:
        archive = response.read(256 * 1024 * 1024 + 1)
    if len(archive) > 256 * 1024 * 1024:
        raise ValueError("OpenCode archive exceeds size limit")
    if hashlib.sha256(archive).hexdigest() != entry["sha256"]:
        raise ValueError("OpenCode archive integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [m for m in bundle.getmembers() if m.name in ("opencode", "./opencode")]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 512 * 1024 * 1024:
            raise ValueError("Unexpected OpenCode binary entry")
        content = bundle.extractfile(members[0])
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.read())
        target.chmod(0o755)


if __name__ == "__main__":
    install(*sys.argv[1:])
