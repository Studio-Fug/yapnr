"""The only network access of ``yapnr order`` (O1): ``--verify-url``, a read-only GET of our own
published zip, compared with the bundle's sha256 (design §3 rule 2, §8.3).

https only, a timeout, a size limit, redirects only to https (release assets redirect to their
storage host), nothing sent but the GET itself: no cookies, no credentials, no body. Every other
module of ``yapnr.order`` and ``yapnr.fab`` is offline by construction (tests patch the socket).
"""

from __future__ import annotations

import hashlib
import urllib.request
from urllib.parse import urlsplit

MAX_BYTES = 100 * 1024 * 1024  # JLCPCB's upload limit; far above any gerber zip
TIMEOUT = 60.0


class VerifyError(RuntimeError):
    """The published zip is unreachable, too large, or not the bundle's zip."""


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise VerifyError(f"refusing a redirect to a non-https URL ({newurl})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_HttpsOnlyRedirect())


def verify_url(url: str, expected_sha256: str, opener=None, timeout: float = TIMEOUT) -> str:
    """GET ``url`` and return its sha256; VerifyError unless it equals ``expected_sha256``."""
    if urlsplit(url).scheme != "https":
        raise VerifyError("--verify-url needs an https URL")
    opener = opener or _opener()
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "yapnr-verify"})
    digest = hashlib.sha256()
    size = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise VerifyError(f"{url} is larger than {MAX_BYTES} bytes")
                digest.update(chunk)
    except VerifyError:
        raise
    except OSError as err:
        raise VerifyError(f"cannot fetch {url}: {err}") from None
    found = digest.hexdigest()
    if found != expected_sha256:
        raise VerifyError(
            f"{url} is not this bundle's zip (sha256 {found[:16]}..., expected "
            f"{expected_sha256[:16]}...)"
        )
    return found
