#!/usr/bin/env python3
"""Write the header of a GitHub release's notes: how to install this exact release.

The release workflow (.github/workflows/release.yaml) passes this header to
``gh release create --notes-file ... --generate-notes``; GitHub appends the notes it
generates from the merged pull requests, grouped by label (.github/release.yml).

Usage::

    tools/release/notes_header.py --tag v0.1.0 --pep440 0.1.0 \\
        --digest sha256:... --kicad-version 10.0.6 \\
        --archive out/yapnr-v0.1.0.tar.gz \\
        --wheel out/yapnr-0.1.0-py3-none-manylinux_2_34_x86_64.whl \\
        --wheel out/yapnr-0.1.0-py3-none-manylinux_2_34_aarch64.whl

The wheels are per platform (they carry yapnr.rf's native FDTD library).

Stdlib-only and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from typing import Optional, Sequence, Union

try:
    from tools.release.version import parse_tag
except ImportError:  # run as a script from tools/release/
    from version import parse_tag  # type: ignore[no-redef]

IMAGE = "ghcr.io/studio-fug/yapnr"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def sri_sha256(path: str) -> str:
    """The Subresource Integrity string Bazel's archive_override wants."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256-" + base64.b64encode(digest.digest()).decode("ascii")


def render(
    *,
    tag: str,
    pep440: str,
    digest: str,
    kicad_version: str,
    archive: str,
    wheel: Union[str, Sequence[str]],
    repo: str = "Studio-Fug/yapnr",
    upgrade_notes: Optional[str] = None,
) -> str:
    release = parse_tag(tag)
    if release is None:
        raise ValueError(f"not a release tag: {tag!r}")
    wheels = [wheel] if isinstance(wheel, str) else list(wheel)
    if not wheels:
        raise ValueError("no wheel")
    version = release.semver
    download = f"https://github.com/{repo}/releases/download/{tag}"
    lines = []
    if release.is_rc:
        lines += [
            f"> Release candidate for {release.major}.{release.minor}.{release.patch}: for"
            " regression testing, not for production use (docs/releases.md).",
            "",
        ]
    if upgrade_notes:
        lines += [
            f"Read the [upgrade notes](https://github.com/{repo}/blob/{tag}/{upgrade_notes})"
            " first: this release changes results or breaks compatibility.",
            "",
        ]
    lines += [
        "## Install",
        "",
        f"**Container image** for linux/amd64 and linux/arm64, with KiCad {kicad_version},"
        " Python 3.11 and CPU-only torch ([docs](https://studio-fug.github.io/yapnr/docs/"
        "containers.html)):",
        "",
        "```sh",
        f"docker pull {IMAGE}:{version}",
        f"docker pull {IMAGE}@{digest}   # the same image, pinned",
        f"gh attestation verify oci://{IMAGE}:{version} -R {repo}",
        "```",
        "",
        "**Wheels** (Python 3.11, one per platform with yapnr.rf's native FDTD library; on x86_64"
        " Linux, take torch from the CPU index):",
        "",
        "```sh",
    ]
    for path in sorted(wheels):
        lines += [
            f"pip install --extra-index-url {TORCH_CPU_INDEX} \\",
            f"  {download}/{os.path.basename(path)}",
        ]
    lines += [
        "```",
        "",
        "**Bazel** (`MODULE.bazel`):",
        "",
        "```starlark",
        f'bazel_dep(name = "yapnr", version = "{version}")',
        "archive_override(",
        '    module_name = "yapnr",',
        f'    urls = ["{download}/{os.path.basename(archive)}"],',
        f'    integrity = "{sri_sha256(archive)}",',
        f'    strip_prefix = "yapnr-{version}",',
        ")",
        "```",
        "",
        f"Every file below is listed in `SHA256SUMS` and attested: `gh attestation verify"
        f" <file> -R {repo}`. The wheel's version is `{pep440}`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--pep440", required=True)
    parser.add_argument("--digest", required=True, help="digest of the image index")
    parser.add_argument("--kicad-version", required=True)
    parser.add_argument("--archive", required=True, help="the source archive (for integrity)")
    parser.add_argument("--wheel", required=True, action="append", help="repeat per platform")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Studio-Fug/yapnr"))
    parser.add_argument("--upgrade-notes", help="repository path of the upgrade notes, if any")
    args = parser.parse_args(argv)
    sys.stdout.write(
        render(
            tag=args.tag,
            pep440=args.pep440,
            digest=args.digest,
            kicad_version=args.kicad_version,
            archive=args.archive,
            wheel=args.wheel,
            repo=args.repo,
            upgrade_notes=args.upgrade_notes,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
