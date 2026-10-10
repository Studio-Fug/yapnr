"""Demand-driven components queries, captured in the article for offline replay.

Our rules_atopile-derived picker matches captured public supplier facts locally.
No hosted Atopile API, credentials, or login are used. Loopback bearer headers are
ignored. Raw supplier responses and converted catalogs are pinned for replay.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from yapnr.frontends.atopile.picker import supplier

SOURCE = supplier.SOURCE
SCHEMA = "yapnr-components-discovery-v2"
RELATIVE = ".yapnr/parts/discovery.json"
MAX_RESPONSE = 4 * 1024 * 1024


def encoded(value):
    return supplier.encoded(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def read_public(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; yapnr component-catalog)",
            "Accept": "application/json",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("Supplier response exceeds its size limit")
        return json.loads(raw)


def fetch(method, path, body):
    try:
        return 200, supplier.discover(method, path, body, read_public)
    except urllib.error.HTTPError as error:
        return 502, {
            "detail": "Public supplier returned HTTP "
            + str(error.code)
            + "; no Atopile login is used"
        }
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return 502, {
            "detail": "Public supplier discovery failed; check connectivity, query support and supplier metadata"
        }


class Discovery:
    def __init__(self, project, *, online=False, request=fetch):
        self.root = Path(project).resolve()
        self.path = self.root / RELATIVE
        if self.path.exists() and (
            self.path.is_symlink() or not self.path.resolve().is_relative_to(self.root)
        ):
            raise ValueError("Component snapshot must stay inside the article")
        self.online, self.request = online, request
        self.lock = threading.RLock()
        self.failures = []
        self.remote_queries = 0
        self.replayed_queries = 0
        self._service_failure = None
        self.document = self.read()

    def read(self):
        if not self.path.is_file():
            return {"schema": SCHEMA, "source": SOURCE, "queries": {}}
        if self.path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Component snapshot exceeds its size limit")
        data = json.loads(self.path.read_text())
        # The removed hosted fallback produced empty snapshots on authentication failure.
        # Discard only those empty inputs; never silently rewrite captured evidence.
        if data.get("schema") == "yapnr-components-discovery-v1" and data.get("queries") == {}:
            return {"schema": SCHEMA, "source": SOURCE, "queries": {}}
        if (
            data.get("schema") != SCHEMA
            or data.get("source") != SOURCE
            or not isinstance(data.get("queries"), dict)
        ):
            raise ValueError("Unsupported component snapshot")
        for key, entry in data["queries"].items():
            content = {name: entry[name] for name in ("response", "catalogs", "sources")}
            if hashlib.sha256(encoded(content)).hexdigest() != entry["sha256"]:
                raise ValueError("Component snapshot response digest mismatch")
            if hashlib.sha256(encoded(entry["request"])).hexdigest() != key:
                raise ValueError("Component snapshot request digest mismatch")
        return data

    @contextlib.contextmanager
    def file_lock(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.parent.resolve().is_relative_to(self.root):
            raise ValueError("Component snapshot must stay inside the article")
        lock_path = self.path.parent / "discovery.lock"
        if lock_path.is_symlink():
            raise ValueError("Component snapshot lock must not be a symlink")
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self):
        with self.file_lock():
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.parent.resolve().is_relative_to(self.root):
            raise ValueError("Component snapshot must stay inside the article")
        raw = encoded(self.document)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("Component snapshot exceeds its size limit")
        # Refuse overlapping build updates rather than silently replacing another snapshot.
        current = self.read()
        for key, entry in current["queries"].items():
            existing = self.document["queries"].get(key)
            if existing is not None and existing != entry:
                raise ValueError("Component snapshot changed during discovery")
            self.document["queries"][key] = entry
        if len(encoded(self.document)) > 32 * 1024 * 1024:
            raise ValueError("Component snapshot exceeds its size limit")
        descriptor, name = tempfile.mkstemp(prefix="discovery-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded(self.document))
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def answer(self, method, path, body, local):
        status, response = local
        if status != 200 or not path.startswith(("/v0/component/", "/v0/query")):
            return local
        if path == "/v0/query":
            results = response.get("results", [])
            if not results or all(item.get("components") for item in results):
                return local
        elif response.get("components"):
            return local
        request = {"method": method, "path": path, "body": json.loads(body) if body else None}
        key = hashlib.sha256(encoded(request)).hexdigest()
        with self.lock:
            entry = self.document["queries"].get(key)
            if entry:
                self.replayed_queries += 1
                return 200, entry["response"]
            if not self.online:
                return local
            if self._service_failure:
                return self._service_failure
            missing = None
            discovery_body = body
            if path == "/v0/query":
                queries = request["body"].get("queries", [])
                missing = [
                    index for index, result in enumerate(results) if not result.get("components")
                ]
                discovery_body = encoded({"queries": [queries[index] for index in missing]})
            status, remote = self.request(method, path, discovery_body)
            catalogs = remote.pop("_catalogs", []) if isinstance(remote, dict) else []
            sources = remote.pop("_sources", []) if isinstance(remote, dict) else []
            self.remote_queries += 1
            batches = (
                remote.get("results", [])
                if isinstance(remote, dict) and path == "/v0/query"
                else [remote]
            )
            valid = isinstance(batches, list) and all(
                isinstance(item, dict)
                and isinstance(item.get("components"), list)
                and all(
                    isinstance(part, dict)
                    and type(part.get("lcsc")) is int
                    and part["lcsc"] > 0
                    and isinstance(part.get("attributes"), dict)
                    for part in item["components"]
                )
                for item in batches
            )
            if path == "/v0/query":
                valid = valid and len(batches) == len(missing)
            if status != 200 or not valid:
                failure = {
                    "status": status if status != 200 else 502,
                    "detail": (
                        remote.get("detail", "Component response has an invalid schema")
                        if isinstance(remote, dict)
                        else "Component response has an invalid schema"
                    ),
                }
                self.failures.append(failure)
                if status != 200 and failure["status"] in (401, 403, 502):
                    self._service_failure = (failure["status"], {"detail": failure["detail"]})
                return failure["status"], {"detail": failure["detail"]}
            if missing is not None:
                combined = list(results)
                for index, batch in zip(missing, batches):
                    combined[index] = batch
                remote["results"] = combined
            content = {"response": remote, "catalogs": catalogs, "sources": sources}
            self.document["queries"][key] = {
                "request": request,
                **content,
                "sha256": hashlib.sha256(encoded(content)).hexdigest(),
            }
            try:
                self.save()
            except (OSError, ValueError):
                self.document["queries"].pop(key, None)
                failure = {"status": 502, "detail": "Component snapshot could not be saved"}
                self.failures.append(failure)
                return 502, {"detail": failure["detail"]}
            return 200, remote
