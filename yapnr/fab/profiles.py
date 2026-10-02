"""A vendor profile: its data (``capability``) joined with the engine's rules (``pnr.fab_profile``).

``pnr.fab_profile`` stays the single source of the engine numbers and of the KiCad rules
(``.kicad_pro`` board constraints, ``.kicad_dru`` custom rules); for a data profile it reads them
from the same JSON file. ``yapnr fab check`` writes exactly those rules into its scratch copy, so
a board is judged by the rules it was (or should have been) routed under.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from yapnr.fab import capability


class ProfileError(ValueError):
    """An unknown, draft or unusable profile; the message says what to do."""


def fab_profile_module():
    """``pnr.fab_profile`` (the engine), from Bazel's runfiles or a source checkout."""
    try:
        from pnr import fab_profile
    except ImportError:
        engine = Path(__file__).resolve().parents[2] / "hardware" / "pnr"
        if not (engine / "pnr" / "fab_profile.py").is_file():
            raise ProfileError(
                "yapnr fab needs the engine's pnr.fab_profile: run it from a checkout "
                "(bazel run //:yapnr -- fab ...)"
            ) from None
        sys.path.insert(0, str(engine))
        from pnr import fab_profile
    return fab_profile


def engine_fab(name: str) -> Dict[str, Any]:
    """The profile's fab block over the engine's pre-profile defaults (what KiCad enforces)."""
    fp = fab_profile_module()
    if name not in fp.PROFILES:
        raise ProfileError(f"unknown fab profile {name!r}; known: {', '.join(fp.PROFILES.names())}")
    return fp.apply_fab(fp.LEGACY_FAB, name)


@dataclass
class Profile:
    """A vendor profile (``yapnr-fab-profile-v1``) with its engine rules."""

    doc: Dict[str, Any]

    @property
    def name(self) -> str:
        return self.doc["name"]

    @property
    def vendor(self) -> str:
        return self.doc["vendor"]

    @property
    def status(self) -> str:
        return self.doc["status"]

    @property
    def draft(self) -> bool:
        return self.status == "draft"

    @property
    def copper_layers(self) -> int:
        return self.doc["copper_layers"]

    @property
    def limits(self) -> Dict[str, Any]:
        return self.doc["vendor_limits"]

    @property
    def defaults(self) -> Dict[str, Any]:
        return self.doc["order_defaults"]

    @property
    def pricing(self) -> Optional[Dict[str, Any]]:
        return self.doc.get("pricing")

    @property
    def default_stackup(self) -> Optional[str]:
        return self.doc["stackups"]["default"]

    @property
    def allowed_stackups(self) -> List[str]:
        return list(self.doc["stackups"]["allowed"])

    def stackup_id(self, requested: Optional[str]) -> str:
        """The stackup to use: the requested one (must be allowed) or the profile's default."""
        if requested:
            if requested not in self.allowed_stackups:
                raise ProfileError(
                    f"stackup {requested!r} is not allowed for {self.name}; "
                    f"allowed: {', '.join(self.allowed_stackups)}"
                )
            return requested
        if not self.default_stackup:
            raise ProfileError(
                f"{self.name} has no default stackup: choose one with --stackup "
                f"({', '.join(self.allowed_stackups)})"
            )
        return self.default_stackup

    def fab(self) -> Dict[str, Any]:
        return engine_fab(self.name)

    def board_constraints(self) -> Dict[str, Any]:
        fp = fab_profile_module()
        return fp.board_constraints(self.fab())

    def dru_text(self, netclass_clearances=None, edge_stroke_mm: float = 0.0) -> Optional[str]:
        fp = fab_profile_module()
        return fp.dru_text(None, self.name, netclass_clearances or {}, edge_stroke_mm)

    def via_classes(self) -> Dict[str, Any]:
        return self.fab().get("via_classes") or {}


def load(name: str) -> Profile:
    """A vendor profile by name (``legacy`` is not a vendor profile)."""
    try:
        doc = capability.profile(name)
    except capability.DataError as err:
        raise ProfileError(str(err)) from None
    return Profile(doc)


def for_vendor(vendor: str, copper_layers: int, in_pad_vias: bool = False) -> str:
    """The vendor's default profile for a board's copper layer count (§6.1)."""
    doc = capability.vendor(vendor)
    if in_pad_vias:
        name = (doc.get("profiles_in_pad") or {}).get(str(copper_layers))
        if name:
            return name
    name = doc["profiles_by_layers"].get(str(copper_layers))
    if not name:
        offered = ", ".join(sorted(doc["profiles_by_layers"], key=int))
        raise ProfileError(
            f"{doc['title']} has no profile for {copper_layers} copper layers in yapnr "
            f"(profiles for {offered} layers); choose one with --profile"
        )
    return name
