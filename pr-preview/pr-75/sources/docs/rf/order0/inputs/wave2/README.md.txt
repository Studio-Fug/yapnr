# Order 0: the second (last) wave of optimizer formulations

Wave 1 ([../compute-m.toml](../compute-m.toml), [../compute-w.toml](../compute-w.toml)) ran the
four formulations of [demos.md](../../demos.md) for D1 and D2. This directory holds the one
extra wave the compute stage allows, written before any of its runs started. Each variant
changes wave 1's `star` spec (the junction start) as listed in `variants-*.json`; the criteria
are wave 1's, unchanged ([../criteria/](../criteria/)), and so is the selection rule, applied to
both waves together.

D2's part (`wave2-w.toml`, GCP campaign `20261004-mceval-f33bd0`, the same machine family as
D2's wave 1) was submitted on 2026-10-04 at 11:45 UTC, when no D2 formulation of wave 1 could
pass: `d2-star` missed the coarse-grid \|S21\| limit (−3.40 dB) by 0.012 dB at 5.75 GHz while
passing the fine and finer grids, and the three uniform starts (base, robust, sched) never
carried power (\|S21\| below −40 dB at β = 64).

| Variant           | Change against `star`                                                             |
| ----------------- | --------------------------------------------------------------------------------- |
| `star-s21`        | the optimizer's \|S21\|, \|S31\| requirement −3.30 dB instead of −3.35 dB (raw)    |
| `star-guard`      | objective band 4.15–5.85 GHz at 9 points instead of 4.25–5.75 GHz at 7            |
| `star-sched`      | the `sched` schedule (35, 15, 15, 10) with `trust_reference: best`                |
| `star-s21-guard`  | both of the first two                                                             |
