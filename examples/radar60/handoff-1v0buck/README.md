# radar60 handoff: the 1.0 V buck pass (throwaway branch)

This bundle hands radar60 Rev A to another agent so they can finish the board and rerun the VnV
independently. It is **not** for merging. The branch `claude/radar60-1v0buck-handoff` is
`claude/radar60-models` (`d6ad8080`) plus this directory, plus one change to
`examples/radar60/schematic/elec/src/radar60.ato` (the LVDS uncoupled-length annotations, see
below).

The content commit's SHA is under "Commits" at the end.

## State in one paragraph

U2's buck stage has a 1.0 V leg resistance budget of 0.5 mΩ. It meets that at 0.383 mΩ on every
routed seed, using the hybrid power-block library: U2 block `34c7ed35931c` and U5 block
`de5bfabb679f`. The board is **not** complete:

- 148–162 pads are still unconnected;
- DRC finds 14 violations plus 6–11 that KiCad ignores by default, including one short on s0;
- LVDS has 0–1 of 8 legs connected and QSPI 4–6 of 7;
- most rails other than 1V0_BUCK are still open.

The RF macro v2 copper is identical on every seed (digest equal; rf_audit A1–A6 pass). ANT-02 has
preregistered misses and D14 (the PA-feed coupling) is open: see `docs/rf-status.md`.

## Where everything is

| Path                                                     | What                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `results/sN/` (s0–s3)                                    | One routed seed from campaign `20261006-mceval-5818f8`. Contents below.                                                                                                                                                                                                                                                         |
| `results/sN/candidate.kicad_pcb.xz`                      | The routed board. Zone fills are stripped to fit the repo's 600 KB file limit; `--refill-zones` or pcbnew restores them. The other files in this directory sit beside the board.                                                                                                                                                |
| `results/sN/candidate.kicad_pro`, `candidate.kicad_dru`  | The project and custom rules. These are the seed's; the task's route directory was not collected.                                                                                                                                                                                                                               |
| `results/sN/fp-lib-table`, `footprint-libs/`             | The footprint libraries, extracted from the board by `pnr.library_table`, which is what `integrate.py finish` runs. The table uses `${KIPRJMOD}`.                                                                                                                                                                               |
| `results/sN/placement.json`                              | The placement, rebuilt from the board (`tools/placement_from_board.py`)                                                                                                                                                                                                                                                         |
| `results/sN/out/sN/files/measure.json`                   | **The flow's full check report** (`integrate.py check` on GCP): DRC by type plus the checks KiCad ignores by default, unconnected nets, IR per rail, LVDS, QSPI, R1 macro digest, R4 foreign copper, rf_audit on the routed board                                                                                               |
| `results/sN/out/sN/files/route-diag.json`, `result.json` | The routing report and the task verdict                                                                                                                                                                                                                                                                                         |
| `results/sN/drc/drc-fresh-local.json`                    | An independent local KiCad DRC of the restored board, refilled, all severities, with the footprint libraries                                                                                                                                                                                                                    |
| `results/sN/drc/fill-strip-equivalence.json`             | Proof that stripping the fills changed nothing: DRC by type and unconnected count are equal for the original and the stripped board                                                                                                                                                                                             |
| `results/sN/logs/`                                       | The step logs (place, select, finish, route, check). Paths in them are container paths.                                                                                                                                                                                                                                         |
| `results/sN/record.json`                                 | The task record: image digest, machine, timings                                                                                                                                                                                                                                                                                 |
| `results/dataset.jsonl`                                  | The campaign's assembled dataset                                                                                                                                                                                                                                                                                                |
| `power-lib/`                                             | The power-block library. **U2 `34c7ed35931c` and U5 `de5bfabb679f` are preserved**, along with the runners-up `a24fb2995477` and `e720478abbd1`.                                                                                                                                                                                |
| `seed/`                                                  | The prepared work directory the four seeds ran from (`source`, `fixblocks`, `prepare`), including the routing constraints (`constraints-place.yaml`, `inputs/rules.json`, `inputs/rules0.json`, `inputs/graph*.json`)                                                                                                           |
| `explore_jobs_w4b.toml`                                  | The GCP campaign definition (live view on)                                                                                                                                                                                                                                                                                      |
| `atopile/`                                               | The original atopile board (`board/rev-a.kicad_pcb`) and its build outputs, plus: IPC-D-356 and JSON netlists of the atopile board and every seed with their diffs, the per-IC pin tables (symbolic lead, pad, net), the ERC-equivalent reports, and the root cause of the schematic checker's misreading (`atopile/README.md`) |
| `docs/`                                                  | The design objectives and acceptance documents (requirements, board design with decisions D0–D15, calculations, stage 3b RF and board, stage 3c plan and consolidation report), plus `vnv-matrix.md`, `lvds-uncoupled-resolution.md`, `rf-status.md` and `simulations.md`. `docs/INDEX.md` maps each to its source.             |
| `sims/`                                                  | EM inputs and configs (openEMS models and job files, campaign plans, generator variants), S-parameters and summaries of the final campaigns, and the Palace status (`docs/simulations.md`)                                                                                                                                      |
| `MANIFEST.md`, `manifest.json`                           | Engine/source SHAs, image digests, tool versions, environment, commands, and the sha256 of every file                                                                                                                                                                                                                           |
| `localize.py`                                            | Unpacks the bundle: decompresses `.gz` and `.xz` and fills in `${HANDOFF}`/`${REPO}`                                                                                                                                                                                                                                            |
| `tools/`                                                 | `placement_from_board.py`, `hash_bundle.py` (the netlist and pin-table tools are in `atopile/tools/`)                                                                                                                                                                                                                           |

