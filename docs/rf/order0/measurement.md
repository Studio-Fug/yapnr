# Order 0: measurement procedure

The boards are measured with a LibreVNA (100 kHz-6 GHz), so every Order 0 band lies inside
1-6 GHz. The LibreVNA measures no switch terms and its own TRL has no switch-term correction, so
the calibration runs offline in two tiers: a coaxial SOLT at the cable ends, then multiline TRL
on the boards' own coupons, as two branches (two-tier on SOLT-corrected data, and raw data with
switch terms estimated from the asymmetric coupon A14) whose agreement is checked.

## Before ordering (owner, about 30 minutes)

1. Record `DEV:INF:HWREV?` and `DEV:INF:FWREV?` and update to firmware 1.6.4 or later (1.6.3 has
   a flash-timing bug; 1.6.1 and older invert some points).
2. Measure the isolation floor, 6 MHz-6 GHz at 1 kHz IF bandwidth, both cables terminated: without
   and with an isolation term, then again after moving the cables.
3. Ten sweeps of a through adapter and five disconnect/reconnect cycles: the instrument's own
   repeatability.
4. Optionally, the same with 3 dB attenuators at the VNA ports; keep whichever gives the better
   residual on the verification line later.

## Equipment

- The LibreVNA powered from its 5 V jack or a powered hub; two phase-stable SMA cables, taped
  down. Move the board, not the cables.
- Tier 1: a LibreCAL if available, else SMA open/short/load standards and an F-F thru adapter.
- Tier 2: the on-board multiline TRL kits, A01-A07 (region M) and B01-B05 (region W).
- Two SMA-male 50 Ω loads for the third port of the 3-port demos, each measured as a 1-port after
  tier 1 and referred to the demo's reference plane through that port's error box.
- An SMA torque wrench at one fixed setting (8 in-lb is inside Cinch's 7-10 in-lb), a 4-wire
  ohmmeter or a current source and millivoltmeter, a thermometer and a hygrometer.

## Assembly

One operator, one alloy, a 3D-printed jig that holds each connector flush against the milled edge
and presses the board up against the top legs (the 1.73 mm slot is 0.19 mm wider than the board),
flux removed with isopropyl alcohol (residue changes εeff on mask-open lines). File any tab nubs
off the launch edges first. Photograph every joint.

## On arrival

- Photograph the boards and note the core colour: FR408HR is milky white, EM528 red.
- Measure the board thickness at three points with calipers (this checks the core, not the
  0.2 mm prepreg).
- Break out the sticks of copies 1 and 2; keep copy 3 as a spare and for the microsection.
- Solder the connectors (O0-M: 33 per copy; O0-W: 16; O0-D: 10), then condition for 24 h at lab
  ambient.

## Set-up and calibration

1. 30 min warm-up. Sweep 6 MHz-6 GHz in 1000 points (a uniform grid starting at its own step,
   as IEEE 370 needs), IF bandwidth 1 kHz, 4 averages, the highest stimulus level. Check for
   compression once (the thru at 0 dBm and −10 dBm). Never change the span after calibrating.
2. Tier-1 SOLT at the cable ends. Save the calibration file, then re-measure a load and a short as
   devices. Leave the isolation term out of the published data.
3. The floor: both cables terminated, the same settings, saved with the data; also with both
   cables open.

## Sequence per copy

1. The thru, then the lines in increasing length, the reflect and the verification line.
2. The width set, A10, the ring, the stub, the C-pads (tier 1, 30-300 MHz matters).
3. A14 in both orientations (swap the cables).
4. R1 (and on O0-D, D1): each pair of ports (1-2, 1-3, 2-3) with a load on the third.
5. The thru again (drift check).
6. Three independent connections of A01, A03, A04 and each demo.
7. Re-measure the tier-1 load; if it moved by more than 0.1 dB or 1°, repeat the copy.
8. O0-W: the same with B01-B05, R1t and D2.

Then the DC meanders of A15 (4-wire, 100 mA, with the board temperature: copper changes by
0.39 % per kelvin), and optionally a microsection of A15 from copy 3 at the cut mark.

## Files

A session follows the coupon session layout ([examples/rf-coupons/measurements/](../../../examples/rf-coupons/measurements/README.md)),
extended for Order 0 with the raw (uncorrected) sweeps, the loads and the floor:

```text
<session>/
  session.yaml          # stackup and received substrate, board serials, generator commit, VNA,
                        # firmware, hardware revision, calibration file sha256, grid, IF bandwidth,
                        # power, temperature and humidity at start and end, cables, torque, notes
  cal/tier1.cal         # the LibreVNA calibration file (with its raw standards)
  corr/O-1_A03-M-L13.0_r1.s2p    # <upload>-<copy>_<stick>-<family>-<what>_r<connection>
  raw/O-1_A03-M-L13.0_r1.s2p     # uncorrected, from the raw data stream
  corr/O-1_D1-M-P12_r1.s2p       # 3-port demos: one file per port pair
  loads/L1.s1p, loads/L2.s1p
  floor/iso_noterm.s2p, floor/iso_term.s2p
  dc.csv
```

The capture script (SCPI on the headless LibreVNA GUI, the raw stream on TCP 19000), the
switch-term step, the two-branch comparison and the demo de-embedding are still to be written
(Order 0 design, WP7); they are needed when the boards arrive, not for the upload.
