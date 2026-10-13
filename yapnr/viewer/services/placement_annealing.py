"""Recorded, iteration-based rotation temperatures for interactive placement."""

import math


def parse(value=None, *, steps=400, start_step=0):
    if value is None:
        value = dict(mode="schedule", start_temperature=2.0, end_temperature=0.2, steps=steps)
    if not isinstance(value, dict):
        raise ValueError("annealing must be a settings object")
    mode = value.get("mode")
    keys = (
        {"mode", "temperature"}
        if mode == "hold"
        else {"mode", "start_temperature", "end_temperature", "steps"}
    )
    if mode not in ("hold", "schedule") or set(value) != keys:
        raise ValueError(
            "Choose hold with temperature, or schedule with start/end temperatures and steps"
        )
    for key in keys - {"mode", "steps"}:
        n = value[key]
        if type(n) not in (int, float) or not math.isfinite(n) or not 0.05 <= n <= 10:
            raise ValueError("Rotation temperatures must be finite and between 0.05 and 10")
    if mode == "schedule" and (
        type(value["steps"]) is not int or not 1 <= value["steps"] <= 1000000
    ):
        raise ValueError("Annealing steps must be an integer from 1 to 1000000")
    return dict(value, start_step=start_step)


def temperature(settings, step):
    if settings["mode"] == "hold":
        return settings["temperature"]
    fraction = min(1.0, max(0.0, (step - settings["start_step"]) / max(1, settings["steps"] - 1)))
    return (
        settings["start_temperature"]
        - (settings["start_temperature"] - settings["end_temperature"]) * fraction
    )
