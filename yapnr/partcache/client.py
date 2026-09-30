"""Clients of the part cache: a local cache directory, or a cache server over HTTP.

``open_cache(location)`` picks the client from a location: an ``http://`` or ``https://`` URL
is a server, anything else a directory. Without a location it is ``$YAPNR_PART_CACHE``, and
without that the default local cache (``default_root()``).

Both clients offer the same calls: look parts up, fetch manifests and files, read the catalog,
upload a part directory, and take parts down. ``materialize`` writes a part into a project
after checking that the manifest is the one the id names and every file against its sha256, so
a cache (or the network) cannot put anything into a build that the project's parts lock did not
name.

Stdlib only. Tokens are never printed. ``$YAPNR_PART_CACHE_TOKEN`` (write or admin scope) is
sent with writes only; ``$YAPNR_PART_CACHE_READ_TOKEN`` with reads, for a server that keeps its
reads private. A token goes only to the server named (redirects are refused, not followed) and
only over ``https://``, or ``http://`` to a loopback address.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from yapnr.partcache import model
from yapnr.partcache.store import NotFound, Store

ENV_LOCATION = "YAPNR_PART_CACHE"
ENV_TOKEN = "YAPNR_PART_CACHE_TOKEN"
ENV_READ_TOKEN = "YAPNR_PART_CACHE_READ_TOKEN"
HTTP_TIMEOUT = 120.0


def default_root() -> Path:
    """``$XDG_DATA_HOME/yapnr/part-cache`` (``~/.local/share/yapnr/part-cache`` by default)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "yapnr" / "part-cache"


class CacheError(RuntimeError):
    pass


