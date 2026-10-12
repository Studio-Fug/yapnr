"""Recorded opt-in settings for hierarchy, outline and branch-coverage repairs."""

import os


def key():
    flags = (
        "PNR_HIER_SAFE_PARTITION",
        "PNR_NATIVE_OUTLINE",
        "PNR_POWER_BRANCHES",
        "PNR_POWER_CHANNELS",
    )
    enabled = [name for name in flags if os.environ.get(name) == "1"]
    return ",".join(enabled) or None
