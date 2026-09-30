"""Compatibility normalization for completed live-viewer phase events."""


def _metric(event, field):
    """Read explicit native metrics; never infer zero or use baseline counts."""
    data = event["data"]
    values = []
    for key in (field, "native_" + field):
        if key in data:
            values.append(("data." + key, data[key]))
    result = data.get("result")
    # Inline reports inherit the immutable event's board binding when the
    # producer omits a redundant report hash. An explicit report hash must
    # match: never mix another board's metrics into this attached geometry.
    if isinstance(result, dict) and (
        "sha256" not in result or result["sha256"] == event["board_sha256"]
    ):
        for key in ("after_" + field, field):
            if key in result:
                values.append(("data.result." + key, result[key]))
    if not values:
        raise KeyError("Missing native " + field + " for attached board")
    if any(type(value) is not int or value < 0 for _, value in values):
        raise ValueError("Invalid native " + field + ": expected nonnegative integer")
    if len({value for _, value in values}) != 1:
        raise ValueError("Conflicting native " + field + " for attached board")
    return values[0][1], [source for source, _ in values]


def phase_frame(event):
    """Normalize legacy display/metric fields without accepting a candidate.

    Required counts and geometry identity still fail closed before lane mutation.
    Original events remain immutable, including unaccepted experiment outcomes.
    """
    data = event["data"]
    label_source = next(
        (key for key in ("name", "phase") if isinstance(data.get(key), str) and data[key].strip()),
        None,
    )
    name = data[label_source] if label_source else "Completed phase (label unavailable)"
    opens, opens_sources = _metric(event, "opens")
    violations, violations_sources = _metric(event, "violations")
    frame = dict(
        name=name,
        label_source=label_source or "unavailable",
        board_sha256=event["board_sha256"],
        event_id=event["id"],
        opens=opens,
        violations=violations,
        metric_sources=dict(opens=opens_sources, violations=violations_sources),
    )
    if any(source.startswith("data.result.") for source in opens_sources + violations_sources):
        frame["result_board_binding"] = (
            "data.result.sha256" if "sha256" in data["result"] else "event.board_sha256"
        )
    if "accepted" in data:
        if type(data["accepted"]) is not bool:
            raise ValueError("Invalid phase accepted flag")
        frame["accepted"] = data["accepted"]
    return frame
