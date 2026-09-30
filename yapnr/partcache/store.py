"""The part cache on disk: content-addressed blobs, part manifests, catalog entries, takedowns.

Layout of a cache root (created by ``Store.open(root, create=True)``)::

    <root>/yapnr-part-cache.json          {"schema": "yapnr-part-cache-v1"}
    <root>/blobs/sha256/ab/<sha256>       file contents, named by their sha256; immutable
    <root>/parts/ab/<id>.json             part manifests, named by the part id; immutable
    <root>/catalog/C<digits>.json         the current catalog entry of each LCSC id
    <root>/takedowns.jsonl                append-only log of taken-down parts, files and entries
    <root>/index.sqlite                   an index over the above; rebuilt by ``reindex``
    <root>/tmp/                           staging for atomic writes

The files are the source of truth; the SQLite index can always be rebuilt from them. Writes go
through a temporary file and an atomic rename, so a reader never sees half a blob or manifest.

A taken-down part keeps a line in ``takedowns.jsonl``: its manifest and every blob no other part
uses are removed, and an upload of the same id is refused from then on. The files it blocks
(by default every file no other part used) get a line each too, and are refused from then on
under any part name. A blob that no part uses is served by nobody (``read_published_blob``) and
removed by ``gc`` once it is older than the grace period the server gives an upload in progress.

Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional, Union

from yapnr.frontends.atopile.picker import catalog as catalog_mod
from yapnr.partcache import model

_INDEX_VERSION = 2


class NotFound(KeyError):
    pass


class TakenDown(Exception):
    """An upload of a part, file or catalog entry that a takedown blocked."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write(tmp_dir: Path, target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix=".w-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o644)
        os.replace(name, target)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


