"""The two stores of a backend (inputs and runs), as a directory or a Cloud Storage bucket.

Layout (docs/design/cloud-experiments.md, section 8)::

    inputs:  bundles/<sha256>.tar.gz
    runs:    campaigns/<cid>/{campaign.json, tasks.jsonl, task.py}
             campaigns/<cid>/submissions/<n>.json, <n>.indices
             campaigns/<cid>/tasks/<task key>/_DONE, <attempt>/...
             checkpoints/<cid>/<task key>/...
             control/frozen

Tasks only ever see the runs store as a directory (a FUSE mount on Batch); the submitter uses
``GcsStore`` through ``gcloud storage``.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from yapnr.exp.cloud import CloudError, Gcloud

FROZEN = "control/frozen"
ONLY_DONE = r"^(?!.*_DONE$).*$"  # an rsync exclude that keeps only the _DONE markers


class Store:
    def url(self, rel: str) -> str:
        raise NotImplementedError

    def exists(self, rel: str) -> bool:
        raise NotImplementedError

    def read_bytes(self, rel: str) -> bytes:
        raise NotImplementedError

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode()

    def upload(self, local: Path, rel: str, no_clobber: bool = False) -> None:
        raise NotImplementedError

    def write_bytes(self, rel: str, data: bytes, no_clobber: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="yapnr-store-") as tmp:
            path = Path(tmp) / Path(rel).name
            path.write_bytes(data)
            self.upload(path, rel, no_clobber=no_clobber)

    def write_text(self, rel: str, text: str, no_clobber: bool = False) -> None:
        self.write_bytes(rel, text.encode(), no_clobber=no_clobber)

    def delete(self, rel: str) -> None:
        raise NotImplementedError

    def sync_down(self, prefix: str, dest: Path, exclude: Optional[str] = None) -> None:
        """Copy everything under ``prefix`` into ``dest`` (``exclude``: a regex on the path)."""
        raise NotImplementedError

    def list(self, pattern: str) -> List[str]:
        """Store paths matching a glob (``*`` within one path component)."""
        raise NotImplementedError

    def read_lines(self, pattern: str) -> List[str]:
        """The lines of every small object matching ``pattern`` (markers, records)."""
        raise NotImplementedError


class LocalStore(Store):
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def url(self, rel: str) -> str:
        return str(self.path(rel))

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def read_bytes(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()

    def upload(self, local: Path, rel: str, no_clobber: bool = False) -> None:
        dest = self.path(rel)
        if no_clobber and dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(".%s.tmp-%d" % (dest.name, os.getpid()))
        shutil.copyfile(local, tmp)
        os.replace(tmp, dest)

    def delete(self, rel: str) -> None:
        try:
            self.path(rel).unlink()
        except FileNotFoundError:
            pass

    def list(self, pattern: str) -> List[str]:
        hits = glob.glob(str(self.root / pattern))
        return sorted(str(Path(h).relative_to(self.root)) for h in hits if Path(h).is_file())

    def read_lines(self, pattern: str) -> List[str]:
        lines: List[str] = []
        for rel in self.list(pattern):
            lines += self.read_text(rel).splitlines()
        return lines

    def sync_down(self, prefix: str, dest: Path, exclude: Optional[str] = None) -> None:
        src = self.path(prefix)
        if not src.is_dir():
            return
        pattern = re.compile(exclude) if exclude else None
        for path in sorted(src.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = str(path.relative_to(src))
            if pattern and pattern.search(rel):
                continue
            target = Path(dest) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


class GcsStore(Store):
    """A bucket, through ``gcloud storage`` (one call per operation, a timeout on each)."""

    def __init__(self, bucket: str, cloud: Gcloud):
        self.bucket = bucket
        self.cloud = cloud

    def url(self, rel: str) -> str:
        return "gs://%s/%s" % (self.bucket, rel)

    def exists(self, rel: str) -> bool:
        try:
            self.cloud.run(["storage", "objects", "describe", self.url(rel), "--format=json"])
        except CloudError as err:
            if err.not_found:
                return False
            raise
        return True

    def read_bytes(self, rel: str) -> bytes:
        return self.cloud.run(["storage", "cat", self.url(rel)]).stdout.encode()

    def upload(self, local: Path, rel: str, no_clobber: bool = False) -> None:
        args = ["storage", "cp", str(local), self.url(rel)]
        if no_clobber:
            args.append("--no-clobber")
        self.cloud.run(args, timeout=1800)

    def delete(self, rel: str) -> None:
        try:
            self.cloud.run(["storage", "rm", self.url(rel)])
        except CloudError as err:
            if not err.not_found:
                raise

    def list(self, pattern: str) -> List[str]:
        try:
            out = self.cloud.run(["storage", "ls", self.url(pattern)]).stdout
        except CloudError as err:
            if err.not_found or "matched no objects" in err.stderr:
                return []
            raise
        prefix = self.url("")
        return sorted(line[len(prefix) :] for line in out.splitlines() if line.startswith(prefix))

    def read_lines(self, pattern: str) -> List[str]:
        try:
            return self.cloud.run(["storage", "cat", self.url(pattern)]).stdout.splitlines()
        except CloudError as err:
            if err.not_found or "matched no objects" in err.stderr:
                return []
            raise

    def sync_down(self, prefix: str, dest: Path, exclude: Optional[str] = None) -> None:
        Path(dest).mkdir(parents=True, exist_ok=True)
        args = ["storage", "rsync", "--recursive", self.url(prefix.rstrip("/")), str(dest)]
        if exclude:
            args.append("--exclude=%s" % exclude)
        try:
            self.cloud.run(args, timeout=3600)
        except CloudError as err:
            if not err.not_found:
                raise