class PartCache:
    """The calls every part cache client offers."""

    location: str

    def manifest(self, part_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def blob(self, sha: str) -> bytes:
        raise NotImplementedError

    def find(self, **filters: Any) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def current(self, lcsc: str) -> Dict[str, Any]:
        raise NotImplementedError

    def catalog(self, lcsc: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def upload_part(self, request: Dict[str, Any], blobs: Dict[str, bytes]) -> Dict[str, Any]:
        raise NotImplementedError

    def put_catalog(self, part: Dict[str, Any], provenance: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def delete_part(self, part_id: str, reason: str, block: Any = "unshared") -> Dict[str, Any]:
        raise NotImplementedError


class LocalPartCache(PartCache):
    """A cache directory used in place, without a server."""

    def __init__(self, root: "str | os.PathLike", create: bool = False):
        self.store = Store.open(root, create=create)
        self.location = str(self.store.root)

    def manifest(self, part_id: str) -> Dict[str, Any]:
        return self.store.manifest(part_id)

    def blob(self, sha: str) -> bytes:
        return self.store.read_blob(sha)

    def find(self, **filters: Any) -> List[Dict[str, Any]]:
        return self.store.find_parts(**filters)

    def current(self, lcsc: str) -> Dict[str, Any]:
        return self.store.current(lcsc)

    def catalog(self, lcsc: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        return self.store.catalog(lcsc)

    def upload_part(self, request: Dict[str, Any], blobs: Dict[str, bytes]) -> Dict[str, Any]:
        for sha, data in blobs.items():
            self.store.put_blob(data, expected=sha)
        return self.store.put_part(request, uploaded_by="local")[0]

    def local_only_parts(self) -> List[str]:
        return self.store.local_only_parts()

    def put_catalog(self, part: Dict[str, Any], provenance: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.put_catalog(part, provenance)

    def delete_part(self, part_id: str, reason: str, block: Any = "unshared") -> Dict[str, Any]:
        return self.store.delete_part(part_id, reason, block=block)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a token must reach the server it was given for and no other."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


_OPENER = urllib.request.build_opener(_NoRedirects)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class HttpPartCache(PartCache):
    """A cache server. Downloaded files are kept in a local content-addressed directory."""

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        blob_dir: Optional[Path] = None,
        read_token: Optional[str] = None,
    ):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise CacheError(f"not a cache server URL: {url!r}")
        if parts.username or parts.password:
            raise CacheError("put the token in $YAPNR_PART_CACHE_TOKEN, not in the URL")
        if parts.query or parts.fragment:
            raise CacheError(f"a cache server URL has no query or fragment: {url!r}")
        self.base = url.rstrip("/")
        self.location = self.base
        self.token = token if token is not None else os.environ.get(ENV_TOKEN) or None
        self.read_token = (
            read_token if read_token is not None else os.environ.get(ENV_READ_TOKEN) or None
        )
        self.blob_dir = blob_dir
        # A token crosses the network only encrypted, or not at all.
        self._token_ok = parts.scheme == "https" or _is_loopback(parts.hostname)

    def _request(
        self, method: str, path: str, body: Optional[bytes] = None, ctype: str = "application/json"
    ) -> bytes:
        request = urllib.request.Request(self.base + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", ctype)
        token = self.read_token if method == "GET" else self.token
        if token:
            if not self._token_ok:
                raise CacheError(
                    f"refusing to send a token to {self.base} over plain http; use https"
                )
            # Not copied onto a redirected request (and redirects are refused anyway).
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read()).get("detail", "")
            except (ValueError, AttributeError):
                detail = ""
            if err.code == 404:
                raise NotFound(path) from err
            if 300 <= err.code < 400:
                raise CacheError(
                    f"{method} {path}: the server redirected (HTTP {err.code}); a part cache URL"
                    " must name the server itself"
                ) from err
            if err.code == 401 and method == "GET" and not token:
                detail = f"{detail} (this server keeps reads private: set ${ENV_READ_TOKEN})"
            raise CacheError(f"{method} {path}: HTTP {err.code} {detail}".strip()) from err
        except urllib.error.URLError as err:
            raise CacheError(f"{method} {self.base}{path}: {err.reason}") from err

    def _json(self, method: str, path: str, doc: Any = None) -> Any:
        body = None if doc is None else json.dumps(doc).encode()
        return json.loads(self._request(method, path, body))

    def manifest(self, part_id: str) -> Dict[str, Any]:
        if not model.is_sha256(part_id):
            raise NotFound(part_id)
        return self._json("GET", f"/v1/parts/{part_id}")

    def blob(self, sha: str) -> bytes:
        if not model.is_sha256(sha):
            raise NotFound(sha)
        local = self.blob_dir / sha[:2] / sha if self.blob_dir else None
        if local is not None and local.is_file():
            data = local.read_bytes()
            if model.sha256_bytes(data) == sha:
                return data
        data = self._request("GET", f"/v1/blobs/{sha}")
        if model.sha256_bytes(data) != sha:
            raise CacheError(f"blob {sha}: the server sent other content")
        if local is not None:
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, local)
        return data

    def find(self, **filters: Any) -> List[Dict[str, Any]]:
        names = {"text": "q", "latest_only": "all_versions"}
        query = {}
        for key, value in filters.items():
            if value is None:
                continue
            if key == "latest_only":
                if not value:
                    query["all_versions"] = "1"
                continue
            query[names.get(key, key)] = str(value)
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        return self._json("GET", f"/v1/parts{suffix}")["parts"]

    def current(self, lcsc: str) -> Dict[str, Any]:
        return self._json("GET", f"/v1/lcsc/{urllib.parse.quote(lcsc)}")

    def catalog(self, lcsc: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        suffix = ""
        if lcsc is not None:
            suffix = "?" + urllib.parse.urlencode({"lcsc": ",".join(sorted(set(lcsc)))})
        return self._json("GET", f"/v1/catalog{suffix}")

    def upload_part(self, request: Dict[str, Any], blobs: Dict[str, bytes]) -> Dict[str, Any]:
        if (request.get("licence") or {}).get("distribution") == model.LOCAL_ONLY:
            raise CacheError(f"{request.get('name')}: local-only parts are never uploaded")
        for sha, data in blobs.items():
            self._request("PUT", f"/v1/blobs/{sha}", data, "application/octet-stream")
        return self._json("POST", "/v1/parts", request)

    def put_catalog(self, part: Dict[str, Any], provenance: Dict[str, Any]) -> Dict[str, Any]:
        lcsc = urllib.parse.quote(str(part.get("lcsc", "")))
        return self._json("PUT", f"/v1/catalog/{lcsc}", {"part": part, "provenance": provenance})

    def delete_part(self, part_id: str, reason: str, block: Any = "unshared") -> Dict[str, Any]:
        if not model.is_sha256(part_id):
            raise NotFound(part_id)
        return self._json("DELETE", f"/v1/parts/{part_id}", {"reason": reason, "block": block})


def open_cache(location: "str | os.PathLike | None" = None, create: bool = False) -> PartCache:
    """The cache client for a location (a URL or a directory); see the module docstring."""
    where = str(location) if location else os.environ.get(ENV_LOCATION) or str(default_root())
    if where.startswith(("http://", "https://")):
        cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
        return HttpPartCache(where, blob_dir=Path(cache_home) / "yapnr" / "part-cache-blobs")
    return LocalPartCache(where, create=create)


# --- part directories -------------------------------------------------------------------------


def read_part_dir(
    part_dir: Path, provenance: Dict[str, str], licence: Dict[str, str]
) -> "tuple[Dict[str, Any], Dict[str, bytes]]":
    """The upload request and file contents for an atopile part directory."""
    part_dir = Path(part_dir)
    files, blobs = [], {}
    for path in sorted(p for p in part_dir.iterdir()):
        if path.name.startswith(".") or path.name == "__pycache__":
            continue
        if not path.is_file() or path.is_symlink():
            raise model.InvalidPart(f"{path}: parts hold plain files only")
        model.check_file_path(path.name)
        data = path.read_bytes()
        sha = model.sha256_bytes(data)
        files.append({"path": path.name, "sha256": sha, "size": len(data)})
        blobs[sha] = data
    request = {
        "schema": model.PART_SCHEMA,
        "name": part_dir.name,
        "files": files,
        "provenance": provenance,
        "licence": licence,
    }
    model.check_manifest_request(request, max_file_bytes=1 << 62)
    return request, blobs


def dir_part_id(part_dir: Path) -> str:
    """The content id a part directory would get in the cache (no upload)."""
    files = []
    for path in sorted(Path(part_dir).iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        data = path.read_bytes()
        files.append({"path": path.name, "sha256": model.sha256_bytes(data), "size": len(data)})
    return model.part_id(Path(part_dir).name, files)


def materialize(cache: PartCache, part_id: str, parts_dir: Path, replace: bool = False) -> Path:
    """Write part ``part_id`` to ``parts_dir/<name>/`` after verifying every file.

    An existing directory with exactly these files is left alone. One with other content is an
    error unless ``replace``, which rewrites it.
    """
    manifest = model.check_manifest(cache.manifest(part_id))
    if manifest["id"] != part_id:
        # check_manifest proves the manifest matches its own id; this, that it is the part asked
        # for, so a cache cannot substitute another (self-consistent) part.
        raise CacheError(f"asked for part {part_id}, the cache returned {manifest['id']}")
    target = Path(parts_dir) / manifest["name"]
    if target.exists():
        if target.is_dir() and not target.is_symlink() and dir_part_id(target) == part_id:
            return target
        if not replace:
            raise CacheError(f"{target} exists with other content (use --replace to overwrite)")
        shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
    staging = Path(parts_dir) / f".{manifest['name']}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for entry in manifest["files"]:
            data = cache.blob(entry["sha256"])
            if model.sha256_bytes(data) != entry["sha256"] or len(data) != entry["size"]:
                raise CacheError(f"{manifest['name']}/{entry['path']}: content does not match")
            (staging / model.check_file_path(entry["path"])).write_bytes(data)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
