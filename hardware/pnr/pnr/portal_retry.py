"""Bound an optional coordinated escape retry of an already-failed joint repair."""

import math


def retry_command(command, out, remaining):
    # Single-net search and specialized current/pair workers remain unchanged.
    # Do not launch a large negotiation without restoration dependencies.
    if (
        not math.isfinite(remaining)
        or remaining < 140
        or "--joint" not in command
        or "--layers" not in command
        or command.count("--net") < 2
        or "--preserve-copper" in command
    ):
        return None
    cmd = list(command)
    for flag, value in [
        ("--out-dir", out),
        ("--pitch", ".05"),
        ("--max-expansions", "600000"),
        ("--max-seconds", str(min(450.0, remaining - 40.0))),
    ]:
        cmd[cmd.index(flag) + 1] = str(value)
    if "--portal-joint" not in cmd:
        cmd.append("--portal-joint")
    return cmd
