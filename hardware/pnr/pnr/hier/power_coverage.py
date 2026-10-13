"""Independent coverage gate for routed power diagnostics.

Connectivity agreement with DRC is not electrical qualification. In particular,
one chosen member cannot stand in for an entire parallel capacitor bank. The
required terminals are derived from source carrying roles, not the measured
loops, so an empty loop list cannot vacuously pass.
"""


def check(roles, measured_loops, *, connectivity_valid):
    carrying = roles["carrying"]
    power = set(roles["power_nets"])
    returns = set(roles["return_nets"])
    required = set()
    # Every branch sharing both a supply and return with another device must
    # appear in the measured loop set. No refdes or footprint-name heuristics.
    refs = sorted(carrying)
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            common = set(carrying[a]) & set(carrying[b])
            if common & power and common & returns:
                required.update((r, n) for r in (a, b) for n in common)
    # Also cover each participant of inferred converter loops, including their
    # series legs. Expand classes rather than accepting the selected member.
    for loop in roles["loops"]:
        if not loop["hot"]:
            continue
        for ci in loop["classes"]:
            for ref in roles["classes"][ci]["members"]:
                required.update((ref, n) for n in loop["nets"] if n in carrying[ref])
    measured, disconnected = set(), set()
    for loop in measured_loops:
        for link in loop["links"]:
            # Multi-member endpoints are alternatives, not individual witnesses.
            for key in ("a", "b"):
                if len(link[key]) == 1:
                    item = (link[key][0], link["net"])
                    measured.add(item)
                    if link["open"]:
                        disconnected.add(item)
    missing = required - measured
    no_power = not power and not required
    complete = (bool(required) or no_power) and not missing and connectivity_valid
    return dict(
        schema="pnr-power-coverage-v1",
        status="not_applicable" if no_power else "covered" if complete else "unqualified",
        required_terminals=[list(x) for x in sorted(required)],
        missing_terminals=[list(x) for x in sorted(missing)],
        disconnected_terminals=[list(x) for x in sorted(disconnected & required)],
        coverage_complete=complete,
        connectivity_pass=complete and not disconnected,
        qualified=False,
        unresolved=[
            "Loop impedance/inductance and return-plane constrictions are not qualified by shortest-path connectivity.",
            "Capacitor ripple current, current sharing and thermal limits require device and excitation models.",
        ]
        + (
            ["No power-loop obligations could be established."]
            if not required and not no_power
            else []
        ),
    )
