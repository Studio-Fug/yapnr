"""Recorded settings for hierarchy, outline and branch-coverage repairs."""

import os


def key():
    flags = (
        "PNR_HIER_SAFE_PARTITION",
        "PNR_NATIVE_OUTLINE",
        "PNR_POWER_BRANCHES",
        "PNR_POWER_CHANNELS",
        "PNR_PAD_LOCALITY",
        "PNR_SMPS_LAYOUT",
        "PNR_SMPS_PLACEMENT",
        "PNR_INGEST_COPPER_PADS",
        "PNR_PLANE_ACCESS_NATIVE_FIT",
        "PNR_PLANE_ACCESS_COMPACT_BUS",
    )
    enabled = [name for name in flags if os.environ.get(name, "1") == "1"]
    return ",".join(enabled) or None
