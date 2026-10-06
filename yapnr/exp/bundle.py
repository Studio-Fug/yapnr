"""Content-addressed input bundles: deterministic ``.tar.gz`` archives named by their sha256.

A source bundle is ``git archive`` of one commit (optionally limited to the paths a kind needs);
with ``allow_dirty`` it is the tracked files as they are in the working tree, and every record
of the campaign says so. A data bundle is a directory or an existing ``.tar.gz``. Bundles are
built in the plan directory and uploaded once to the inputs store (``bundles/<sha256>.tar.gz``).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

GIT_TIMEOUT_S = 120


class BundleError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary: bool = False):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as err:
        detail = getattr(err, "stderr", b"") or b""
        raise BundleError(
            "git %s failed: %s" % (" ".join(args[:2]), detail.decode(errors="replace").strip())
        ) from err
    return result.stdout if binary else result.stdout.decode().strip()


def repo_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel"))


def resolve_source(repo: Path, source: str, paths: Optional[Sequence[str]]) -> Tuple[str, bool]:
    """``(commit, dirty)`` for a campaign's ``source`` (``HEAD`` or a commit)."""
    commit = _git(repo, "rev-parse", "--verify", "%s^{commit}" % source)
    dirty = False
    if source == "HEAD":
        status = _git(repo, "status", "--porcelain", "--untracked-files=no", "--", *(paths or []))
        dirty = bool(status)
    return commit, dirty


def present(repo: Path, commit: str, paths: Sequence[str]) -> List[str]:
    """The ``paths`` that exist in ``commit`` (a file or a directory), in order."""
    out = []
    for path in paths:
        try:
            _git(repo, "cat-file", "-e", "%s:%s" % (commit, path))
        except BundleError:
            continue
        out.append(path)
    return out


def _gzip(data: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=6) as gz:
        gz.write(data)
    return out.getvalue()


def _store(data: bytes, dest_dir: Path) -> Tuple[str, Path]:
    digest = hashlib.sha256(data).hexdigest()
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / ("%s.tar.gz" % digest)
    if not path.is_file():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return digest, path


def source_bundle(
    repo: Path,
    commit: str,
    paths: Optional[Sequence[str]],
    dest_dir: Path,
    from_worktree: bool = False,
) -> Tuple[str, Path]:
    """Archive ``paths`` (all when None) of ``commit`` into ``dest_dir``; ``(sha256, path)``."""
    if not from_worktree:
        raw = _git(repo, "archive", "--format=tar", commit, "--", *(paths or ["."]), binary=True)
        return _store(_gzip(raw), dest_dir)
    listing = _git(repo, "ls-files", "-z", "--", *(paths or ["."]), binary=True)
    files = sorted(p for p in listing.decode().split("\0") if p)
    return _store(_gzip(_tar_files(repo, files)), dest_dir)


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    return info


def _tar_files(root: Path, files: List[str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in files:
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue
            tar.add(str(path), arcname=name, recursive=False, filter=_normalize)
    return out.getvalue()


def data_bundle(path: Path, dest_dir: Path) -> Tuple[str, Path]:
    """A directory (archived deterministically) or an existing ``.tar.gz`` (taken as is)."""
    if path.is_file() and path.name.endswith((".tar.gz", ".tgz")):
        data = path.read_bytes()
        _check_members(data)
        return _store(data, dest_dir)
    if not path.is_dir():
        raise BundleError("input %s is neither a directory nor a .tar.gz" % path)
    files = sorted(
        str(p.relative_to(path)) for p in path.rglob("*") if p.is_file() and not p.is_symlink()
    )
    return _store(_gzip(_tar_files(path, files)), dest_dir)


def _check_members(data: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            unsafe_member(member)


def unsafe_member(member: tarfile.TarInfo) -> None:
    """Raise BundleError for members that could write outside the extraction directory."""
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise BundleError("unsafe path in bundle: %s" % name)
    if member.issym() or member.islnk():
        target = member.linkname
        if target.startswith("/") or ".." in Path(target).parts:
            raise BundleError("unsafe link in bundle: %s -> %s" % (name, target))
    if member.isdev() or member.isfifo():
        raise BundleError("device or fifo in bundle: %s" % name)


def extract(bundle: Path, sha256: str, dest: Path) -> None:
    """Verify ``bundle`` against ``sha256`` and extract it into ``dest``."""
    data = bundle.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise BundleError("bundle %s does not match its sha256" % bundle.name)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            unsafe_member(member)
        if hasattr(tarfile, "data_filter"):  # checked above; silences the 3.12+ warning
            tar.extractall(dest, members=members, filter="fully_trusted")
        else:
            tar.extractall(dest, members=members)


def copy_bundle(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
