"""The part cache on disk: content-addressed blobs, part manifests, catalog entries, takedowns.

Layout of a cache root (created by ``Store.open(root, create=True)``)::

    <root>/yapnr-part-cache.json          {"schema": "yapnr-part-cache-v1"}
    <root>/blobs/sha256/ab/<sha256>       file contents, named by their sha256; immutable
    <root>/parts/ab/<id>.json             part manifests, named by the part id; immutable
    <root>/catalog/C<digits>.json         the current catalog entry of each LCSC id
    <root>/takedowns.jsonl                append-only log of deleted parts and catalog entries
    <root>/index.sqlite                   an index over the above; rebuilt by ``reindex``
    <root>/tmp/                           staging for atomic writes

The files are the source of truth; the SQLite index can always be rebuilt from them. Writes go
through a temporary file and an atomic rename, so a reader never sees half a blob or manifest.
A deleted (taken down) part keeps a line in ``takedowns.jsonl``: its manifest and every blob no
other part uses are removed, and an upload of the same id is refused from then on.

Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from yapnr.frontends.atopile.picker import catalog as catalog_mod
from yapnr.partcache import model

_INDEX_VERSION = 1


class NotFound(KeyError):
    pass


class TakenDown(Exception):
    """An upload of a part or catalog entry that was deleted by a takedown."""


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
                DROP TABLE IF EXISTS catalog;
                DROP TABLE IF EXISTS takedowns;
                CREATE TABLE parts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, lcsc TEXT, manufacturer TEXT,
                    mpn TEXT, mfr_key TEXT, uploaded_at TEXT, size INTEGER);
                CREATE INDEX parts_lcsc ON parts (lcsc, uploaded_at);
                CREATE INDEX parts_mfr ON parts (mfr_key, uploaded_at);
                CREATE INDEX parts_name ON parts (name);
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
        return {
            "parts": db.execute("SELECT COUNT(*) FROM parts").fetchone()[0],
            "catalog": db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0],
            "takedowns": db.execute("SELECT COUNT(*) FROM takedowns").fetchone()[0],
        }

    @staticmethod
    def _mfr_key(manufacturer: Optional[str], mpn: Optional[str]) -> str:
        return f"{(manufacturer or '').strip().casefold()}\x1f{(mpn or '').strip().casefold()}"

    def _index_part(self, db: sqlite3.Connection, manifest: Dict[str, Any]) -> None:
        db.execute(
            "INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest["id"],
                manifest["name"],
                manifest.get("lcsc"),
                manifest.get("manufacturer"),
                manifest.get("mpn"),
                self._mfr_key(manifest.get("manufacturer"), manifest.get("mpn")),
                (manifest.get("uploaded") or {}).get("at", ""),
                sum(f["size"] for f in manifest["files"]),
            ),
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

    def put_blob(self, data: bytes, expected: Optional[str] = None) -> str:
        sha = model.sha256_bytes(data)
        if expected is not None and expected != sha:
            raise model.InvalidPart(f"blob content hashes to {sha}, not {expected}")
        path = self.blob_path(sha)
        if not path.is_file():
            _atomic_write(self._tmp, path, data)
        return sha

    def read_blob(self, sha: str) -> bytes:
        try:
            return self.blob_path(sha).read_bytes()
        except FileNotFoundError as err:
            raise NotFound(sha) from err

    # --- parts ------------------------------------------------------------------------------

    def put_part(
        self,
        request: Dict[str, Any],
        uploaded_by: str = "local",
        max_file_bytes: int = model.DEFAULT_MAX_FILE_BYTES,
    ) -> "tuple[Dict[str, Any], bool]":
        """Store a part whose blobs are already present; returns (manifest, created)."""
        request = model.check_manifest_request(request, max_file_bytes=max_file_bytes)
        for entry in request["files"]:
            path = self.blob_path(entry["sha256"])
            if not path.is_file():
                raise model.InvalidPart(f"blob {entry['sha256']} ({entry['path']}) is missing")
            if path.stat().st_size != entry["size"]:
                raise model.InvalidPart(f"{entry['path']}: size does not match its blob")
        ato_sha = next(
            f["sha256"] for f in request["files"] if f["path"] == f"{request['name']}.ato"
        )
        try:
            ato_text = self.read_blob(ato_sha).decode("utf-8")
        except UnicodeDecodeError as err:
            raise model.InvalidPart(f"{request['name']}.ato is not UTF-8 text") from err
        pid = model.part_id(request["name"], request["files"])
        with self._lock:
            takedown = self.taken_down("part", pid)
            if takedown:
                raise TakenDown(f"part {pid} was taken down on {takedown['at']}")
            existing = self.manifest_path(pid)
            if existing.is_file():
                return json.loads(existing.read_text(encoding="utf-8")), False
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

    def delete_part(self, pid: str, reason: str) -> Dict[str, Any]:
        """Take a part down: remove its manifest and unshared blobs; refuse it from now on."""
        with self._lock:
            manifest = self.manifest(pid)
            self._log_takedown("part", pid, reason)
            self.manifest_path(pid).unlink()
            db = self._db()
            with db:
                db.execute("DELETE FROM parts WHERE id = ?", (pid,))
                db.execute(
                    "INSERT OR REPLACE INTO takedowns VALUES ('part', ?, ?, ?)",
                    (pid, utc_now(), reason),
                )
            removed = self.gc(candidates=[f["sha256"] for f in manifest["files"]])
            return {"id": pid, "removed_blobs": removed}

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

    def gc(self, candidates: Optional[Iterable[str]] = None) -> int:
        """Delete blobs no manifest references (only ``candidates`` when given)."""
        with self._lock:
            refs = self.referenced_blobs()
            if candidates is None:
                candidates = [p.name for p in (self.root / "blobs" / "sha256").glob("*/*")]
            removed = 0
            for sha in set(candidates) - refs:
                try:
                    self.blob_path(sha).unlink()
                    removed += 1
                except (FileNotFoundError, NotFound):
                    pass
            return removed

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
