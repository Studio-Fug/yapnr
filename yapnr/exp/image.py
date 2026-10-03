"""Container image references: parsing, pinning by digest, and resolving a tag on its registry.

Every task names an image by its **index digest** (``ghcr.io/studio-fug/yapnr@sha256:...``), so a
campaign runs one exact image on every backend and every platform. ``resolve`` asks the registry
(the OCI distribution API, standard library only) for the digest of a tag: anonymously for public
packages, or with a token from ``YAPNR_REGISTRY_TOKEN`` (a GitHub token with ``read:packages``
while the GHCR packages are private). ``--image-digest`` and a pinned ``image`` in the campaign file
skip the lookup, which is what ``plan --offline`` needs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_REPOSITORY = "ghcr.io/studio-fug/yapnr"
TOKEN_ENV = "YAPNR_REGISTRY_TOKEN"
TIMEOUT_S = 30

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REF_RE = re.compile(
    r"^(?P<registry>[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?)/"
    r"(?P<repository>[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*)"
    r"(:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


# How the yapnr image runs the task wrapper: its interpreter, and its KiCad environment launcher
# as the container entrypoint. A campaign's ``runtime`` table replaces either for another image.
YAPNR_PYTHON = "/opt/venv/bin/python"
YAPNR_ENTRYPOINT = "/usr/local/bin/yapnr-kicad-env"


class ImageError(ValueError):
    pass


def runtime(image_meta: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """(python, entrypoint or None) that run ``task.py`` in a plan's image (``meta["image"]``)."""
    given = image_meta.get("runtime") or {}
    python = given.get("python") or YAPNR_PYTHON
    entrypoint = given.get("entrypoint", YAPNR_ENTRYPOINT)
    return python, (entrypoint or None)


@dataclass(frozen=True)
class Ref:
    registry: str
    repository: str
    tag: Optional[str] = None
    digest: Optional[str] = None

    @property
    def name(self) -> str:
        return "%s/%s" % (self.registry, self.repository)

    @property
    def pinned(self) -> str:
        if not self.digest:
            raise ImageError("%s is not pinned to a digest" % self)
        return "%s@%s" % (self.name, self.digest)

    def __str__(self) -> str:
        text = self.name
        if self.tag:
            text += ":" + self.tag
        if self.digest:
            text += "@" + self.digest
        return text


def parse(text: str, default_repository: str = DEFAULT_REPOSITORY) -> Ref:
    """A full reference, or a bare tag of ``default_repository`` (``edge``, ``v0.1.0``)."""
    if TAG_RE.match(text) and "/" not in text:
        text = "%s:%s" % (default_repository, text)
    match = REF_RE.match(text)
    if not match:
        raise ImageError("%r is not an image reference" % text)
    return Ref(match["registry"], match["repository"], match["tag"], match["digest"])


Opener = Callable[[urllib.request.Request], "urllib.response.addinfourl"]


def _open(request: urllib.request.Request):
    return urllib.request.urlopen(request, timeout=TIMEOUT_S)


def _bearer_token(ref: Ref, opener: Opener, secret: Optional[str]) -> Optional[str]:
    """A pull token from the registry's token endpoint (``/token``, as GHCR and Docker Hub use)."""
    query = urllib.parse.urlencode(
        {"scope": "repository:%s:pull" % ref.repository, "service": ref.registry}
    )
    request = urllib.request.Request("https://%s/token?%s" % (ref.registry, query))
    if secret:
        basic = base64.b64encode(("token:%s" % secret).encode()).decode()
        request.add_header("Authorization", "Basic " + basic)
    try:
        with opener(request) as response:
            return json.loads(response.read().decode()).get("token")
    except (urllib.error.URLError, ValueError, OSError):
        return None


def resolve(
    ref: Ref,
    opener: Opener = _open,
    environ: Optional[Dict[str, str]] = None,
) -> str:
    """The digest the registry serves for ``ref``'s tag (an index digest for multi-arch images)."""
    if ref.digest:
        return ref.digest
    env = os.environ if environ is None else environ
    secret = env.get(TOKEN_ENV) or None
    token = _bearer_token(ref, opener, secret)
    request = urllib.request.Request(
        "https://%s/v2/%s/manifests/%s" % (ref.registry, ref.repository, ref.tag or "latest"),
        method="HEAD",
    )
    request.add_header("Accept", ", ".join(INDEX_TYPES))
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with opener(request) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as err:
        hint = ""
        if err.code in (401, 403):
            hint = (
                " (the package is private: make it public, set %s to a token with read:packages, "
                "or pass --image-digest)" % TOKEN_ENV
            )
        raise ImageError("%s: registry answered %d%s" % (ref, err.code, hint)) from err
    except (urllib.error.URLError, OSError) as err:
        raise ImageError("%s: %s (pass --image-digest to plan offline)" % (ref, err)) from err
    if not DIGEST_RE.match(digest):
        raise ImageError("%s: the registry sent no usable digest" % ref)
    return digest


def pin(
    text: str,
    digest: Optional[str] = None,
    opener: Opener = _open,
    environ: Optional[Dict[str, str]] = None,
    offline: bool = False,
) -> Ref:
    """``text`` pinned to ``digest`` (given), its own digest, or the registry's digest of its tag."""
    ref = parse(text)
    if digest:
        if not DIGEST_RE.match(digest):
            raise ImageError("%r is not a sha256 digest" % digest)
        if ref.digest and ref.digest != digest:
            raise ImageError("%s is pinned to another digest than %s" % (ref, digest))
        return Ref(ref.registry, ref.repository, ref.tag, digest)
    if ref.digest:
        return ref
    if offline:
        raise ImageError("%s is a tag; pass --image-digest (or a pinned image) offline" % ref)
    return Ref(ref.registry, ref.repository, ref.tag, resolve(ref, opener, environ))


def mirror(ref: Ref, registry_prefix: str) -> str:
    """``ref`` through an Artifact Registry remote repository of its registry.

    ``registry_prefix`` is the repository (``<region>-docker.pkg.dev/<project>/ghcr``); the image
    path below the upstream registry stays the same, so ``ghcr.io/studio-fug/yapnr@sha256:...``
    becomes ``<prefix>/studio-fug/yapnr@sha256:...``.
    """
    return "%s/%s@%s" % (registry_prefix.rstrip("/"), ref.repository, ref.pinned.split("@", 1)[1])
