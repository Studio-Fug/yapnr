"""Per-vendor staging mechanisms and their status (``yapnr order vendors``), from vendors/*.json.

Mechanisms (design §8.3):

- ``manual`` (O1, every vendor): open the vendor's upload or quote page; the human drops the zip.
- ``import-url`` (O1, OSH Park): ``https://oshpark.com/import?url=<public zip>``. OSH Park fetches a
  zip that is already public (a release asset, the docs site); yapnr uploads nothing.
- ``upload`` (O2) and ``api`` (O3): not built. They wait for the owner's decisions D1 and D3.
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote, urlsplit

from yapnr.fab import capability

NOT_BUILT = {
    "upload": "O2, not built: waits for the owner's decision D1 (does an opt-in upload count as "
    "staging?)",
    "api": "O3, not built: waits for vendor API access and the owner's decision D3",
}


class StagingError(ValueError):
    """A mechanism this vendor does not offer, or an unusable URL."""


def mechanisms(vendor: str) -> List[Dict[str, Any]]:
    doc = capability.vendor(vendor)
    out = []
    for key, spec in doc["staging"].items():
        name = key.replace("_", "-")
        if name == "assembly":
            name = "manual (assembly)"
        out.append(
            {
                "mechanism": name,
                "status": "available (O1)",
                "url": spec.get("url") or spec.get("template"),
                "src": spec.get("src"),
            }
        )
    for name, status in NOT_BUILT.items():
        out.append({"mechanism": name, "status": status, "url": None, "src": None})
    return out


def manual_page(vendor: str, assembly: bool = False) -> Dict[str, Any]:
    staging = capability.vendor(vendor)["staging"]
    if assembly and "assembly" in staging:
        return staging["assembly"]
    return staging["manual"]


def import_link(vendor: str, public_url: str) -> str:
    """The vendor's import-by-URL link for a public zip (OSH Park only)."""
    spec = capability.vendor(vendor)["staging"].get("import_url")
    if spec is None:
        raise StagingError(f"{vendor} offers no import by URL; use the manual page")
    parts = urlsplit(public_url)
    if parts.scheme != "https" or not parts.netloc:
        raise StagingError(f"--url must be a public https URL, not {public_url!r}")
    if parts.username or parts.password:
        raise StagingError("--url must not carry credentials")
    return spec["template"].format(url=quote(public_url, safe=":/"))


def steps(spec: Dict[str, Any], zip_name: str, assembly: bool) -> List[str]:
    """A staging spec's steps for one zip; ``Assembly:`` steps only for an assembly order."""
    return [
        step.format(zip=zip_name)
        for step in spec.get("steps", [])
        if assembly or not step.startswith("Assembly:")
    ]
