"""Conservative DC copper screening of every inferred power-loop leg.

Each branch is excited separately at its net's source current envelope. This
does not assume equal sharing between capacitors and does not claim to model
ripple, inductance, ESR or thermal behavior. Filled planes are resistive meshes,
not zero-impedance connectivity hubs. IPC-2221 neck findings are screening
failures; a clean screen remains unqualified for AC/thermal acceptance.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path


def screen(coppers, roles, rules, *, h_mm=0.2, max_iter=10000):
    from pnr.ir_drop import solve

    links = {}
    for loop in roles["loops"]:
        if not loop["hot"]:
            continue
        for link in loop["links"]:
            a, b = (roles["classes"][link[k]]["members"] for k in ("a", "b"))
            for ra in a:
                for rb in b:
                    links[(link["net"], *sorted((ra, rb)))] = None
    results = []
    fab = rules.get("electrical_fab") or {}
    for net, a, b in sorted(links):
        rec = dict(net=net, a=a, b=b, status="unqualified")
        copper = coppers.get(net)
        envelope = roles.get("envelope", {}).get(net, {})
        amps = envelope.get("rms_a")
        if (
            copper is None
            or not amps
            or not fab.get("min_via_plating_um")
            or not fab.get("delta_t_c")
        ):
            rec["reason"] = (
                "missing extracted copper, current envelope or fabrication screening model"
            )
            results.append(rec)
            continue
        try:
            ends = []
            for ref in (a, b):
                pins = set(roles["carrying"][ref][net])
                ids = [
                    i for i, p in enumerate(copper["pads"]) if p["ref"] == ref and p["pad"] in pins
                ]
                if {copper["pads"][i]["pad"] for i in ids} != pins:
                    raise ValueError("carrying terminals missing from native copper")
                ends.append(ids)
            report = solve(
                copper,
                sources=ends[0],
                sinks=ends[1],
                current_a=amps,
                h=h_mm,
                max_iter=max_iter,
                two_point=True,
                plating_mm=float(fab["min_via_plating_um"]) / 1000,
                copper_oz_delta_t_c=float(fab["delta_t_c"]),
            )
            necks = {
                layer: item for layer, item in report.get("density", {}).items() if item.get("neck")
            }
            rec.update(
                report=report,
                necks=necks,
                current_a=amps,
                current_basis="individual branch at full source envelope; no simultaneous sum",
                envelope_source=envelope.get("source"),
            )
            rec["status"] = (
                "fail"
                if report["status"] == "open" or necks
                else "screened" if report["status"] == "pass" else "unqualified"
            )
        except (ValueError, KeyError, ArithmeticError) as exc:
            rec["reason"] = str(exc)
        results.append(rec)
    unknown = sum(x["status"] == "unqualified" for x in results)
    failures = sum(x["status"] == "fail" for x in results)
    no_power = not roles["power_nets"] and not results
    return dict(
        schema="pnr-power-loop-copper-v1",
        links=results,
        failure_count=0 if no_power else failures if results and not unknown else None,
        status=(
            "not_applicable"
            if no_power
            else "unqualified" if not results or unknown else "fail" if failures else "screened"
        ),
        unqualified_links=unknown,
        qualified=False,
        model="per-branch DC copper/IPC-2221 screening; AC and thermal qualification open",
        h_mm=h_mm,
        max_iter=max_iter,
    )


def evaluate(board, roles, rules, *, ki_py, timeout=120, out_dir=None):
    """Bound both native extraction and the numeric screen; no unbounded solver."""
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1])
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    if out_dir is not None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    with nullcontext(str(out_dir)) if out_dir is not None else tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        inputs, copper, result = (
            d / name for name in ("inputs.json", "copper.json", "result.json")
        )
        board_sha = hashlib.sha256(Path(board).read_bytes()).hexdigest()
        inputs.write_text(json.dumps(dict(roles=roles, rules=rules, board_sha256=board_sha)))
        try:
            subprocess.run(
                [ki_py, "-m", "pnr.power_loop", "dump", str(board), str(inputs), str(copper)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pnr.power_loop",
                    "screen",
                    str(copper),
                    str(inputs),
                    str(result),
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            report = json.loads(result.read_text())
            report.update(
                board_sha256=board_sha,
                copper_sha256=hashlib.sha256(copper.read_bytes()).hexdigest(),
            )
            result.write_text(json.dumps(report, indent=2) + "\n")
            return report
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            report = dict(
                qualified=False,
                failure_count=None,
                error=str(exc),
                status="unqualified",
                model="per-branch DC copper screening",
                board_sha256=board_sha,
            )
            result.write_text(json.dumps(report, indent=2) + "\n")
            return report


def main():
    mode, source, inputs, output = sys.argv[1:]
    data = json.loads(Path(inputs).read_text())
    if mode == "dump":
        import pcbnew

        from pnr.ir_extract import extract

        board = pcbnew.LoadBoard(source)
        roles = data["roles"]
        result = {
            n: extract(board, n, source)
            for n in sorted(set(roles["power_nets"] + roles["return_nets"]))
        }
    elif mode == "screen":
        result = screen(json.loads(Path(source).read_text()), data["roles"], data["rules"])
    else:
        raise ValueError("expected dump or screen")
    Path(output).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
