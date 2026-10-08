"""Fetch checksum-pinned official native Codex files without npm or Node."""

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request
from pathlib import Path


def install(arch, out):
    pins = json.loads(Path(__file__).with_name("codex.lock.json").read_text())
    pin = pins[arch]
    with urllib.request.urlopen(pin["url"], timeout=60) as response:
        data = response.read(256 * 1024 * 1024 + 1)
    if len(data) > 256 * 1024 * 1024:
        raise ValueError("Codex archive exceeds size limit")
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != pin["integrity"]:
        raise ValueError("Codex archive checksum mismatch")
    root = Path(out)
    triplet = pin["triplet"]
    prefix = "package/vendor/" + triplet + "/"
    # Preserve the native CLI's relative resource layout. Optional voice/zsh runtime
    # is deliberately excluded; this image is a text engineering terminal.
    files = ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in files:
            member = archive.getmember(prefix + name)
            if not member.isfile() or member.size > 512 * 1024 * 1024:
                raise ValueError("Unexpected Codex archive entry: " + name)
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open("wb") as dst:
                import shutil

                shutil.copyfileobj(src, dst)
            os.chmod(target, 0o755)
    return pins


if __name__ == "__main__":
    install(sys.argv[1], sys.argv[2])
