# radar60 handoff: the 1.0 V buck pass (throwaway branch)

This bundle holds the radar60 Rev A state in which the U2 buck stage's 1V0_BUCK copper meets its
0.5 mΩ budget (0.383 mΩ) on the whole routed board. It is meant for another agent to pick up the
board while the main line works on the legalizer. It is **not** for merging: the branch
`claude/radar60-1v0buck-handoff` is `claude/radar60-models` plus this directory.

## What is here

| Path                     | What                                                                                                                                                                                                                                                          |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `power-lib/library.json` | The hybrid power-block library: wave 4's U2 buck block `34c7ed35931c` (1V0_BUCK 0.388 mΩ inside the block) and the consolidation's U5 eFuse block `de5bfabb679f`, with their runners-up (`a24fb2995477`, which fails the 1V0_BUCK budget, and `e720478abbd1`) |
| `power-lib/blocks/<id>/` | Each block's synthesis board, routed block, constraints, evaluation and IR report                                                                                                                                                                             |
| `seed/`                  | The prepared work directory (after `source`, `fixblocks` and `prepare`) that the four routed results below were made from                                                                                                                                     |
| `explore_jobs_w4b.toml`  | The GCP campaign that produced the results (four placement seeds, live view on)                                                                                                                                                                               |
| `results/s0..s3/`        | Each seed's `result.json`, `measure.json` and `route-diag.json`. The routed boards themselves are not committed (about 5.4 MB each); rerun the campaign to get them back                                                                                      |
| `localize.py`            | Unpacks the bundle: gunzips the compressed files and fills in the `${HANDOFF}` and `${REPO}` path placeholders                                                                                                                                                |

## Results on the whole board (GCP campaign `20261006-mceval-5818f8`, branch `claude/radar60-3d`)

| Seed | Unconnected | DRC                      | 1V0_BUCK      |
| ---- | ----------- | ------------------------ | ------------- |
| s0   | 152         | 14, one `shorting_items` | 0.383 mΩ pass |
| s1   | 148         | 14                       | 0.383 mΩ pass |
| s2   | 162         | 14                       | 0.383 mΩ pass |
| s3   | 153         | 14                       | 0.383 mΩ pass |

The RF macro copper (macro v2) is unchanged on all four. The best earlier board (wave 4, on the
Mac) had 152 unconnected and DRC 9.

## How to run it

```sh
python3 examples/radar60/handoff-1v0buck/localize.py /path/to/work/handoff
# then run the flow from the localized seed, e.g. the GCP campaign:
python3 tools/exp/pnr_explore_plan.py /path/to/work/handoff/explore_jobs_w4b.toml --repo . --out <dir>
yapnr exp plan <dir>/campaign.toml --backend gcp-batch
```

`explore_jobs_w4b.toml` still contains `${PYTHON}`. Set it to a numerical Python, meaning one
with torch, numpy and pyyaml. Use KiCad 10's **headless** copy for `--kicad-python` and
`--kicad-cli` (see `DEVELOPERS.md`). The review run that produced this state used the GUI app's
Python and Homebrew's `kicad-cli`, which this project does not allow.

## Open items, in priority order

1. **`power_block.py` ranking.** It puts open hot-loop links ahead of in-block IR, so a re-made
   library picked a U2 block whose own 1V0_BUCK path is open. A block with its own IR open must
   rank below every block without one. Fix that before any re-make of the library.
2. **s0's short and the DRC rise from 9 to 14** on every seed.
3. **PA-feed vias.** `kicad_ops.merge_macro` accepts the v2 macro's `RFM1_PA_FEED` footprint but
   drops its copper. Still needed:
   - the through vias;
   - A2/B2 as skipped pads;
   - the In3 1V0 pour;
   - a narrower form of `finish`'s pre-route macro-copper check, which now reads false because the
     power blocks' copper sits beside the macro;
   - U1 GND ball vias for the D14 PA-feed coupling, −33.5 dB as built against −40 dB.
4. **`route`'s internal `fixed_copper --validate` failure** on the old arm-A placement. Its stderr
   is swallowed, so find the cause.
5. **Wider search.** 12 or more seeds per wave on GCP with live view. The flow emits few live
   checkpoints so far, so add engine and flow checkpoints to make the viewer useful.
6. **LVDS 0/8.** Placement does not leave a coupled channel. The fix is a re-place with pair ends
   kept adjacent.

Background, in the main session's notes (not in this repository): stage 3c's plan, the
consolidation report and the RF freeze (antenna block v2: ANT-02 return loss −7.2 dB,
preregistered; D14 open).