class Store:
    """A part cache root. Thread-safe; several processes may share one root."""

    def __init__(self, root: Path):
        self.root = root
        # One connection, shared by every thread under this lock (the HTTP server starts a
        # thread per request); SQLite's own locking covers other processes.
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # --- opening ---------------------------------------------------------------------------

    @classmethod
    def open(cls, root: "str | os.PathLike", create: bool = False) -> "Store":
        root = Path(root).expanduser()
        marker = root / "yapnr-part-cache.json"
        if not marker.is_file():
            if not create:
                raise NotFound(f"no part cache at {root} (create one with `yapnr part-cache init`)")
            if root.exists() and any(root.iterdir()):
                raise FileExistsError(f"{root} exists and is not an empty directory")
            for sub in ("blobs/sha256", "parts", "catalog", "tmp"):
                (root / sub).mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"schema": model.CACHE_SCHEMA, "created": utc_now()}, indent=2) + "\n",
                encoding="utf-8",
            )
        doc = json.loads(marker.read_text(encoding="utf-8"))
        if doc.get("schema") != model.CACHE_SCHEMA:
            raise ValueError(f"{marker}: not a {model.CACHE_SCHEMA} cache")
        store = cls(root)
        store._ensure_index()
        return store

    # --- paths ------------------------------------------------------------------------------

    def blob_path(self, sha: str) -> Path:
        if not model.is_sha256(sha):
            raise NotFound(sha)
        return self.root / "blobs" / "sha256" / sha[:2] / sha

    def manifest_path(self, pid: str) -> Path:
        if not model.is_sha256(pid):
            raise NotFound(pid)
        return self.root / "parts" / pid[:2] / f"{pid}.json"

    def catalog_path(self, lcsc: str) -> Path:
        return self.root / "catalog" / f"{catalog_mod.normalize_lcsc(lcsc)}.json"

    @property
    def _tmp(self) -> Path:
        path = self.root / "tmp"
        path.mkdir(exist_ok=True)
        return path

    # --- index ------------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                db = sqlite3.connect(
                    str(self.root / "index.sqlite"), timeout=30, check_same_thread=False
                )
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                self._conn = db
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _ensure_index(self) -> None:
        with self._lock:
            self._ensure_index_locked()

    def _ensure_index_locked(self) -> None:
        db = self._db()
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == _INDEX_VERSION:
            return
        with db:
            db.executescript(
                """
                DROP TABLE IF EXISTS parts;
                DROP TABLE IF EXISTS part_files;
                DROP TABLE IF EXISTS catalog;
                DROP TABLE IF EXISTS takedowns;
                CREATE TABLE parts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, lcsc TEXT, manufacturer TEXT,
                    mpn TEXT, mfr_key TEXT, uploaded_at TEXT, size INTEGER,
                    local_only INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX parts_lcsc ON parts (lcsc, uploaded_at);
                CREATE INDEX parts_mfr ON parts (mfr_key, uploaded_at);
                CREATE INDEX parts_name ON parts (name);
                CREATE TABLE part_files (part_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY (part_id, sha256));
                CREATE INDEX part_files_sha ON part_files (sha256);
                CREATE TABLE catalog (
                    lcsc TEXT PRIMARY KEY, kind TEXT, mfr_key TEXT, updated_at TEXT, doc TEXT);
                CREATE TABLE takedowns (kind TEXT, key TEXT, at TEXT, reason TEXT,
                    PRIMARY KEY (kind, key));
                """
            )
            db.execute(f"PRAGMA user_version={_INDEX_VERSION}")
        self.reindex()

    def reindex(self) -> Dict[str, int]:
        """Rebuild the index from the manifests, catalog entries and takedown log."""
        with self._lock:
            db = self._db()
            with db:
                db.execute("DELETE FROM parts")
                db.execute("DELETE FROM part_files")
                db.execute("DELETE FROM catalog")
                db.execute("DELETE FROM takedowns")
                for path in sorted((self.root / "parts").glob("*/*.json")):
                    self._index_part(db, json.loads(path.read_text(encoding="utf-8")))
                for path in sorted((self.root / "catalog").glob("C*.json")):
                    self._index_catalog(db, json.loads(path.read_text(encoding="utf-8")))
                for entry in self._takedown_log():
                    db.execute(
                        "INSERT OR REPLACE INTO takedowns VALUES (?, ?, ?, ?)",
                        (entry["kind"], entry["key"], entry["at"], entry.get("reason", "")),
                    )
            return self._stats()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return self._stats()

    def _stats(self) -> Dict[str, int]:
        db = self._db()

        def count(sql: str) -> int:
            return db.execute(sql).fetchone()[0]

        return {
            "parts": count("SELECT COUNT(*) FROM parts"),
            "catalog": count("SELECT COUNT(*) FROM catalog"),
            "takedowns": count("SELECT COUNT(*) FROM takedowns WHERE kind != 'blob'"),
            "blocked_files": count("SELECT COUNT(*) FROM takedowns WHERE kind = 'blob'"),
            "local_only": count("SELECT COUNT(*) FROM parts WHERE local_only = 1"),
        }

    @staticmethod
    def _mfr_key(manufacturer: Optional[str], mpn: Optional[str]) -> str:
        return f"{(manufacturer or '').strip().casefold()}\x1f{(mpn or '').strip().casefold()}"

    def _index_part(self, db: sqlite3.Connection, manifest: Dict[str, Any]) -> None:
        db.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest["id"],
                manifest["name"],
                manifest.get("lcsc"),
                manifest.get("manufacturer"),
                manifest.get("mpn"),
                self._mfr_key(manifest.get("manufacturer"), manifest.get("mpn")),
                (manifest.get("uploaded") or {}).get("at", ""),
                sum(f["size"] for f in manifest["files"]),
                1 if model.is_local_only(manifest) else 0,
            ),
        )
        db.execute("DELETE FROM part_files WHERE part_id = ?", (manifest["id"],))
        db.executemany(
            "INSERT OR IGNORE INTO part_files VALUES (?, ?)",
            [(manifest["id"], f["sha256"]) for f in manifest["files"]],
        )

    def _index_catalog(self, db: sqlite3.Connection, entry: Dict[str, Any]) -> None:
        part = entry["part"]
        db.execute(
            "INSERT OR REPLACE INTO catalog VALUES (?, ?, ?, ?, ?)",
            (
                part["lcsc"],
                part["kind"],
                self._mfr_key(part["manufacturer"], part["mpn"]),
                entry.get("updated_at", ""),
                json.dumps(entry, sort_keys=True),
            ),
        )

    def _takedown_log(self) -> Iterator[Dict[str, Any]]:
        path = self.root / "takedowns.jsonl"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield json.loads(line)

    def taken_down(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT at, reason FROM takedowns WHERE kind = ? AND key = ?", (kind, key))
                .fetchone()
            )
        return {"at": row["at"], "reason": row["reason"]} if row else None

    # --- blobs ------------------------------------------------------------------------------

    def has_blob(self, sha: str) -> bool:
        try:
            return self.blob_path(sha).is_file()
        except NotFound:
            return False

    def _refuse_blocked(self, sha: str) -> None:
        blocked = self.taken_down("blob", sha)
        if blocked:
            raise TakenDown(f"file {sha} was taken down on {blocked['at']}")

    def _keep(self, path: Path) -> bool:
        """Whether ``path`` exists; refreshes its time so ``gc`` gives it a new grace period."""
        try:
            os.utime(path)
            return True
        except FileNotFoundError:
            return False

    def put_blob(self, data: bytes, expected: Optional[str] = None) -> str:
        sha = model.sha256_bytes(data)
        if expected is not None and expected != sha:
            raise model.InvalidPart(f"blob content hashes to {sha}, not {expected}")
        self._refuse_blocked(sha)
        path = self.blob_path(sha)
        with self._lock:
            if self._keep(path):
                return sha
            _atomic_write(self._tmp, path, data)
        return sha

    def put_blob_stream(self, stream: BinaryIO, length: int, expected: str) -> str:
        """Store ``length`` bytes read from ``stream`` in chunks (the server's uploads)."""
        if not model.is_sha256(expected):
            raise model.InvalidPart(f"{expected!r} is not a sha256")
        self._refuse_blocked(expected)
        fd, name = tempfile.mkstemp(dir=self._tmp, prefix=".u-")
        staged: Optional[str] = name
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(1 << 20, remaining))
                    if not chunk:
                        raise model.InvalidPart("the upload ended before its Content-Length")
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            if sha != expected:
                raise model.InvalidPart(f"blob content hashes to {sha}, not {expected}")
            path = self.blob_path(sha)
            with self._lock:
                if not self._keep(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(name, 0o644)
                    os.replace(name, path)
                    staged = None
            return sha
        finally:
            if staged is not None:
                try:
                    os.unlink(staged)
                except OSError:
                    pass

    def read_blob(self, sha: str) -> bytes:
        try:
            return self.blob_path(sha).read_bytes()
        except FileNotFoundError as err:
            raise NotFound(sha) from err

    def is_used(self, sha: str) -> bool:
        """Whether a stored part uses blob ``sha``."""
        with self._lock:
            row = (
                self._db()
                .execute("SELECT 1 FROM part_files WHERE sha256 = ? LIMIT 1", (sha,))
                .fetchone()
            )
        return row is not None

    def read_published_blob(self, sha: str) -> bytes:
        """A file that a stored part uses (what a server serves); NotFound for any other blob."""
        if not model.is_sha256(sha) or not self.is_used(sha):
            raise NotFound(sha)
        return self.read_blob(sha)

    # --- parts ------------------------------------------------------------------------------

    def put_part(
        self,
        request: Dict[str, Any],
        uploaded_by: str = "local",
        max_file_bytes: int = model.DEFAULT_MAX_FILE_BYTES,
    ) -> "tuple[Dict[str, Any], bool]":
        """Store a part whose blobs are already present; returns (manifest, created).

        The blobs are checked (present, sizes, content types, not taken down) under the store's
        lock, which ``gc`` and takedowns take too, so a part never names a missing file.
        """
        request = model.check_manifest_request(request, max_file_bytes=max_file_bytes)
        pid = model.part_id(request["name"], request["files"])
        with self._lock:
            takedown = self.taken_down("part", pid)
            if takedown:
                raise TakenDown(f"part {pid} was taken down on {takedown['at']}")
            existing = self.manifest_path(pid)
            if existing.is_file():
                return json.loads(existing.read_text(encoding="utf-8")), False
            ato_text = ""
            for entry in request["files"]:
                self._refuse_blocked(entry["sha256"])
                path = self.blob_path(entry["sha256"])
                if not path.is_file():
                    raise model.InvalidPart(f"blob {entry['sha256']} ({entry['path']}) is missing")
                if path.stat().st_size != entry["size"]:
                    raise model.InvalidPart(f"{entry['path']}: size does not match its blob")
                with open(path, "rb") as handle:
                    data = handle.read(
                        -1 if model.needs_full_content(entry["path"]) else model.CONTENT_HEAD_BYTES
                    )
                model.check_file_content(entry["path"], data)
                if entry["path"] == f"{request['name']}.ato":
                    ato_text = data.decode("utf-8")
            manifest = model.build_manifest(
                request, ato_text, uploaded={"at": utc_now(), "by": uploaded_by}
            )
            data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
            _atomic_write(self._tmp, existing, data)
            db = self._db()
            with db:
                self._index_part(db, manifest)
            return manifest, True

    def manifest(self, pid: str) -> Dict[str, Any]:
        try:
            return json.loads(self.manifest_path(pid).read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise NotFound(pid) from err

    def find_parts(
        self,
        lcsc: Optional[str] = None,
        manufacturer: Optional[str] = None,
        mpn: Optional[str] = None,
        name: Optional[str] = None,
        text: Optional[str] = None,
        latest_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Part summaries matching every given filter, newest first."""
        where, args = [], []
        if lcsc:
            where.append("lcsc = ?")
            args.append(catalog_mod.normalize_lcsc(lcsc))
        if mpn:
            where.append("mfr_key = ?" if manufacturer is not None else "LOWER(mpn) = ?")
            args.append(
                self._mfr_key(manufacturer, mpn) if manufacturer is not None else mpn.casefold()
            )
        elif manufacturer:
            where.append("LOWER(manufacturer) = ?")
            args.append(manufacturer.casefold())
        if name:
            where.append("name = ?")
            args.append(name)
        if text:
            where.append("(name LIKE ? OR mpn LIKE ? OR manufacturer LIKE ? OR lcsc LIKE ?)")
            args.extend([f"%{text}%"] * 4)
        sql = "SELECT id, uploaded_at, name FROM parts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY uploaded_at DESC, id"
        with self._lock:
            rows = self._db().execute(sql, args).fetchall()
        out, seen = [], set()
        for row in rows:
            if latest_only and row["name"] in seen:
                continue
            seen.add(row["name"])
            out.append(row["id"])
        return [model.summary(self.manifest(pid)) for pid in out[offset : offset + limit]]

    def current(self, lcsc: str) -> Dict[str, Any]:
        """The newest part manifest for an LCSC id."""
        found = self.find_parts(lcsc=lcsc, limit=1)
        if not found:
            raise NotFound(lcsc)
        return self.manifest(found[0]["id"])

    def delete_part(
        self, pid: str, reason: str, block: Union[str, List[str]] = "unshared"
    ) -> Dict[str, Any]:
        """Take a part down: remove its manifest and unshared blobs; refuse it from now on.

        ``block`` names the files that are refused from now on under any part name:
        ``"unshared"`` (every file of the part that no other part uses; the default), ``"none"``,
        or a list of the part's file names, each of which no other part may use (take those
        parts down first).
        """
        with self._lock:
            manifest = self.manifest(pid)
            by_path = {f["path"]: f["sha256"] for f in manifest["files"]}
            db = self._db()
            others = {
                row[0]
                for row in db.execute(
                    "SELECT sha256 FROM part_files WHERE part_id != ?", (pid,)
                ).fetchall()
            }
            unshared = {sha for sha in by_path.values() if sha not in others}
            if block == "unshared":
                blocked = sorted(unshared)
            elif block == "none":
                blocked = []
            elif isinstance(block, list) and all(isinstance(p, str) for p in block):
                unknown = sorted(set(block) - set(by_path))
                if unknown:
                    raise model.InvalidPart(f"part {pid} has no files {unknown}")
                shared = sorted(p for p in block if by_path[p] not in unshared)
                if shared:
                    raise model.InvalidPart(
                        f"{shared}: other parts use these files too; take them down first"
                    )
                blocked = sorted({by_path[p] for p in block})
            else:
                raise model.InvalidPart("block must be 'unshared', 'none' or a list of file names")
            self._log_takedown("part", pid, reason)
            for sha in blocked:
                self._log_takedown("blob", sha, f"{reason} (a file of part {pid})")
            self.manifest_path(pid).unlink()
            with db:
                db.execute("DELETE FROM parts WHERE id = ?", (pid,))
                db.execute("DELETE FROM part_files WHERE part_id = ?", (pid,))
                db.execute(
                    "INSERT OR REPLACE INTO takedowns VALUES ('part', ?, ?, ?)",
                    (pid, utc_now(), reason),
                )
                db.executemany(
                    "INSERT OR REPLACE INTO takedowns VALUES ('blob', ?, ?, ?)",
                    [(sha, utc_now(), reason) for sha in blocked],
                )
            removed = self.gc(candidates=list(by_path.values()))
            return {"id": pid, "removed_blobs": removed, "blocked_files": blocked}

    def _log_takedown(self, kind: str, key: str, reason: str) -> None:
        line = json.dumps({"kind": kind, "key": key, "at": utc_now(), "reason": reason})
        with open(self.root / "takedowns.jsonl", "a", encoding="utf-8") as out:
            out.write(line + "\n")
            out.flush()
            os.fsync(out.fileno())

    def referenced_blobs(self) -> set:
        refs = set()
        for path in (self.root / "parts").glob("*/*.json"):
            for entry in json.loads(path.read_text(encoding="utf-8"))["files"]:
                refs.add(entry["sha256"])
        return refs

    def gc(self, candidates: Optional[Iterable[str]] = None, min_age: float = 0.0) -> int:
        """Delete blobs no manifest references (only ``candidates`` when given).

        ``min_age`` (seconds) spares blobs written or re-uploaded more recently: the server
        collects orphans with a grace period, so an upload between its files and its part is
        not cut short.
        """
        with self._lock:
            refs = self.referenced_blobs()
            if candidates is None:
                candidates = [p.name for p in (self.root / "blobs" / "sha256").glob("*/*")]
            now = time.time()
            removed = 0
            for sha in set(candidates) - refs:
                try:
                    path = self.blob_path(sha)
                    if min_age > 0 and now - path.stat().st_mtime < min_age:
                        continue
                    path.unlink()
                    removed += 1
                except (FileNotFoundError, NotFound):
                    pass
            return removed

    # --- distribution -----------------------------------------------------------------------

    def local_only_parts(self) -> List[str]:
        """The ids of the stored parts marked ``licence.distribution: local-only``."""
        with self._lock:
            rows = self._db().execute("SELECT id FROM parts WHERE local_only = 1 ORDER BY id")
            return [row[0] for row in rows.fetchall()]

    def set_distribution(self, pid: str, distribution: str) -> Dict[str, Any]:
        """Correct a part's ``licence.distribution`` (the licence is not part of its id)."""
        if distribution not in model.DISTRIBUTIONS:
            raise model.InvalidPart(f"distribution must be one of {', '.join(model.DISTRIBUTIONS)}")
        with self._lock:
            manifest = self.manifest(pid)
            licence = dict(manifest.get("licence") or {})
            licence["distribution"] = distribution
            manifest["licence"] = licence
            model.check_manifest(manifest)
            data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
            _atomic_write(self._tmp, self.manifest_path(pid), data)
            db = self._db()
            with db:
                self._index_part(db, manifest)
            return manifest

    def verify(self) -> List[str]:
        """Every problem found by re-hashing blobs and re-checking manifests."""
        problems = []
        for path in sorted((self.root / "blobs" / "sha256").glob("*/*")):
            if model.sha256_bytes(path.read_bytes()) != path.name:
                problems.append(f"blob {path.name}: content does not match its name")
        for path in sorted((self.root / "parts").glob("*/*.json")):
            try:
                manifest = model.check_manifest(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError) as err:
                problems.append(f"manifest {path.name}: {err}")
                continue
            if path.stem != manifest["id"]:
                problems.append(f"manifest {path.name}: named after another id")
            for entry in manifest["files"]:
                if not self.has_blob(entry["sha256"]):
                    problems.append(f"part {manifest['id']}: blob of {entry['path']} is missing")
                    continue
                try:
                    model.check_file_content(entry["path"], self.read_blob(entry["sha256"]))
                except model.InvalidPart as err:
                    problems.append(f"part {manifest['id']}: {err}")
                if self.taken_down("blob", entry["sha256"]):
                    problems.append(f"part {manifest['id']}: {entry['path']} was taken down")
        return problems

    # --- catalog ----------------------------------------------------------------------------

    def put_catalog(
        self, part: Dict[str, Any], provenance: Dict[str, Any], uploaded_by: str = "local"
    ) -> Dict[str, Any]:
        """Store (or replace) the catalog entry of one LCSC id."""
        doc = catalog_mod.validate(
            {"schema": catalog_mod.SCHEMA, "provenance": provenance, "parts": [part]},
            where="catalog entry",
        )
        clean = doc["parts"][0]
        with self._lock:
            takedown = self.taken_down("catalog", clean["lcsc"])
            if takedown:
                raise TakenDown(f"catalog entry {clean['lcsc']} was taken down on {takedown['at']}")
            entry = {
                "part": clean,
                "provenance": doc["provenance"],
                "updated_at": utc_now(),
                "updated_by": uploaded_by,
            }
            data = json.dumps(entry, indent=2, sort_keys=True).encode() + b"\n"
            _atomic_write(self._tmp, self.catalog_path(clean["lcsc"]), data)
            db = self._db()
            with db:
                self._index_catalog(db, entry)
            return entry

    def catalog_entry(self, lcsc: str) -> Dict[str, Any]:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT doc FROM catalog WHERE lcsc = ?", (catalog_mod.normalize_lcsc(lcsc),)
                )
                .fetchone()
            )
        if row is None:
            raise NotFound(lcsc)
        return json.loads(row["doc"])

    def delete_catalog(self, lcsc: str, reason: str) -> None:
        lcsc = catalog_mod.normalize_lcsc(lcsc)
        with self._lock:
            path = self.catalog_path(lcsc)
            if not path.is_file():
                raise NotFound(lcsc)
            self._log_takedown("catalog", lcsc, reason)
            path.unlink()
            db = self._db()
            with db:
                db.execute("DELETE FROM catalog WHERE lcsc = ?", (lcsc,))
                db.execute(
                    "INSERT OR REPLACE INTO takedowns VALUES ('catalog', ?, ?, ?)",
                    (lcsc, utc_now(), reason),
                )

    def catalog(self, lcsc: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """A ``yapnr-picker-catalog-v1`` document of the catalog entries (all, or ``lcsc``)."""
        with self._lock:
            db = self._db()
            if lcsc is None:
                rows = db.execute("SELECT doc FROM catalog ORDER BY lcsc").fetchall()
            else:
                wanted = sorted({catalog_mod.normalize_lcsc(x) for x in lcsc})
                rows = [
                    row
                    for key in wanted
                    for row in db.execute("SELECT doc FROM catalog WHERE lcsc = ?", (key,))
                ]
        entries = [json.loads(row["doc"]) for row in rows]
        sources = sorted({e["provenance"].get("source", "") for e in entries} - {""})
        return {
            "schema": catalog_mod.SCHEMA,
            "provenance": {
                "source": "yapnr part cache" + (f" ({'; '.join(sources)})" if sources else ""),
                "retrieved": utc_now()[:10],
            },
            "parts": [e["part"] for e in entries],
        }
