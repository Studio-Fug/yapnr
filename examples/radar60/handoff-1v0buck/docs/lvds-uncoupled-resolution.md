# LVDS uncoupled length: 1 mm or 3 mm? Resolved to 3 mm

Resolved 2026-10-06 by the main session, on the owner's instruction to resolve the conflict for this
handoff. The owner may override this decision; if they do, change the four places listed under
"After this resolution".

## Where each value comes from

| Value  | Where                                                                                                                                                                                                                                                                                            | Introduced                                                                                                              |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 1.0 mm | `examples/radar60/schematic/elec/src/radar60.ato`, the four `@pnr-pair` annotations (`lvds_tx0`, `lvds_tx1`, `lvds_clk`, `lvds_frclk`, `"max_uncoupled_mm":1.0`)                                                                                                                                 | commit `edd99b91` (2026-10-03, "radar60 schematic: atopile source for the Rev A electronics"), with no stated rationale |
| 3 mm   | `examples/radar60/board/radar60.kicad_dru` and `reva/radar60-reva.kicad_dru` (`(constraint diff_pair_uncoupled (max 3mm))`), `board/floorplan.yaml` (`max_uncoupled_mm: 3.0`, "the DRU's own diff_pair_uncoupled cap"), `board/constraints.yaml` (four `max_uncoupled_mm: 3`), `board/README.md` | commit `02356d57` (2026-10-03, "radar60 board: floorplan, custom rules and constraints for Rev A")                      |
| 3 mm   | `docs/stage3c/plan.md` (the stage-3c pass marks: "LVDS: 4 pairs on F.Cu, gap 0.16–0.22 mm, ≤ 3 mm uncoupled, skew ≤ 0.1 mm per pair and ≤ 2 mm across the group"), `docs/stage3b-board/design.md`                                                                                                | stage 3b/3c plans (2026-10-04/05)                                                                                       |
| (none) | `docs/requirements/requirements.txt`; `docs/design/board-design.md` (§3.2 pin plan, §3.4 annotations: 100 Ω, ±10 %, 0.1 mm intra-pair skew, 2 mm group skew; no uncoupled limit)                                                                                                                 | —                                                                                                                       |

No owner decision (D0–D15 in `docs/design/board-design.md`) sets an uncoupled limit.

## Precedence applied

1. An explicit owner decision would win. There is none.
2. The requirements and the original board design would win over implementation files. Neither
   sets an uncoupled limit.
3. Among the documents the owner named as authoritative for this handoff, the stage-3c plan
   (`docs/stage3c/plan.md`) sets **≤ 3 mm** as the acceptance pass mark. The 1.0 mm value
   appears only in an implementation annotation and gives no rationale.

## Engineering check

- The LVDS here is the development capture interface to J2 (QTH-030), which is not fitted in product
  builds (board-design §1.2, §3.1). It runs at 600 Mbps per lane, about 410 Mbps needed
  (board-design §3.2).
- A signal edge of about 200–300 ps spans roughly 30–45 mm of FR-4 or RO4835 microstrip. The usual
  budget for an uncoupled stretch is a tenth of that or less, about 3–4 mm. So 3 mm is consistent
  with the signal; 1 mm is stricter than it needs.
- At the 0.65 mm-pitch ABL0161 escape, the legs leave adjacent balls and separate briefly before
  they couple (stage-3c plan R4: "Uncoupled budget = DRU 3 mm − the escape length outside the
  courtyard"). A 1 mm limit would leave almost no room for that escape.

## After this resolution

- **The acceptance limit is ≤ 3 mm uncoupled per leg** (KiCad's `diff_pair_uncoupled`), with the
  other LVDS limits unchanged: F.Cu, 0.16–0.22 mm gap, 0.1 mm intra-pair skew, 2 mm group skew.
- On this branch, the four `@pnr-pair` annotations in `radar60.ato` are changed from 1.0 to 3.0, so
  the source and the rules agree. The DRU, `floorplan.yaml` and `constraints.yaml` already say 3 mm.
- None of the routed results depend on this yet: LVDS is 0 of 8 legs on every seed (see the README).