## Results per seed (from each `measure.json`)

| Seed | Unconnected | DRC (+ ignored by default)             | 1V0_BUCK      | Other rails                                                    | LVDS legs | QSPI |
| ---- | ----------- | -------------------------------------- | ------------- | -------------------------------------------------------------- | --------- | ---- |
| s0   | 152         | 14 (+10), including 1 `shorting_items` | 0.383 mΩ pass | 3V3_RADIO_IO pass; the rest open                               | 0/8       | 4/7  |
| s1   | 148         | 14 (+7)                                | 0.383 mΩ pass | 1V8 pass (14.6 mΩ); the rest open                              | 0/8       | 4/7  |
| s2   | 162         | 14 (+6)                                | 0.383 mΩ pass | 1V8 pass; the rest open                                        | 1/8       | 6/7  |
| s3   | 153         | 14 (+11)                               | 0.383 mΩ pass | 1V0_RF1 fails (8.4 mΩ vs 4); 3V3_RADIO_IO fails; the rest open | 0/8       | 4/7  |

**Independent re-check:**

- **s0, s2 and s3:** a fresh local KiCad DRC of the restored board (fills restored, footprint
  libraries present) gives exactly the GCP check's DRC types, counts and unconnected count.
- **s1 (open discrepancy):** the fresh run finds 11 violations, not 14. The 3 `items_not_allowed`
  the GCP check reported don't reproduce with the seed's project and design rules, while unconnected
  (148) matches. The likely cause is a difference between the task's route-directory project/DRU and
  the seed's, but it is not confirmed.

## What still fails, and what hasn't been run

**Board (the acceptance gates are in `docs/vnv-matrix.md` and `docs/stage3c/plan.md`):**

1. **Unconnected pads (148–162) and DRC.** Includes s0's short and the rise from 9 to 14 since wave
   4: dangling vias, isolated copper, and one QSPI length over 25 mm.
2. **IR.** Every rail except 1V0_BUCK (and 1V8/3V3_RADIO_IO on some seeds) is open or failing.
3. **LVDS 0–1/8.** The placement leaves no coupled channel; the fix is a re-place that keeps pair
   ends adjacent. The acceptance limit is ≤ 3 mm uncoupled, see `docs/lvds-uncoupled-resolution.md`.
4. **`power_block.py` ranking.** It must put any block whose own IR path is open below every block
   without one. Fix that before re-making the library.
5. **PA-feed integration.**
   - `merge_macro` accepts `RFM1_PA_FEED` but drops its copper;
   - still needed: the through vias, A2/B2 as skipped pads, and the In3 1V0 pour;
   - `finish`'s pre-route R1 check needs narrowing;
   - U1 GND ball vias are needed for D14.
6. **`route`'s internal `fixed_copper --validate` failure** on the old arm-A placement. Its stderr
   is swallowed.

**RF (`docs/rf-status.md`, `docs/simulations.md`):**

- ANT-02: RL −7.2 dB against −10 dB, gain at 60.3 GHz and efficiency; all preregistered misses.
- D14: TX1 −33.5 dB against −40 dB, open.
- **Not run:**
  - C2 (RF sign-off on routed copper);
  - D14 on routed copper with U1 GND vias;
  - c1b-6 at 15 µm and the Dk corners;
  - the D12 brackets;
  - embedded patterns.

**Not included:**

- the routed boards' zone fills (restored by refill);
- the task route directories' own project/DRU/fp-lib-table and `selection.json` (the campaign didn't
  collect them; `placement.json` is rebuilt from the board);
- EM field dumps, meshes and Palace validation runs (rebuild by rerunning the campaigns);
- the TI-EVM extraction script and any TI file (never committed);
- the owner's cloud config and credentials.

**Proprietary or private dependencies:** none for verification. The openEMS and Palace images
live in the owner's private registry; rebuild them from `docker/` (`docs/simulations.md`).

## How to run

```sh
python3 examples/radar60/handoff-1v0buck/localize.py /path/to/handoff
```

Then follow `MANIFEST.md` (DRC, netlists, pin tables, the flow from the seed, the GCP campaign, EM).

Use KiCad 10's **headless** copy for `kicad-cli` and KiCad's Python. The review run behind the
hybrid
library used the GUI app's Python and Homebrew's `kicad-cli`, which this project does not allow.

## LVDS uncoupled length

The 1 mm vs 3 mm conflict is resolved to **3 mm**; `docs/lvds-uncoupled-resolution.md` has the
evidence. On this
branch the four `@pnr-pair` annotations in `radar60.ato` now say 3.0, matching the DRU and
constraints. The routed results were
produced under the DRU's 3 mm, so nothing changes for them.

## Commits

- Content commit: `CONTENT_SHA` (this README is updated in the following commit).
