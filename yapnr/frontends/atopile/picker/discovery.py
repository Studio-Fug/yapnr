"""Demand-driven components queries, captured in the article for offline replay.

Credentials are read from the operator environment or native Atopile user session;
loopback bearer headers are never forwarded or recorded. Responses remain in the
pinned upstream wire format, without invented electrical attributes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = "https://legacy.atopileapi.com"
SCHEMA = "yapnr-components-discovery-v1"
RELATIVE = ".yapnr/parts/discovery.json"
TOKEN_ENV = "YAPNR_COMPONENTS_API_TOKEN"
MAX_RESPONSE = 4 * 1024 * 1024


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def token(python):
    value = os.environ.get(TOKEN_ENV)
    if value:
        return value
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from atopile.auth.session import get_stored_access_token; print(get_stored_access_token() or '')",
            ],
            capture_output=True,
            timeout=15,
            check=True,
        )
        if len(result.stdout) > 16384:
            return None
        return result.stdout.decode().strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch(method, path, body, bearer):
    headers = {"User-Agent": "yapnr-components-discovery", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(SOURCE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return 502, {"detail": "Component response exceeds its size limit"}
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return error.code, {
                "detail": "Component service sign-in required: run yapnr atopile auth login "
                "on the execution host, or configure " + TOKEN_ENV + " outside the article."
            }
        return 502, {"detail": "Component service returned HTTP " + str(error.code)}
    except (OSError, ValueError, urllib.error.URLError):
        return 502, {"detail": "Component service request failed; check connectivity and sign-in"}


class Discovery:
    def __init__(self, project, *, online=False, python=None, request=fetch):
        self.root = Path(project).resolve()
        self.path = self.root / RELATIVE
        if self.path.exists() and (
            self.path.is_symlink() or not self.path.resolve().is_relative_to(self.root)
        ):
            raise ValueError("Component snapshot must stay inside the article")
        self.online, self.python, self.request = online, python, request
        self.lock = threading.RLock()
        self.failures = []
        self.remote_queries = 0
        self.replayed_queries = 0
        self._bearer = None
        self._authenticated = False
        self._auth_failure = None
        self.document = self.read()

    def read(self):
        if not self.path.is_file():
            return {"schema": SCHEMA, "source": SOURCE, "queries": {}}
        if self.path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Component snapshot exceeds its size limit")
        data = json.loads(self.path.read_text())
        if (
            data.get("schema") != SCHEMA
            or data.get("source") != SOURCE
            or not isinstance(data.get("queries"), dict)
        ):
            raise ValueError("Unsupported component snapshot")
        for key, entry in data["queries"].items():
            if hashlib.sha256(encoded(entry["response"])).hexdigest() != entry["sha256"]:
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
            if self._auth_failure:
                return self._auth_failure
            if not self._authenticated:
                self._bearer = token(self.python) if self.python else os.environ.get(TOKEN_ENV)
                self._authenticated = True
            status, remote = self.request(method, path, body, self._bearer)
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
                valid = valid and len(batches) == len(request["body"].get("queries", []))
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
                if failure["status"] in (401, 403):
                    self._auth_failure = (failure["status"], {"detail": failure["detail"]})
                return failure["status"], {"detail": failure["detail"]}
            self.document["queries"][key] = {
                "request": request,
                "response": remote,
                "sha256": hashlib.sha256(encoded(remote)).hexdigest(),
            }
            try:
                self.save()
            except (OSError, ValueError):
                self.document["queries"].pop(key, None)
                failure = {"status": 502, "detail": "Component snapshot could not be saved"}
                self.failures.append(failure)
                return 502, {"detail": failure["detail"]}
            return 200, remote
