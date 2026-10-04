#!/usr/bin/env python3
"""Turn a yapnr wheel built on another platform into the Linux wheel of one architecture.

    tools/release/linux_wheel.py WHEEL LIBRARY {amd64,arm64} OUT_DIR

The yapnr wheel carries yapnr.rf's native FDTD library for the platform Bazel built it on
(release/wheel.bzl). tools/image/build_local.sh on a Mac builds the wheel there, compiles the
library for Linux in a container (the flags of //yapnr/rf:libyapnr_fdtd.so) and calls this to
put that library in the wheel instead, retag it (py3-none-manylinux_2_34_<cpu>) and rewrite its
RECORD, so that the image it builds installs a Linux wheel. CI builds each Linux wheel natively
and does not need this. Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import zipfile

LIBRARY = "yapnr/rf/libyapnr_fdtd.so"
TAGS = {"amd64": "manylinux_2_34_x86_64", "arm64": "manylinux_2_34_aarch64"}


def record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def convert(wheel: str, library: str, arch: str, out_dir: str) -> str:
    tag = TAGS[arch]
    base = os.path.basename(wheel)
    name, version = base.split("-")[:2]
    out = os.path.join(out_dir, f"{name}-{version}-py3-none-{tag}.whl")
    with open(library, "rb") as handle:
        lib = handle.read()
    with zipfile.ZipFile(wheel) as src:
        names = src.namelist()
        if LIBRARY not in names:
            raise SystemExit(f"{wheel}: no {LIBRARY}")
        record = next(n for n in names if n.endswith(".dist-info/RECORD"))
        info = record.rsplit("/", 1)[0]
        lines = []
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == record:
                    continue
                data = src.read(item)
                if item.filename == LIBRARY:
                    data = lib
                elif item.filename == f"{info}/WHEEL":
                    text = data.decode()
                    kept = [x for x in text.splitlines() if not x.startswith("Tag: ")]
                    data = ("\n".join(kept + [f"Tag: py3-none-{tag}"]) + "\n").encode()
                dst.writestr(item, data)
                lines.append(record_line(item.filename, data))
            lines.append(f"{record},,")
            dst.writestr(src.getinfo(record), "\n".join(lines) + "\n")
    return out


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4 or args[2] not in TAGS:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    print(convert(*args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
