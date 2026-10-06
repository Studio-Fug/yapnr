<!-- markdownlint-disable -->

# radar60: Rev A board design, from the requirements to an orderable board

Design plan, 2026-10-03. No board has been laid out, simulated, quoted or ordered yet. This document
turns the owner's requirements (`requirements.txt`, proposal of 2026-10-02) and the three research
notes (`research-radio.md`, `research-fab.md`, `research-rf.md`) into one Rev A plan that ends in a
file set the owner can upload for a quote and order.

How to read the numbers:

- **[D]** is a number I derived (closed-form arithmetic or engineering judgement). The arithmetic is
  in `board-calc/board_calc.py`, and its output is in `board-calc/board_calc.out`; it reuses the
  microstrip and patch models of `fab-calc/fab60_calc.py`. Conductor loss is good to about ±30 %.
  None of it is a full-wave result.
- **[E]** is a planning allowance or schedule estimate with no public price or quote behind it.
- Every other number carries a source tag (list at the end). Prices and stock were read on
  2026-10-03 from public pages without an account. They go stale quickly.
- The evidence labels follow PNR-05. Everything in this document is either an _analytical estimate_
  or a _vendor statement_. Nothing here is a solver prediction or a measurement yet.

Nothing was ordered, reserved, quoted under an account or requested from anyone. Datasheet facts I
re-checked against the primary PDFs on 2026-10-03 are marked "(checked)".

---

## 0. Summary and decisions

**What gets built.**

- **Rev A** is the full radar: **Board A** (the conventional RF design) plus a coupon strip on one
  PCBWay panel.
  - Stackup: 6 layers. RO4835 LoPro 4 mil core on L1–L2, then RO4450F 4 mil bondply on L2–L3, then
    FR-4. Immersion silver finish, through vias only, about 1.2 mm thick.
  - Board: 60 × 46 mm.
  - Parts: IWR6843AQGABLR radio, LP87524J PMIC in the 1 V LDO-bypass power tree (all three TX at
    once), UART and CAN-FD on one JST GH connector.
  - Development builds also fit a DCA1000-compatible LVDS header. Product builds leave it
    unfitted (DNP).
- **Rev A0** is optional, and I recommend it: the same netlist and the same flow on a JLCPCB 6-layer
  FR-4 board (JLC06161H-3313, ENIG), assembled turnkey from radios JLC has in stock.
  - It is an electronics and firmware bring-up board, and the first physical proof of the yapnr
    flow.
  - It is **not** an RF-01 build: it has about 3 dB less SNR [D] and does not count as RF evidence.

**Antenna floorplan [D].**

- U1 is rotated so its RX edge faces the antenna (north) side.
- The 4-column RX bank sits straight out of that edge, with 2.7 mm feeds.
- The 3-column TX bank sits to the north-east, with equal-length feeds of about 10.9 mm. _Review:_
  the TX1/TX2 meanders (+6.9 / +3.3 mm) and the corporate column inside the 2.342 mm pitch are not
  drawn yet; both can change this floorplan (§14.4 R2, R3).
- The nearest TX and RX columns are 6.2 mm apart (1.27 λ0), with a via-fenced ground strip between
  them.
- Column phase centres follow ANT-01 exactly: d = 2.342 mm, and both banks' azimuth axes run
  parallel to the board's x axis.

**Honest RF status.**

- The Rev A conventional copper is parametric: closed-form starting values, swept and signed off
  in openEMS on GCP.
- yapnr.rf is validated only at 7–13 GHz [RRF §2]. It lacks multilayer substrates, vias and GCPW
  ports, finite copper thickness and a near-to-far-field transform.
- Board B (the optimized radar) joins the Rev A panel only if it passes a mechanical gate by the
  freeze date (§6.5). Otherwise it goes into Rev B as a pair with a fresh Board A, so DEMO-01
  still gets a same-lot comparison.
- The optimized divider and the optimized TX feed go on the Rev A coupon strip either way.

**Preregistered predictions [D].** (Review: the bands below now carry the model's own ±30 %
conductor-loss error and a 0.3–0.8 dB through-via launch; the central values are unchanged, §14.3.)

- **RX path loss** (ball to column input): 0.8–1.0 dB central, **0.6–1.4 dB** band, which meets RF-03.
- **TX path loss:** 1.5–1.8 dB central, **1.2–2.4 dB** band, which misses RF-03's 1.5 dB target. The TX
  feeds are long because the TX and RX balls sit on adjacent package edges (§5.3).
  - The miss costs nothing in link budget. The 12 dBm EIRP cap already forces at least 7 dB of TX
    backoff (§10.3).
  - It is a waiver request (D6), and the natural DEMO-01 optimizer target.
- **Bandwidth:** a single patch on the 8 mil (L2-cleared) region covers about 4.3 %, against
  ANT-02's 5.6 %. So the conventional column is **expected to miss 10 dB return loss at the band
  edges** (60.3 and 63.8 GHz).
  - Review: the patch centre frequency is itself uncertain by about ±1.1 GHz (Dk, etch, thickness;
    `board-calc/review_calc.out` §2) [D], close to the patch's own ±1.3 GHz half-bandwidth. Without
    60 GHz metrology the conventional column may also be off-centre; see D12.

**Cost.**

- Electronics: about $29.6 [D, RR §8].
- Whole sensor: about $55 [D] with the requirements' PCB, radome, shield and mounting allocations,
  against the $85 target (SYS-01).
- The PCB line is still an allocation: no hybrid price is public (§2.3).

**What Rev A can prove.**

- G1 board items: the 3-TX supply, power, thermal and regulatory timeline.
- Radar-level 60 GHz evidence: patterns, steering, leakage, detection, plus A/B against a TI EVM.
- It **cannot** close G2, RF-03/RF-04 at reference planes or ANT-02 return loss. Those wait for
  booked mm-wave metrology (RF-08).

**When [E].** (Revised in review, §12 and §14.3. The original dates were Oct 23–27 / Oct 28 / Nov 6 / late
November.)

- Floorplan render (placement only, labelled "not routed"): about **Oct 13**.
- First routed board render: about **Oct 24–28**.
- Rev A0 orderable: about **Oct 30**.
- Rev A orderable package (the files plus a staged PCBWay quote request): about **Nov 10**. The owner
  approves the PCBWay order only after Rev A0 passes power-up and boot (B2/B3), about **Nov 14** (D13).
- Rev A boards in hand: about **mid-December** (P50); early January if PCBWay has to buy the Rogers
  materials (P90).
- §12 has the critical path.

### 0.1 Decisions for the owner (recommendation in bold)

| ID  | Decision                                                                                                                                                                                                                                                   | Recommendation and basis                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D0  | Order the full Rev A radar before G2 closes. This departs from the requirements' §10, "fund G0–G2 before the full radar"                                                                                                                                   | **Yes.** With no 60 GHz metrology available, the only 60 GHz evidence the project can produce is radar-level. The Rev A lot also carries the G2 coupons. Rev A is labelled an engineering build, not a G5 or DEMO-01 build (§1)                                                                                                                                                                                                                                                                                                                                              |
| D1  | Rev A0 JLC FR-4 bring-up board, about $250–350 [E]                                                                                                                                                                                                         | **Yes**, but only together with D13. It validates the schematic, footprints, power tree, boot, firmware and the yapnr flow about 4–5 weeks before Rev A arrives, using radios in JLC stock (§1.2)                                                                                                                                                                                                                                                                                                                                                                            |
| D2  | Reserve radios now: 5 into a JLC parts inventory for A0, and 7 from LCSC (C2866258) to consign to PCBWay. _Review:_ LCSC's page shows $21.86 (1+) / $21.22 (10+), 24 in stock (re-read 2026-10-03); $21.66 / $21.02 came from the JLC parts search [RR §1] | **Yes, and buy about 20, not 12** (about $425 at $21.22). The 24 units are the only commodity stock (TI out of stock with a purchase limit, Digi-Key 26 weeks), and Rev B plus G5 (≥ 10 units over two lots) need about 10 more [RR §1]                                                                                                                                                                                                                                                                                                                                      |
| D3  | Bench kit for G0/G1: IWR6843ISK ($183.75), DCA1000EVM ($691.85), LP-XDS110 ($35.09), total $910.69                                                                                                                                                         | **ISK rather than LEVM.** It uses the same LP87524J PMIC and the same RO4835 RF layer as Rev A [RR §2]. An oscilloscope with an FFT is also needed (§10.4)                                                                                                                                                                                                                                                                                                                                                                                                                   |
| D4  | Stackup option B: RO4450F 4 mil bondply on L2–L3, with L2 windows under the radiators and the TX feeds                                                                                                                                                     | **Yes for the radiators; review recommends no windows under the TX feeds** (§14.4 R3). Windows under the patches fix most of the bandwidth gap (2.1 → 4.3 % [D]). Windowed TX feeds save about 0.8 dB [D] that the EIRP cap throws away anyway, at the cost of three ground-step transitions, R ≥ 1.5 mm meanders that need up to about 6 × 5 mm each [D], more radiation, and a larger L3 GND area. PCBWay lists RO4450F for pure-Rogers multilayers and "ROGERS4403 series"/Shengyi prepreg for Rogers + FR-4 builds [PW-cap], so the quote must confirm the material (O1) |
| D5  | Column topology. The requirements ask for a corporate divider. If the corporate column fails its openEMS gate by phase P2, Board A may fall back to TI's proven series-fed 2-patch column                                                                  | **Corporate first, but decide by Oct 9 on a drawn geometry, not on Oct 17.** At d = 2.342 mm and W = 1.6 mm the inter-column gap is 0.74 mm; the column's input line must run in it, 0.27 mm (4 mil line) or 0.15 mm (8 mil line) from both neighbouring patches [D] (§14.4 R2). The floorplan, phase centres and isolation spacing all depend on this choice. Series fallback still needs your sign-off                                                                                                                                                                     |
| D6  | RF-03 TX waiver: up to 1.8 dB from ball to column input, against a 1.5 dB target                                                                                                                                                                           | **Accept, widened to ≤ 2.7 dB (central prediction 2.4–2.6 dB [D]) if D4 drops the TX-feed windows**, with ≥ 0.5 dB less excess loss preregistered as the DEMO-01 optimizer target (§6.3). TX loss is absorbed by the EIRP backoff (§10.3)                                                                                                                                                                                                                                                                                                                                    |
| D7  | PCBWay: request the Rogers quote (it needs your account), approve it, ship the consigned radios                                                                                                                                                            | Required. yapnr stages the files only and never logs in or pays (§9)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| D8  | Book a 60 GHz measurement path (RF-08): a 67 GHz VNA rental or lab session with GSG probes                                                                                                                                                                 | Required before G2. Candidates are in [RRF §4.3]; I contacted no one                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| D9  | Radar test range: two trihedral corner reflectors (48.6 mm and 27.3 mm edges [D]), a rotation stage and some absorber, about $300–600 [E]                                                                                                                  | **Yes.** This is development equipment (RF-08), not BOM                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| D10 | Firmware home. It builds on TI's MMWAVE-SDK, which carries TI's licence terms, not AGPL                                                                                                                                                                    | **A separate repository**, not yapnr, as decided for the 5.8 GHz IQ firmware                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| D11 | Rule for adding Board B to the Rev A panel (§6.5)                                                                                                                                                                                                          | **Accept the mechanical gate.** Nobody hand-picks or hand-tunes Board B                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| D12 | _Added in review._ Frequency bracketing: two of the six assembled Rev A boards carry the same generator's columns with patch length scaled −1.8 % and +1.8 % [D] (declared parameter, no hand tuning)                                                      | **Yes**, unless the PCBWay quote charges heavily for extra panel designs. With no 60 GHz metrology, the sub-band two-way gain test (B6) then shows which centre frequency the process actually delivers, and Rev B gets a measured correction instead of a guess (§14.4 R1)                                                                                                                                                                                                                                                                                                  |
| D13 | _Added in review._ Gate the Rev A order on Rev A0: request the PCBWay quote when the package is ready (about Nov 10), approve it only after A0 passes B2/B3 (power, PGOOD→NRESET, PMIC dump, boot)                                                         | **Yes.** On the original dates A0 arrives after Rev A is ordered, so A0 could not catch a schematic or footprint error before the expensive lot. The PCBWay manual quote and CAM reply take days anyway; the gate costs about 0–4 days [E]                                                                                                                                                                                                                                                                                                                                   |

---

## 1. Product definition for Rev A

### 1.1 What Rev A is

Rev A is an **engineering build** of the complete sensor electronics plus the conventional antennas,
made on the intended production stackup class (RF-01).

What it serves:

- G1 board-level proof on our own hardware, after the same items have been proven on the TI ISK.
- The first radar-level 60 GHz measurements.
- The same-lot coupons that G2 needs.
- G4 evidence: yapnr generates and routes the board with frozen contracts, mechanical selection and
  no hand-edited copper.

What it is not:

- the released product (G5);
- the DEMO-01 comparison pair, unless Board B passes its gate (§6.5).

### 1.2 Rev A0 (optional, JLC FR-4)

Rev A0 uses the same atopile source, the same contracts and the same PnR flow. It changes only the
fab profile and the stackup, and the RF macro regenerated for that stackup.

|                       | Rev A0 (JLCPCB)                                                                                                                                                | Rev A (PCBWay)                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Purpose               | Bring-up: power tree, 3-TX supply, boot/flash, UART/CAN, firmware, PMIC spur, LVDS capture, fab/order pipeline                                                 | Everything A0 does, plus RF-01-conformant antennas, coupons for G2, and radar-level 60 GHz evidence |
| Stackup               | JLC06161H-3313: L1–L2 3313 prepreg 0.0994 mm (Dk 4.1), FR-4, ENIG [YAP fab data]                                                                               | §4 hybrid, ImAg                                                                                     |
| RF consequence [D]    | RX path 1.4–1.6 dB; TX path 4.5–4.7 dB; patch efficiency about 51 %. TX loss is recovered by reducing the backoff (EIRP-capped). RX SNR about 3 dB below Rev A | Per §6                                                                                              |
| Radios                | JLC turnkey from stock (C2866258)                                                                                                                              | Consigned (D2)                                                                                      |
| Counts as RF evidence | No. RF-01 calls FR-4 "a cost-down experiment, not an interchangeable substitute"                                                                               | Yes                                                                                                 |

### 1.3 Requirements coverage

| Requirement                     | Rev A verifies  | How                                                                                     | Waits for                                                                  |
| ------------------------------- | --------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| SYS-01 cost                     | Partly          | Exact-part BOM, real Rev A prices                                                       | G5 quotes at 10k (PCB, radome)                                             |
| SYS-02 commodity                | Yes             | Catalog parts, PCBWay standard hybrid service; no RF connector in the product           | —                                                                          |
| SYS-03 domain                   | Partly          | Recorded test runs at 0.5–20 m, dry conditions                                          | Vehicle runs; 0–50 °C characterization                                     |
| SYS-04 provenance               | Yes             | All RF copper comes from yapnr generators (§6), hashed into the macro                   | —                                                                          |
| SYS-05 envelope/power/output    | Yes             | 5 V input, measured peak/average power, TJ at 50 °C (hot box), latency, output message  | Final enclosure                                                            |
| ARCH-01 radio                   | Yes             | Suffix and silicon frozen at G1 (AQGABLR, marking 678A)                                 | —                                                                          |
| ARCH-02 modes                   | Yes             | Both firmware modes on the board                                                        | Final calibration                                                          |
| ARCH-03 3-TX supply             | **Yes**         | Rail current (PMIC readout plus Kelvin shunt), ripple, sequencing in the worst TX state | —                                                                          |
| ANT-01 geometry                 | Partly          | Exported phase centres; two-way patterns per virtual pair                               | Full-wave embedded patterns (solver evidence)                              |
| ANT-02 radiation                | Partly          | Pointing ±5° and coverage from two-way patterns; relative gain against a TI EVM         | **Return loss** (60 GHz metrology); chamber patterns with the final radome |
| ANT-03 honest angle             | Yes             | Firmware reports azimuth only; no elevation output                                      | —                                                                          |
| RAD-01…10                       | Preliminary     | Rev A runs with the test radome and corner reflectors                                   | Final radome and frozen firmware (G5)                                      |
| RF-01 stackup contract          | Yes             | Frozen stackup file, fab notes, CD report, microsection                                 | Measured 60 GHz material values (G2)                                       |
| RF-02 synthesized classes       | Partly          | Conventional classes generated; optimized divider and TX feed on coupons                | Optimized column (Rev B unless the gate passes)                            |
| RF-03/RF-04 at reference planes | **No**          | —                                                                                       | Coupons plus booked metrology (G2)                                         |
| RF-05                           | Yes (by design) | Microstrip/GCPW only; optional stripline coupon (§8)                                    | —                                                                          |
| RF-06                           | Partly          | Erosion/dilation and Dk/thickness corners in the sweeps                                 | Measured process corners                                                   |
| RF-07 coupling/leakage          | Partly          | Measured desense (TX on/off), ADC headroom, zero-range leakage                          | Port-level coupling (metrology)                                            |
| RF-08 metrology                 | Coupons only    | Probe-ready 60 GHz coupons; LibreVNA coupons up to 6 GHz                                | **Owner booking (D8)**                                                     |
| PNR-01…06                       | Yes             | §7                                                                                      | —                                                                          |
| REG-01/02                       | Partly          | Firmware envelope, GPIO timeline, a second unit as a 60 GHz sniffer, relative EIRP      | Calibrated EIRP (compliance lab)                                           |
| REG-03/04                       | Design yes      | No external phase-lock or sync input exposed; US-only profile                           | G1 regulatory memo                                                         |
| DEMO-01/02                      | No / partly     | —                                                                                       | Rev B pair (§6.5); raw data and releases                                   |

---

## 2. Parts, BOM and sourcing

### 2.1 Production parts (Rev A fit unless noted)

Unit prices are at the quantity shown. SYS-01 asks for a 10k basis, but no 10k prices are public, so
1k prices stand in.

| Ref     | Function                                                                                         | Exact part                                                                                                                                                                           | Sourcing (2026-10-03)                                                                                                                                         | Qty | Unit $         | Basis         |
| ------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- | -------------- | ------------- |
| U1      | 60–64 GHz radar SoC, external antenna                                                            | **IWR6843AQGABLR** (A = silicon PG2.0, Q = non-functional-safety, G = general, ABL = 161-ball FCBGA 10.4 mm, R = reel). Prototype alternative: IWR6843AQGABL (tray)                  | LCSC/JLC C2866258: **24 in stock**; LCSC $21.86 (1+), $21.22 (10+) (review re-read); JLC search $21.66 / $21.02 [RR §1]. TI: out of stock. Digi-Key: 26 weeks | 1   | 20.86          | TI 1k [RR §1] |
| U2      | PMIC: 4 × 4 MHz bucks, LDO-bypass rail set                                                       | **LP87524JRNFRQ1** (OTP: Buck0 3.3 V/1.5 A, Buck1 1.2 V/1.5 A, Buck2 **1.0 V/4 A** "RF, with ferrite filter", Buck3 1.8 V/2.5 A) (checked [TI-PMIC]). **Never LP87524B or LP87524P** | JLC C701982: 187 in stock, $2.09 at 1k                                                                                                                        | 1   | 2.35           | TI 1k         |
| L1–L4   | Buck inductors 0.47 µH, 4.5 A, 0805                                                              | Taiyo Yuden MCKK2012TR47M                                                                                                                                                            | JLC C655211: **39 in stock** (A0 needs 20)                                                                                                                    | 4   | 0.12           | JLC 3k        |
| FB1–FB8 | RF-rail ferrite LC filters                                                                       | TDK MPZ2012S101AT000 (100 Ω, 4 A) ×4; Murata BLM15PD300SZ1D ×4                                                                                                                       | JLC C15957: 136k in stock                                                                                                                                     | 8   | 0.02           | JLC           |
| Y1      | 40 MHz crystal, CL 8 pF, ESR ≤ 50 Ω, ±200 ppm total [TI-DS Table 7-5]                            | Kyocera **CX3225SA40000D0PTWCC** (TI EVM part). A0 alternative: Yangxing X3S040000B81H-R (C254377, 2,927 in stock); check its ESR and temperature grade first                        | JLC C2908116: 0 in stock; Digi-Key stocks it [RR §4]                                                                                                          | 1   | 0.23           | JLC 1k        |
| U3      | 16 Mbit QSPI flash, 80 MHz on all commands                                                       | Macronix **MX25V1635FZNQ** (TI-tested). Fallback MX25R1635FZUIH0 (C5687033, 8,088 in stock, $1.84 at 1k); its 80 MHz boot must be checked at G0                                      | JLC C2908148: 0 in stock, $0.98                                                                                                                               | 1   | 0.98           | JLC 100+      |
| U4      | CAN-FD transceiver                                                                               | TI **TCAN1044AVDRBRQ1** (VCC 5 V, VIO 3.3 V)                                                                                                                                         | JLC C3234119: 36,481 in stock                                                                                                                                 | 1   | 0.23           | JLC 1k        |
| D1      | CAN bus ESD                                                                                      | TI ESD2CAN24DBZRQ1                                                                                                                                                                   | JLC C5736151                                                                                                                                                  | 1   | 0.14           | JLC           |
| D2      | UART ESD                                                                                         | TI TPD2E2U06DCKR                                                                                                                                                                     | JLC                                                                                                                                                           | 1   | 0.12           | JLC 6k        |
| U5      | Input eFuse: OVLO about 5.5 V (review; was 5.6 V), ILIM about 2 A circuit breaker, PG output     | TI **TPS259474ARPWR** (adjustable OVLO, circuit breaker, auto-retry; checked [TI-EFUSE] Table 4)                                                                                     | JLC C3662807: 498 in stock, $1.77 at 1k                                                                                                                       | 1   | ⎫ 1.79         | JLC 1k/3k     |
| D3      | Input TVS                                                                                        | SMF5.0A                                                                                                                                                                              | JLC                                                                                                                                                           | 1   | ⎭              |               |
| J1      | Power and data connector: 5 V, GND, CANH, CANL, UART TX, UART RX                                 | JST **SM06B-GHS-TB(LF)(SN)** (GH, 1.25 mm, right-angle SMD)                                                                                                                          | JLC C133065: 1,778 in stock, $0.17 at 5k                                                                                                                      | 1   | ⎫ 0.21         | JLC           |
| D4      | Status LED                                                                                       | 0603 LED                                                                                                                                                                             | JLC basic                                                                                                                                                     | 1   | ⎭              |               |
| R_SH    | 1.0 V RF/PA rail Kelvin shunt footprint (0612)                                                   | 0 Ω on product builds; **2 mΩ** on development builds (review; was 5 mΩ; ISK Rev D value, §3.3)                                                                                      | —                                                                                                                                                             | 1   | ≈0             | —             |
| C, R    | About 55 MLCCs and about 30 resistors, values per TI ISK Rev D/LEVM; VBGAP 47 nF (errata ANA#19) | Various                                                                                                                                                                              | JLC                                                                                                                                                           | —   | 2.06 [D]       | [RR §8]       |
|         | **Electronics subtotal**                                                                         |                                                                                                                                                                                      |                                                                                                                                                               |     | **≈ 29.6 [D]** |               |

**Parts to avoid** [RR §1, RF §5]:

- Any **IWR6843AR…ALP** part (antenna-on-package, FCBGA-180). JLC holds 2,232 of IWR6843ARQGALPR,
  and that stock is the trap.
- IWRL6432 and IWRL6844 (ARCH-01).
- LP87524B and LP87524P. Their OTP defaults are wrong for this tree: on the B part, Buck2 and Buck3
  come up at 1.8 V and 2.3 V.

### 2.2 Development-only parts (not in the BOM; SYS-01 excludes development equipment)

| Ref  | Function                                                         | Part                                                                                                                                               | Source                                   |
| ---- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| J2   | DCA1000EVM raw-ADC header (60-pin, 0.5 mm)                       | Samtec **QTH-030-01-L-D-A**, the part DCA1000EVM's guide names for the xWR EVM side, mated through an HQCD-030 cable (checked [TI-DCA])            | JLC C2843756: 930 in stock, $5.21 (100+) |
| J3   | JTAG (TCK P10, TMS N10, TDI R11, TDO N13)                        | 10-pin 1.27 mm Arm Cortex debug footprint, DNP, for the LP-XDS110's "XDS110 out" cable. **Pinout to be verified** against the LP-XDS110 guide (O7) | —                                        |
| TP\* | Rail, reset, SOP, UART, CHIRP/FRAME_START test pads              | Pads only                                                                                                                                          | —                                        |
| —    | Test radome (§5.4), standoffs, corner reflectors, rotation stage | —                                                                                                                                                  | D9                                       |

**Shield can (ANA#21).** TI's workaround is "shield around the device (excluding antenna region)" or
an absorber on the device top (checked [TI-ERR] ANA#21). The RF balls leave the package edge with
their 50 Ω point only about 1.3 mm out. A generic frame such as the Würth WE-SHC 36103205S
(20.4 × 20.4 × 2.5 mm, two-piece [WE]) would therefore sit across all seven feeds.

What Rev A does about it:

1. no can;
2. an absorber-pad option on the IWR6843 lid. _Review:_ the RX columns' lower edges are about 2.25 mm
   from the package edge, and the 1.17 mm package alone already uses 2.0 mm of the §5.2 30° rule [D].
   An absorber pad thicker than about 0.1 mm breaks that rule and sits in the RX bank's near field; it
   also insulates the package top (§14.4 R7). Treat it as a bench experiment only, never a Rev A fit;
3. measurement of the 14.4 and 28.8 GHz spurs at G1 (15.209 limits apply below 40 GHz,
   15.255(d)(2) [CFR]);
4. a custom half-can with RF mouseholes for Rev B only if the spur fails.

The $2 thermal/shield allocation stays in the BOM.

### 2.3 Sensor cost against SYS-01 (USD per unit)

| Line                                                | Nominal                                             | Basis                                                                                                                                                                                                                                 |
| --------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Electronics (§2.1)                                  | 29.6 [D]                                            | TI/JLC 1k prices                                                                                                                                                                                                                      |
| PCB (6-layer hybrid, 27.6 cm², including RF copper) | 16 (allocation; range 10–25)                        | **Unquoted**: PCBWay blanks the online price for Rogers [RF §0]. Cost-down routes: a partial hybrid (Rogers only under the 19 × 9 mm RF region; PCBWay offers "partially hybrid") or an LEVM-like FR408HR build (an RF-01 experiment) |
| Radome and enclosure                                | 6 (allocation)                                      | §5.4                                                                                                                                                                                                                                  |
| Thermal/shield                                      | 2 (allocation)                                      | Above                                                                                                                                                                                                                                 |
| Mounting hardware (4 × M2.5)                        | 1 [E]                                               | —                                                                                                                                                                                                                                     |
| **Total**                                           | **≈ 55 [D]**; all ranges at their high end ≈ 70 [D] | Meets < $100 and the $85 target on allocations. COST-01 (review at ≥ $85) is not triggered. G5 needs dated quotes                                                                                                                     |

The requirements' $18.963 radio anchor is stale: TI's 1k price is now $20.859, still under the $22
allocation [RR §1].

---

## 3. Block diagram and schematic plan

### 3.1 Block diagram

```
 J1 (JST GH 6)                                                              RF macro RFM1 (yapnr-generated copper)
  5V ─► D3 SMF5.0A ─► U5 TPS259474A ──► 5V_SYS ─┬─► U2 LP87524J ─ Buck0 3V3 ─────────► VIOIN, flash, TCAN VIO, LED
          (TVS)        OVLO≈5.5V, ILIM≈2A [D]   │     (I2C 0x60) ─ Buck1 1V2 ─────────► VDDIN, VIN_SRAM, VNWA
                        PG ──► U2 EN            │                ─ Buck2 1V0 ─FB─R_SH─► VIN_13RF1, VIN_13RF2, VOUT_PA (3-TX bypass)
                                                │                ─ Buck3 1V8 ─FB(×4)──► VIN_18BB, VIN_18VCO, VIN_18CLK, VIOIN_18, VIOIN_18DIFF
                                                │      PGOOD ──(RC)──► U1 NRESET
                                                └─► U4 TCAN1044A VCC (5 V)
 CANH/CANL ◄── D1 ESD2CAN24 ◄── U4 ◄── H14 (CAN_FD_TX, mode 9) / F14 (CAN_FD_RX, mode 8) ──┐
 UART TX/RX ◄── D2 TPD2E2U06 ◄────── N5 RS232_TX / N4 RS232_RX (also the ROM flashing port) ─┤
                                                                                              │
                     Y1 40 MHz ── B15/C15        U3 flash ── QSPI R12,P11,R13,N12,R14,P12 ─┤
                     I2C G14/F13 ── U2           J3 JTAG (DNP)   SOP straps P9/G13/N13 ─────┤
                     J2 QTH-030 (DNP) ◄─ LVDS J14/J15,K14/K15,L14/L15,M14/M15; SPIA E13/E14/E15/D13
                                                                                              │
                                                                     U1 IWR6843AQGABLR ──────┘
                                  TX1 B4, TX2 B6, TX3 B8 ─► launch ─► equal-length feed ─► TX columns (3 × 2 patches)
                                  RX1 M2, RX2 K2, RX3 H2, RX4 F2 ◄─ launch ◄─ feed ◄────── RX columns (4 × 2 patches)
```

### 3.2 Pin plan (checked against SWRS219F Table 6-1 [TI-DS])

| Function                      | Balls (mode)                                         | Note                                                                                                                                                                                                                                                                                                 |
| ----------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TX1/TX2/TX3                   | B4/B6/B8                                             | Single-ended; launch fixed in RFM1                                                                                                                                                                                                                                                                   |
| RX1/RX2/RX3/RX4               | **M2/K2/H2/F2**                                      | `research-radio.md` lists these in reverse order; the data sheet maps RX1 = M2                                                                                                                                                                                                                       |
| Product UART and ROM flashing | N4 RS232_RX, N5 RS232_TX                             | Field reflash over J1 with SOP = 101                                                                                                                                                                                                                                                                 |
| **CAN-FD**                    | **H14 TX (mode 9), F14 RX (mode 8)**                 | _Changed from E14/D13._ E14/D13 are SPIA_MISO/MOSI. mmWave Studio needs SPIA to control the radar through the DCA1000 header, and TI's LEVM shares them through an analog mux [TI-LEVM SWRR178]. H14 also has a QSPI_CLK_EXT option; leave it unused. 0 Ω DNP options keep E14/D13 open for CAN (O3) |
| SPIA (mmWave Studio over J2)  | E13 CLK, E15 CS, E14 MISO, D13 MOSI                  | Development builds only                                                                                                                                                                                                                                                                              |
| I2C to PMIC                   | G14 SCL, F13 SDA (mode 2)                            | Spread spectrum and current readout                                                                                                                                                                                                                                                                  |
| QSPI                          | R12 CLK, P11 CS, R13/N12/R14/P12 D0–D3               | 80 MHz; 3-byte addressing; SFDP                                                                                                                                                                                                                                                                      |
| LVDS (J2)                     | TX0 J14/J15, TX1 K14/K15, CLK L14/L15, FRCLK M14/M15 | 2 lanes; about 410 Mbps per lane needed for the §4.1 profile [D]; use 600 Mbps                                                                                                                                                                                                                       |
| JTAG                          | TCK P10, TMS N10, TDI R11, TDO N13                   | J3 (DNP)                                                                                                                                                                                                                                                                                             |
| SOP2 / SOP1 / SOP0            | P9 (PMIC_CLKOUT) / G13 (SYNC_OUT) / N13 (TDO)        | Production 001: SOP0 pull-up, SOP1 and SOP2 pull-down. Pogo pads for 101 (flash) and 011 (debug)                                                                                                                                                                                                     |
| PMIC_CLKOUT → LP87524 CLKIN   | P9 via a 0 Ω **DNP**                                 | Spread spectrum needs the PMIC's internal RC oscillator (PLL_MODE = 00) [TI-PMIC §7.3.1.4]. Sync and spread spectrum exclude each other: spread spectrum by default, sync as a G1 experiment. CLKIN is otherwise tied to ground                                                                      |
| NRESET                        | R3                                                   | From PMIC PGOOD through RC, plus a test pad                                                                                                                                                                                                                                                          |
| NERROR_OUT / WARM_RESET       | N6 / N9                                              | Test pads; NERROR_OUT also drives the LED through a FET                                                                                                                                                                                                                                              |
| FRAME/CHIRP_START             | N8 (MCU_CLKOUT, mode 7 FRAME_START)                  | Test pad for the REG-01 timeline                                                                                                                                                                                                                                                                     |
| VBGAP                         | B10                                                  | 47 nF (errata ANA#19)                                                                                                                                                                                                                                                                                |
| VOUT_14APLL / VOUT_14SYNTH    | A10 / B13                                            | Caps only (internal LDO outputs)                                                                                                                                                                                                                                                                     |
| SYNC_IN / OSC_CLKOUT          | P4 / A14                                             | **Not routed off-board.** REG-03 and 15.255(h): no external phase-locking input [CFR]                                                                                                                                                                                                                |
| VPP                           | L13                                                  | Left unconnected (non-secure part) [RR §3]                                                                                                                                                                                                                                                           |

### 3.3 Power tree and intent

- **3-TX mode (ARCH-03).** The 1.0 V rail must feed **VIN_13RF1 (G5/H5/J5), VIN_13RF2 (C2/D2) and
  VOUT_PA (A2/B2)** (checked [TI-DS §6.2.2, §8.3.1]).
  - Limits: 0.95–1.05 V, 1.4 V absolute maximum, **2.5 A peak**.
  - VOUT_PA sits at the package corner between the TX and RX edges; after rotation it is at
    U1-relative (+3.9…+4.55, +3.9) [D]. Its decoupling sits in a reserved pocket of RFM1 (§5.2),
    as on TI's LEVM.
  - _Review: DC budget for the ±50 mV window [D] (`review_calc.out` §5)._ LP87524 Buck2 is ±2 % DC
    (PWM, VOUT ≥ 1 V) and ±3 % for a 0→2 A step [TI-PMIC §6.5]; MPZ2012S101AT000 is 20 mΩ [LCSC
    C15957]. Sensed at the buck output, one ferrite carrying 2.5 A plus a 5 mΩ shunt drops about
    66 mV, so the balls see about 0.92 V nominal and 0.88 V worst case, below the 0.95 V minimum.
    - Fix: split the rail as on ISK Rev D (separate ferrites for the RF1 and RF2/PA branches), put
      two ferrites in parallel on the PA branch, and use a **2 mΩ** shunt (the ISK Rev D value, R199/R200
      [TI-ISK-SCH]) on development builds only. That gives about 28 mV of IR drop (0.96 V nominal,
      0.92 V worst case).
    - Keep FB_B2 at the buck output (no ferrite inside the control loop) and add a DNP remote-sense
      option. Measure the minimum VOUT_PA during a 3-TX burst at B4; if it is under 0.95 V, raise
      Buck2 in 5 mV steps over I2C after checking the no-load maximum against 1.05 V.
  - _Review:_ ISK Rev D delays the radio's 3.3 V with a TPS22917 load switch ("Load switch for
    delaying 3.3V to IWR6843 module" [TI-ISK-SCH]). The data sheet gives no reason. Add a DNP load
    switch with a 0 Ω bypass on VIOIN (O13).
- **Sequencing.** SWRS219F §7.12.1 requires only that "all external voltage rails [are] stable
  before reset is deasserted" (checked). It sets no order between rails.
  - Plan: PGOOD (all four bucks) → RC (about 10 ms [E]) → NRESET.
  - The J part's OTP start-up delays are not in its data sheet; the delays it gives are only an
    example. Read `BUCKx_DELAY` and the PGOOD sources over I2C on the first A0 board. A DNP
    footprint for a reset supervisor is the fallback.
- **Rail current measurement (ARCH-03).**
  - The LP87524 measures load current (20 mA LSB, < 10 % above 1 A [TI-PMIC]). It gives averages
    over I2C.
  - The 0612 Kelvin shunt in the 1.0 V RF/PA rail gives scope waveforms of the 2.5 A bursts.
    _Review:_ 2 mΩ (was 5 mΩ), downstream of Buck2's FB sense point, so its 5 mV drop at 2.5 A is
    **not** regulated out and counts in the DC budget above [D]. 2.5 A × 2 mΩ = 5 mV full scale needs a
    differential probe or a ×10 amplifier footprint (O10).
- **Ripple.** Limits (µVrms for a −105 dBc spur) on the 1.0 V rail: 7 / 5 / 3 / 2 / 11 / 13 / 22 at
  137.5 / 275 / 550 kHz and 1.1 / 2.2 / 4.4 / 6.6 MHz [TI-DS Table 7-2] (review: the 275 and 550 kHz
  rows were missing).
  - The 4 MHz switching spur maps to **12.0 m** at 50 MHz/µs; with spread spectrum it moves to
    3.89 MHz, or 11.7 m [D]. Its second harmonic (8 MHz) falls at 24 m, beyond the 21.6 m IF limit
    [D].
  - Plan: ferrite LC filters, spread spectrum on at boot, and a G1 measurement with the radar's own
    ADC data (TI's method [RR §3]).
- **Power budget [D].** About 1.27 A peak from 5 V at the data sheet's maximum rail currents, against
  the 2 A allowance. About 1.0–1.3 W average for the IC (4 W limit). TJ is about 72–79 °C at 50 °C
  ambient with RθJA 22.3 °C/W (checked [TI-DS §7.11]; JEDEC 2S2P, so measure it).
  - _Review:_ 22.3 °C/W is a 76 × 114 mm JEDEC board in free air. A 60 × 46 mm board behind a radome
    and rear cover is more like 30–40 °C/W [E], which puts TJ at 89–102 °C for 1.3 W and up to 110 °C
    for 1.5 W (phased-TX mode with the DSP busy) [D], against TJ max 105 °C. Plan a GND via field under
    U1 into L2/L5, a bottom-side copper area that can touch the enclosure, and read the on-die sensors
    at SYS-05 verification (§14.4 R7).
- **eFuse.** The circuit-breaker ILIM is about 2 A, and ITIMER blanking allows peaks to 2 × ILIM
  [TI-EFUSE]. Its PG drives the PMIC enable.
  - _Review:_ OVLO changed from about 5.6 V to **about 5.5 V**. The LP87524's recommended maximum VIN
    is 5.5 V (absolute maximum 6 V) [TI-PMIC §6.3]. A 5 V ± 5 % supply still passes. The 115 %
    (5.75 V) condition of 15.255(f) then gives "no emission", which the regulatory memo must justify
    (O8).

### 3.4 atopile source plan (public yapnr example `examples/radar60/`; TI files are never committed)

```
examples/radar60/
  ato.yaml                    # builds: rev-a (pcbway hybrid), rev-a0 (jlc fr-4); dev/prod fit variants
  elec/src/
    interfaces.ato            # RfPort50 (rf + ref), Lvds2Lane, Qspi
    radar60.ato               # top level and annotations
    power_in.ato              # J1 power pins, SMF5.0A, TPS259474A (OVLO and ILIM resistors), bulk
    pmic_lp87524j.ato         # U2, L1–L4, input/output caps, ferrite LC filters, R_SH, PGOOD→NRESET RC, CLKIN option
    radio_iwr6843.ato         # U1 (part-cache footprint from SWRS219F mechanical data), decoupling per ISK Rev D,
                              #   Y1 + load caps, VBGAP 47 nF, APLL/SYNTH caps, SOP straps and pogo pads, test pads
    flash_qspi.ato            # U3 + series resistors at QSPI_CLK
    can_fd.ato                # U4, D1, split 120 Ω termination footprint (DNP), CAN pinmux options
    uart.ato                  # D2, J1 pins
    debug.ato                 # J2 QTH-030 (dev), J3 JTAG (DNP)
    rf_macro.ato              # RFM1: instance of the yapnr RF macro with 7 RF ports (sha256-pinned result)
    mech.ato                  # 4 × M2.5 holes, fiducials, radome standoff lands
  rf/specs/*.yaml             # RFS-1…RFS-6 (§6)
  rf/results/<id>/            # macro export, S-parameter files, result.json; sha256-pinned
  pnr/constraints.yaml        # §7.3
  yapnr-parts.lock.json       # part-cache lock (no EasyEDA-derived data)
```

**Annotations.**

- These forms exist in yapnr today: `@pnr-current`, `@pnr-si`, `@pnr-pair` and the N-0003
  `@pnr-noise-*` lines.
- These forms are proposed, as in the 5.8 GHz plan [T58 §7]: `@pnr-rf-macro`, `@pnr-rf-keepout`
  and `@pnr-rf-chain`.
- Every value below is [D] from §3.3, flagged for review.

```ato
# @pnr-current {"target":"board.power_in.vin","rms_current_a":0.6,"peak_current_a":1.5}
# @pnr-current {"target":"board.pmic.v1v0_rf","rms_current_a":1.0,"peak_current_a":2.5}     # ARCH-03, VOUT_PA included
# @pnr-current {"target":"board.pmic.sw_b2","rms_current_a":1.0,"peak_current_a":2.8}       # 2.5 A + 0.43 A p-p ripple / 2
# @pnr-current {"target":"board.pmic.v1v2","rms_current_a":0.6,"peak_current_a":1.0}
# @pnr-current {"target":"board.pmic.v1v8","rms_current_a":0.5,"peak_current_a":0.85}
# @pnr-current {"target":"board.pmic.v3v3","rms_current_a":0.1,"peak_current_a":0.2}
# @pnr-si {"target":"board.flash.qspi","bus":"qspi","f_mhz":80,"max_length_mm":25,"series_r_ohm":22,"ref":"GND"}
# @pnr-pair {"target":"board.debug.lvds_tx0","diff_ohm":100,"tol":0.1,"skew_mm":0.1}          # likewise tx1, clk, frclk
# @pnr-si {"target":"board.debug.lvds","group":["lvds_tx0","lvds_tx1","lvds_clk","lvds_frclk"],"group_skew_mm":2.0}
# @pnr-noise-source {"target":"board.pmic.sw_b0..sw_b3","kind":"switching","f_mhz":4.0,"spread":true}
# @pnr-noise-source {"target":"board.radio.y1","kind":"clock","f_mhz":40}
# @pnr-noise-budget {"target":"board.radio.v1v0_rf_pins","kind":"supply_ripple_uvrms","limits":{"0.1375":7,"0.275":5,"0.55":3,"1.1":2,"2.2":11,"4.4":13,"6.6":22}}   # review: 275/550 kHz rows added from SWRS219F Table 7-2
# @pnr-noise-budget {"target":"board.radio.y1","kind":"clock","min_distance_mm":{"board.pmic.sw_*":8}}
# @pnr-rf-macro {"target":"board.rf_macro","spec":"rf/specs/rfm1.yaml","result":"rf/results/rfm1-conv/result.json","sha256":"<pinned>","lock":"fixed","anchor":"U1","layers":["F.Cu","In1.Cu","In2.Cu"],"owns_vias":true}
# @pnr-rf-keepout {"target":"board.rf_macro","layers":["F.Cu","In1.Cu","In2.Cu"],"kind":"rf_region","vias":"macro_only","parts":"pocket_only"}
# @pnr-rf-chain {"name":"rx1","order":["radio.RX1","rf_macro.rx1_p0","rf_macro.rx1_p1"],"budget":{"il_db_max":1.5,"rl_db_min":10,"band_ghz":[60.3,63.8]}}
```

**Part cache.**

- U1's footprint is generated from the SWRS219F package drawing: 15 × 15 grid at 0.65 mm, 161
  balls, 0.32 mm non-solder-mask-defined lands. The depopulated sites follow Table 6-1.
- LP87524J (VQFN-HR 26), TPS259474A (VQFN-HR 10), QTH-030 and GH-6 come from datasheet drawings,
  each with provenance.

---

## 4. Stackup, design rules and fab profile

### 4.1 Rev A stackup (PCBWay Advanced PCB, "Rogers 4 series + FR-4 mixed"; vendor to confirm)

| Layer       | Copper                                                                                                                         | Dielectric below it                                                                                                                   | Role                                                                                                                                                                |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1 (F.Cu)   | 0.5 oz base (RO4835 LoPro reverse-treated foil), plated to ≤ 35 µm finished, **immersion silver**, LDI, no mask over RF copper | **RO4835 LoPro core 0.1016 mm** (process Dk 3.33 ± 0.05 at 4 mil, design Dk 3.66 for 8–40 GHz, Df 0.0037; thickness ±0.7 mil [RF §2]) | RF macro (launch, feeds, columns), IWR6843, ring 0–1 escape, parts                                                                                                  |
| L2 (In1.Cu) | 0.5 oz                                                                                                                         | **RO4450F bondply 0.1016 mm** (Dk 3.52, Df 0.004 [RF §4])                                                                             | GND: the RF reference for launches and RX feeds. **Windows cleared** under every patch and under the 8 mil TX feed segment (_review, D4:_ patches only recommended) |
| L3 (In2.Cu) | 0.5 oz                                                                                                                         | FR-4 core 0.51 mm (IT180A/S1000-2M class)                                                                                             | Under RFM1: solid GND (the patch and TX-feed reference). Elsewhere: ring 2 escape and signals                                                                       |
| L4 (In3.Cu) | 1 oz                                                                                                                           | FR-4 prepreg about 0.10 mm                                                                                                            | Power pours: 1V0_RF (2.5 A peak), 1V2, 1V8, 3V3, 5V_SYS                                                                                                             |
| L5 (In4.Cu) | 0.5 oz                                                                                                                         | FR-4 core 0.20 mm                                                                                                                     | GND, solid                                                                                                                                                          |
| L6 (B.Cu)   | 0.5 oz base, plated, ImAg                                                                                                      | —                                                                                                                                     | Signals, GND pour                                                                                                                                                   |

- Total ≈ 1.17 mm nominal (1.2 mm class) [D]. This is an asymmetric hybrid like TI's EVMs; PCBWay's
  CAM decides the balancing and warp control.
- **Fallback if RO4450F is not offered.** FR-4 prepreg on L2–L3 makes the 8 mil feed _worse_ than
  4 mil RO4835 (TX 2.3–2.6 dB [D]). In that case keep the TX feeds on 4 mil and windows under the
  patches only (patch about 4.1 % BW, lower efficiency [D]). PCBWay's page names its Rogers prepreg
  "ROGERS4403 series", so it must be confirmed (O1).
- **Critical dimensions.** A ±25 µm patch-length error moves resonance by ±1.30 GHz on 8 mil [D]. To
  get under the ±1 % (G2) target:
  - order the advanced width-tolerance tier (< ±10 %);
  - ask for a CAD-to-CAM RF critical-dimension report;
  - measure every lot's patch dimensions optically (§10.4);
  - carry a ±25 µm length ladder on the coupons (§8).
- _Review: the window dielectric is not 0.2032 mm._ Under an L2 window the bondply also fills the
  removed 17.5 µm of L2 copper, so the dielectric is core + L2 copper + pressed bondply ≈ **0.21–0.22 mm**
  [D] (`review_calc.out` §1). With the board_calc patch copper that moves resonance down by about
  0.3 GHz [D]. Start values and sweeps should use this composite, and the PCBWay CAM reply must state
  the pressed bondply thickness (part of RF-01's "thickness distribution").
- _Review: the ±10 % width tier does not bound the patch._ It is relative to line width (±20 µm on a
  0.2 mm line) but would be ±119 µm on a 1.19 mm patch. Ask for an absolute etch tolerance on L1
  (±13 µm target, ±25 µm limit) in the fab notes, and confirm whether PCBWay's "RO4835" is the LoPro
  (reverse-treated foil) product: the Rogers data sheet lists ED foil as standard [R-4835] (O1).

### 4.2 Design rules for yapnr (new profile `pcbway-adv-6l-rf`, status draft)

| Rule                                                           | Value                                                                                                                    | Source                                                                                                                                                                                                                                                         |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Track/space, general                                           | 0.10 / 0.10 mm                                                                                                           | PCBWay 4/4 mil [RF §1]                                                                                                                                                                                                                                         |
| Track/space in the BGA escape (one trace between ring-0 balls) | 0.10 / 0.10 mm (needs 0.30 of 0.33 mm)                                                                                   | [RF §3]                                                                                                                                                                                                                                                        |
| Via, default                                                   | 0.20 mm drill / 0.40 mm pad (4 mil ring, advanced tier)                                                                  | [RF §4]                                                                                                                                                                                                                                                        |
| Via, BGA interstitial (GND only)                               | 0.15 / 0.35 mm (gap to 0.32 lands = 0.125 mm [D])                                                                        | Review: PCBWay lists a 0.15 mm CNC minimum and "thickness to diameter ratio ≤ 8" as normal process [PW-cap, PW-adv]; 1.17 / 0.15 = 7.8 fits. The 0.35 mm pad needs the advanced 3–4 mil ring (standard ring is 6 mil). Confirm both for the Rogers hybrid (O2) |
| Via, RF fence (RFS-5)                                          | 0.15 / 0.30 mm at ≥ 0.40 mm pitch, two staggered rows where one row is not enough                                        | Review: the original ≤ 0.36 mm pitch is infeasible: 0.40 mm pads would overlap and 0.35 mm pads leave 10 µm [D]. A 0.45 mm pitch still leaves openings of about λg/10 [D]                                                                                      |
| Via, power                                                     | 0.30 / 0.55 mm                                                                                                           | [E]                                                                                                                                                                                                                                                            |
| BGA land                                                       | 0.32 mm NSMD, mask opening 0.42 mm, stencil 0.125 mm                                                                     | [TI-DS], [RF §3]                                                                                                                                                                                                                                               |
| Mask dam                                                       | ≥ 0.10 mm                                                                                                                | [E]                                                                                                                                                                                                                                                            |
| Mask over RF copper                                            | Open (macro-owned F.Mask polygons)                                                                                       | [TI-RF]                                                                                                                                                                                                                                                        |
| Edge clearance / hole-to-edge                                  | 0.30 / 0.50 mm                                                                                                           | yapnr default profile values                                                                                                                                                                                                                                   |
| Impedance                                                      | Not ordered as a controlled-impedance service; RF widths come from the macro, verified by the CD report and the coupons  | —                                                                                                                                                                                                                                                              |
| Assembly                                                       | Single-sided top by default; bottom side allowed only if the decoupling-loop audit fails top-only (decided mechanically) | §7                                                                                                                                                                                                                                                             |

**Data to add on `claude/fab-order`.**

- `stackups/pcbway-6l-ro4835-ro4450f.json`, with per-layer material, Dk at its stated frequency, Df,
  and `*_prior` values for 62 GHz.
- `profiles/pcbway-adv-6l-rf.json`, status draft.
- Rev A0 uses the existing `jlc-6l` profile and `jlc06161h-3313` stackup, plus a 0.09/0.09 mm
  BGA-region class (JLC allows 3.5 mil in BGA fan-out [RF §1]). The draft `jlc-6l` profile says
  0.127 mm today.

---

## 5. Board outline and floorplan

### 5.1 Outline and fixed items (board origin at the lower-left corner; +y is north, toward the antennas)

| Item                            | Pose                                                                                                  | Basis                                                                                                                              |
| ------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Outline                         | 60.0 × 46.0 mm, 1 mm corner radii (27.6 cm²)                                                          | Fits SYS-05's 100 × 70 mm with enclosure walls. Keeps ≥ 5 mm (≈ 1 λ0) of ground beyond the outer patches for pattern stability [D] |
| U1                              | **Fixed** at (26.0, 28.0), rotation 270° (RX edge north, TX edge east)                                | §5.3                                                                                                                               |
| RFM1                            | **Fixed**, generated in U1's frame                                                                    | §6                                                                                                                                 |
| Mounting holes                  | 4 × Ø2.7 mm plated, GND (M2.5), at (3.5, 3.5), (56.5, 3.5), (3.5, 42.5), (56.5, 42.5); 6.5 mm keepout | ≥ 12 mm from any patch [D]                                                                                                         |
| J1 (GH-6)                       | **Fixed** on the south edge, centred at x = 30, entry facing south                                    | Farthest point from the antennas                                                                                                   |
| J2 (QTH-030, development)       | Region x 10–42, y 4–18 (south of U1, on the LVDS side)                                                | LVDS leaves U1's south edge                                                                                                        |
| J3 (JTAG, DNP)                  | Region x 1–14, y 18–40 (west of U1, on the JTAG/UART side)                                            |                                                                                                                                    |
| PMIC block (U2, L1–L4, filters) | Region x 36–58, y 2–24                                                                                | ≥ 8 mm from Y1 (noise budget); 1V0 feeds U1 through the L4 pour                                                                    |

### 5.2 RF region (RFM1)

All coordinates are absolute [D].

- **Patch copper:** the RX bank occupies x 21.0–29.7, y 35.2–39.6; the TX bank occupies x 33.2–39.5,
  y 31.8–36.2.
- **RF region:**
  - (a) the RX fan, x 21.0–31.2, y 33.2–46.0;
  - (b) the TX feeds and bank, x 31.2–44.5, y 26.8–46.0;
  - plus a 5 mm ground margin west of the RX bank, to x = 16.0.
- **Inside the region:**
  - no parts, tracks or vias other than the macro's own on L1–L3;
  - L4–L6 are free, because the region's references (L2/L3) shield them;
  - no vias of any kind except the macro's fences.
- **Pocket:** x 29.7–32.1, y 33.4–34.4 (U1-relative x 3.7–6.1, y 5.4–6.4), for the
  VOUT_PA/VIN_13RF2 decoupling caps (0402, ≤ 0.6 mm tall), placed by the PnR.
  - The pocket keeps ≥ 0.5 mm from the RX4 feed and ≥ 1.04 mm from the nearest patch edges, which
    is the visibility rule for a 0.6 mm part [D]. That leaves room for 2–3 caps.
  - If the decoupling-loop audit needs more, the remaining VOUT_PA capacitance goes on L6 directly
    under the corner (the double-sided branch of §4.2).
- **Radome visibility rule [D]:** no part may rise above a 30° line from the nearest patch edge. A
  part of height h must stay at least 1.73·h away horizontally; the 4.25 mm GH connector, for
  example, needs ≥ 7.4 mm. Every region in §5.1 already satisfies this.

### 5.3 Why this floorplan (feed geometry, U1-relative mm [D])

- After rotation, RX1–RX4 sit at x = −2.6, −1.3, 0, +1.3 on y = +3.9, and TX1–TX3 sit at
  y = +2.6, +1.3, 0 on x = +3.9. The 50 Ω port planes P0 lie about 1.3 mm from the ball centres,
  per SPRACG5 [TI-RF].
- **RX bank, straight north.**
  - Columns at x = −4.163, −1.821, +0.521, +2.863 (d = 2.342 mm), so the lateral shifts are ±1.563
    and ±0.521 mm.
  - S-bends with R = 1 mm need 1.95 mm of rise; the longest path is 2.70 mm, and the others meander
    to match.
  - Column inputs (P1) sit at y = +7.2.
- **TX bank, north-east.** Columns at x = 8.0, 10.342, 12.684, with inputs at y = 3.8.
- **Why the TX feeds are long.** The TX balls lie on the edge next to the RX edge, so each TX feed
  has to turn 90°. Nested L-routes from a 1.3 mm pitch to a 2.342 mm pitch differ by 3.64 mm per
  column (Manhattan lengths 4.0 / 7.6 / 11.3 mm). Equalized to the longest, each feed is about
  **10.9 mm**.
  - Swapping the roles, so that RX feeds are the long ones, would put the loss into the noise
    figure. TX loss only uses up EIRP backoff that has to be thrown away anyway.
  - TI's LEVM uses the same arrangement: RX straight out, TX feeds turning, with a
    length-equalizing loop [RRF §1.2].
- **Isolation.** The nearest TX and RX column centres are 6.16 mm apart (1.27 λ0), with a fenced
  ground strip between them.
- **ANT-01 export.** Phase centres lie at the column centres: RX at y_c = 9.5 and TX at y_c = 6.1
  (U1-relative). The banks are offset by a common translation, and their azimuth axes are parallel.

### 5.4 Radome and envelope

- **Test radome for Rev A [D]:** a flat polycarbonate sheet, half-wave thick (1.42 mm, assuming
  εr ≈ 2.9), held at a λ0 air gap (4.83 mm) above L1 on standoffs at the four holes. The εr must be
  measured; the ≤ 6 GHz LibreVNA cavity or line method gives only a low-frequency value.
  - _Review:_ a sheet over the whole board covers J2, so the DCA1000 cable cannot be plugged in with
    the radome fitted, yet B6 needs raw capture with the radome. Make the test radome cover the north
    part only (y ≥ about 27 mm, all RF regions plus ≥ 5 mm), held on the two north holes plus two
    standoff lands added at about y = 27 mm (mech.ato).
- **Envelope [D]:** 1.2 mm board + 4.83 mm gap + 1.42 mm radome + about 8 mm rear cover, roughly
  16 mm, inside the 25 mm limit.
- The final radome is a G3 item: ANT-02 is verified with the final radome.

---

## 6. RF structures

### 6.1 Reference planes and ports (PNR-01, SYS-04)

- **Pb (vendor boundary):** the RF ball land on L1. The package and ball are TI's block. No package
  S-parameters are published, so the model ports at the land.
- **P0:** the 50 Ω GCPW plane about 1.3 mm from the ball centre [TI-RF], and the de-embedding plane
  for coupons.
- **P1:** the column input (the antenna-feed plane of RF-03). ANT-02's "external antenna port" is P1.
- The divider is part of the column, so its loss shows up in realized gain.
- Every structure's spec file declares its ports, planes, owned ground and keepouts, frequency
  masks, fab envelope and versioned S-parameter files (RF-02).

### 6.2 Conventional set (Board A baseline)

Each structure is "textbook" geometry from closed-form starting values. At most 3–4 parameters per
structure are then swept in openEMS over corners, and the winner is selected mechanically by the
masks.

| ID       | Structure                                                 | Geometry (start values [D])                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Source of method                                                                                                                           |
| -------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| RFS-1 ×7 | BGA ball → GCPW launch                                    | L1 ground pour around the RF land, circular L2 cut-out under the land, **through-via** ground vias at the depopulated sites (LEVM style; no microvias), GCPW w 0.200 / g 0.20 mm on 4 mil to P0. Swept: cut-out diameter, via positions, taper                                                                                                                                                                                                                                                                                                                                                                                                                                          | TI SPRACG5 §2.1 method; LEVM through-via precedent. TI's RO4835 transition used microvias, so this one has to be redesigned [RF §3]        |
| RFS-2 ×4 | RX feed P0 → P1                                           | 4 mil microstrip/GCPW, w 0.200 mm, S-bends R ≥ 1 mm, ~2.7 mm, equal delay                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Closed-form                                                                                                                                |
| RFS-3 ×3 | TX feed P0 → P1                                           | 1.5 mm of 4 mil GCPW, then a ground-step transition at the L2 window edge, then 8 mil microstrip (w 0.423 mm, over L3), arcs R ≥ 1.5 mm, 10.9 mm equal delay. _Review (D4):_ recommended instead: 4 mil GCPW with fences all the way (R ≈ 0.6 mm meanders, no ground step). TX1 needs +6.9 mm and TX2 +3.3 mm of meander; one R 1.5 mm period for TX1 takes about 6.0 × 5.2 mm, the GCPW version about 2.4 × 4.8 mm [D]. Neither is drawn yet: lay the three feeds out before freezing §5                                                                                                                                                                                               | Closed-form plus swept taper length                                                                                                        |
| RFS-4 ×7 | 2-patch column with a corporate divider                   | Inset-fed patches over L2 windows (8 mil: W 1.25–1.6, L ≈ 1.19 mm; centre spacing about λg ≈ 2.9 mm). Centre T with a 35 Ω λ/4 (4 mil: 0.353 mm) and 50 Ω arms; one arm λg/2 longer to drive both patches in phase. Input in the inter-column gap (0.74–1.09 mm wide, depending on W). _Review:_ the divider and input line must stay on 4 mil over solid L2 (an 8 mil 50 Ω line is 0.44 mm, leaving 0.15 mm to each neighbour at W = 1.6 [D]); the λg/2 arm (about 1.45 mm) has to meander in the 1.7 mm between the facing radiating edges; keep W/L ≥ 1.2 (W 1.25 / L 1.19 puts TM10 within about 5 % of TM01, a cross-polar risk). Draw it and run one openEMS solve before C1 (D5) | Standard 2-element E-plane array. The 100 Ω arm (0.085 mm) is below the fab minimum, so it uses 50 Ω arms behind a 35 Ω section [RRF §1.4] |
| RFS-4S   | Series-fed column (TI LEVM-style), coupon and D5 fallback | Two patches joined by a 0.10 mm link, re-centred for this stackup                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [RRF §1.2]                                                                                                                                 |
| RFS-5    | TX–RX isolation strip and fences                          | L1 GND strip with stitched L1–L2–L3 vias. _Review:_ 0.15/0.30 mm vias at ≥ 0.40 mm pitch, staggered double rows where needed (the original ≤ 0.36 mm pitch is not manufacturable, §4.2)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | —                                                                                                                                          |
| RFS-6    | Ground margins and window edges                           | ≥ 5 mm ground beyond the outer patches; L2 window margins swept                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | —                                                                                                                                          |

**Masks (60.3–63.8 GHz unless stated; design targets from RF-03/04/07 and ANT-02; [D] where derived):**

| Path / object         | Mask                                                                                                                                                                                                                                                 |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pb → P1, each RX path | IL ≤ 1.5 dB (prediction 0.8–1.0 dB central, 0.6–1.4 dB band [D]); \|S11\| ≤ −10 dB (design −15 dB)                                                                                                                                                   |
| Pb → P1, each TX path | IL ≤ 1.5 dB target; prediction **1.5–1.8 dB** central, 1.2–2.4 dB band with option B; about 2.4–2.6 dB central, 1.8–3.6 dB band with 4 mil GCPW feeds (review, D4) [D]; waiver D6                                                                    |
| Within a bank         | Amplitude imbalance ≤ 0.75 dB. Phase match at 62.05 GHz ≤ 5° before calibration (design). **Group-delay match ≤ 2 ps (design) / ≤ 7.9 ps (limit [D])**: a single-frequency calibration then leaves ≤ 5° across the band                              |
| Column at P1          | RL ≥ 10 dB (**preregistered: the conventional column misses at the band edges**). Realized broadside gain ≥ 5 dBi (prediction 7–8 dBi [RRF §1.5]). One-way gain at ±45° az ≥ G_bs − 6 dB (a working definition of "useful coverage" [D], to confirm) |
| Column phase centre   | Within ±50 µm of its lattice position in x [D]                                                                                                                                                                                                       |
| Bank, embedded        | Adjacent-column coupling reported. Active RL for the ±30° steering codes reported (target ≥ 8 dB [D]). **TX→RX isolation ≥ 27 dB, design ≥ 35 dB** [RRF §1.6]                                                                                        |
| Fab corners           | ±12.5 µm edge bias, Dk ±0.05, thickness ±17.5 % (RO4835) and ±10 % (RO4450F) (RF-06)                                                                                                                                                                 |

### 6.3 yapnr.rf-optimized variants (DEMO-01)

| ID    | Structure                                                                   | Feasible with the current engine?                                                                                                                                            | Rev A placement                                                  | Preregistered gain                                                                                                                                                                                                                  |
| ----- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OPT-D | 2-way divider plus λ/4 match, microstrip on 4 mil over solid L2             | **Closest to today's engine**: one copper layer, microstrip ports, infinite ground is acceptable where L2 is solid. Needs 50 µm pixels and the sub-pixel edge stage [RRF §2] | Coupon strip, back-to-back next to the conventional one          | **≥ 20 % less area at equal masks (primary)**; ≥ 0.2 dB less excess loss reported only. _Review:_ 0.2 dB on a back-to-back coupon is inside the repeatability of a 60 GHz probe measurement [E], so it cannot carry a DEMO-01 claim |
| OPT-T | TX feed network (3 paths, equal delay) including the ground-step transition | Needs a layered substrate (an L2 window over L3) and GCPW ports                                                                                                              | Coupon strip only if the engine features land by the freeze date | **≥ 0.5 dB less excess loss** than RFS-3 at equal delay match                                                                                                                                                                       |
| OPT-C | Column (multi-resonant radiator or detuned pair) for ANT-02 bandwidth       | Needs NTFF, layered substrate, finite copper thickness, whole-bank solves                                                                                                    | Board B, only through the §6.5 gate; otherwise Rev B             | RL ≥ 10 dB over the full 60.3–63.8 GHz with ≤ 0.5 dB loss of realized gain                                                                                                                                                          |

**Honest risk.**

- yapnr.rf has no validation above 13 GHz. At 60 GHz, copper thickness is 14–28 % of the dielectric
  height, roughness is near saturation, and one 50 µm pixel shifts resonance by 3.4 % [RRF §2].
- Any optimized structure is labelled a _solver prediction_ until G2 measures the coupons.
- Required before any optimized copper ships:
  - a three-grid trend;
  - an independent openEMS cross-check within 0.5 dB and 1 % in frequency;
  - regressions on TI's ODS and LEVM patches, which should land within 1 % of their cavity-model
    resonances (62.0 and 61.7 GHz [RRF §0]) or explain the difference.
  - _Review:_ add the ISK long-range antenna (RO4835 LoPro 4 mil, the only TI 60 GHz antenna on Rev A's
    own laminate; DXF in SWRC355 [RR §2]) as the first regression. ODS is RO3003 and LEVM is FR408HR,
    so neither checks the RO4835 Dk choice (3.33 process at 4 mil vs 3.66 design [R-4835]), which is
    the largest single term in the patch-frequency uncertainty (`review_calc.out` §2).

### 6.4 Compute plan (GCP Batch, C4D Spot, us-west4; radar cap $50, owner, 2026-10-03)

**Tools.**

- openEMS (GPLv3, run as an external process) for the conventional sweeps, the launch and bank
  sign-off.
- yapnr.rf (`claude/rf-topopt` @ 90f2dc2, C kernels from `claude/rf-kernels`) for the TO runs,
  submitted as `mc-eval` campaigns (`claude/rf-gcp` 47f910c) through `yapnr exp` with campaign
  labels `radar60-*`.
- openEMS needs a container image: either the Ubuntu 24.04 package or a source build. Which one
  works is still to be checked (O9).

| Campaign               | Content                                                                                      | Runs [D] | VM-hours [D] | Cost [D]                                                         |
| ---------------------- | -------------------------------------------------------------------------------------------- | -------- | ------------ | ---------------------------------------------------------------- |
| C0 calibration         | openEMS throughput on c4d-highcpu-8/16; TI ODS and LEVM patch regressions; three-grid launch | ~10      | ~3           | ≤ $1                                                             |
| C1 conventional sweeps | RFS-1, RFS-3 transition, RFS-4 (3–4 parameters, about 40 DOE points each) × 3 corners        | ~350     | 80–200       | $6–15                                                            |
| C2 sign-off            | Full TX and RX banks with feeds, finite board, test radome; 7 excitations; NTFF              | 7–14     | 20–40        | $3–6                                                             |
| C3 TO                  | OPT-D (3 grids, robust) and OPT-T if the engine is ready                                     | 4–8      | ~50–150      | $6–20                                                            |
| **Total**              |                                                                                              |          |              | **$16–42, target ≤ $35**, under the $50 cap with the kill switch |

Throughput is an assumption (about 100 M cell-updates/s per c4d-highcpu-8 for openEMS) until C0
measures it. If C0 comes in 2× slower, C1 drops to 2 corners.

_Review [D]:_ C2 looks 2–3× low. A 25 × 20 mm RF region at 25 µm in-plane with about 60 z-cells is
about 48 M cells; at about 10 fs steps a 1 ns ring-down is about 10⁵ steps, so one excitation is
about 13 h on c4d-highcpu-8 at the assumed rate. Seven excitations are about 90 VM-hours, or
$7–15. The cost still fits the cap; the wall-clock time (about half a day per excitation, in
parallel) has to sit inside P4.

### 6.5 Board B inclusion rule (mechanical)

Board B (same outline, ports, phase centres and stackup) joins the Rev A panel only if, by the
package freeze date (P4 start, §12):

1. OPT-C and OPT-T pass every §6.2 mask across the declared corners;
2. the three-grid trend converges, and openEMS agrees within 0.5 dB and 1 %;
3. full-board PnR with Board B's macro closes the §7.5 gates.

Otherwise Board B goes to Rev B together with a fresh Board A (DEMO-01: same lot, same stackup).
Nobody tunes Board B by hand to make the date.

---

## 7. PnR plan

### 7.1 What is fixed, constrained or free

| Item                                | Treatment                                                                                                                                                                                                      |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| U1 + RFM1                           | **Fixed** (§5.1). The macro is generated in U1's ball frame, so the two agree by construction; no anchor-to-part solver constraint is needed for Rev A. Macro copper is kept bit-for-bit and checked by sha256 |
| U1 block (hierarchical subcell)     | U1 + generated BGA fanout + decoupling + Y1 group. Laid out by the block stage under Monte Carlo, inside the hierarchy flow (memory: hierarchical subcells, mechanical selection)                              |
| J1, mounting holes                  | Fixed (edge and corner constraints)                                                                                                                                                                            |
| J2, J3, PMIC block, CAN/UART, eFuse | `region` constraints (§5.1). Placement is power-first for the PMIC (tight hot loops, short 1V0 path)                                                                                                           |
| Decoupling                          | Hard groups anchored to their balls, radius 1.5–3 mm. The VOUT_PA caps go in the RFM1 pocket                                                                                                                   |
| Y1                                  | Group anchored to B15/C15, ≤ 3 mm, no vias in the crystal nets, L2 solid beneath                                                                                                                               |
| Everything else                     | Free                                                                                                                                                                                                           |

### 7.2 BGA escape (ABL0161, through vias only) [RF §3]

- **Ring 0 and ring 1:** 42 signals route out on L1, one trace between balls at 0.10/0.10 mm. None of
  them may leave across the north or east RF edges; those edges carry only RF, GND and power balls.
- **Ring 2:** 17 signals and 2 power balls use dog-bones into the fully depopulated ring 3, with
  0.20/0.40 mm vias, and route on L3 or L6.
- **Interior power and GND:** vias to the L4 pours and to L2/L5. The interstitial sites take same-net
  GND vias only (0.15/0.35).
- **The 7 RF balls** have no vias; their ground vias belong to RFS-1.

**Implementation.** A deterministic **fanout generator** for this ball map (new, E4) emits the
dog-bone pattern as part of the U1 block. Monte Carlo chooses among its variants (stub direction per
quadrant). The router then connects the fanout ends. This follows TI's LEVM pattern (8 layers there).
Whether 6 layers is enough is decided by the first PnR runs, and 8 layers is the fallback stackup (O4).

_Review:_ the original text said "6 here, with no LVDS bulk on internal layers", but Rev A carries the
DCA1000 header and its LVDS. TI states that ISK Rev C went to 8 layers "mainly due to the addition of
the 60-Pin Samtec connector ... and the layout requirements for high speed LVDS traces", and that
"Rev B is still a valid reference for 6-layer board design" [TI-ISK-REV]. So O4 is a real risk, not a
formality. Keep J2 directly south of the LVDS balls (columns 14/15 face south after rotation) with the
four pairs on L1 or L6 only, and use ISK Rev B (6-layer) rather than the LEVM as the escape reference.
Two more escape rules: power balls on the RF edges (VIN_13RF2 C2/D2) escape inward to ring-3 vias,
never across the RX4 launch; and the U1 rotation must come from the part-cache footprint checked
against the LEVM IPC-356 pad coordinates, not from the data sheet's ball diagram, whose top/bottom
view is not stated in the text.

### 7.3 Constraint file sketch

```yaml
schema: v0
board:
  {
    outline: { w: 60, h: 46, corner_r: 1.0 },
    layers: 6,
    profile: pcbway-adv-6l-rf,
    stackup: pcbway-6l-ro4835-ro4450f,
    sides: single,
  }
fixed:
  U1: { x: 26.0, y: 28.0, rot: 270, side: top }
  RFM1: { frame: U1, lock: fixed } # rf_macro: proposed (E2)
  J1: { edge: south, x: 30.0, rot: 180, side: top }
  H1..H4: { at: [[3.5, 3.5], [56.5, 3.5], [3.5, 42.5], [56.5, 42.5]] }
region:
  - { members: [J2], x: [10, 42], y: [4, 18] }
  - { members: [J3], x: [1, 14], y: [18, 40] }
  - { members: [U2, L1, L2, L3, L4, 'FB*', 'C_PMIC*'], x: [36, 58], y: [2, 24] }
group:
  - { members: ['C_PA*'], region: rfm1.pocket, hard: true }
  - { members: [Y1, C_Y1a, C_Y1b], anchor: U1.B15, radius_mm: 3, hard: true }
  - { members: ['C_U1_*'], anchor_pins: true, radius_mm: 2.5, hard: true }
keepout:
  - {
      name: rf_region,
      polygon: rfm1.region,
      layers: [F.Cu, In1.Cu, In2.Cu],
      vias: macro_only,
      parts: pocket_only,
    }
net_class:
  gnd: { nets: [GND], plane_layer: [In1.Cu, In4.Cu] }
  pwr: { nets: [1V0_RF, 1V2, 1V8, 3V3, 5V_SYS], plane_layer: In3.Cu } # several pours on one layer
  bga: { nets: ['U1_*'], track_mm: 0.10, clearance_mm: 0.10, via: bga }
  lvds:
    {
      pairs: [LVDS_TX0, LVDS_TX1, LVDS_CLK, LVDS_FRCLK],
      diff_ohm: 100,
      skew_mm: 0.1,
      group_skew_mm: 2.0,
    }
  qspi: { nets: ['QSPI_*'], max_length_mm: 25 }
```

### 7.4 Engine gaps, effort [E] and fallbacks

| ID  | Need                                                                                                                    | Status (2026-10-03)                                                                | Effort             | Fallback (never hand routing)                                                                                                   |
| --- | ----------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| G-a | >4 layers, every plane formed                                                                                           | `claude/gap-stackup` d1a186f: 12/12 chaser stackup rungs clean; not yet integrated | integration, 1–2 d | —                                                                                                                               |
| G-b | Per-dielectric materials (hybrid) in the stack record and fab data                                                      | Missing                                                                            | 1–2 d              | Single-material approximation in the engine (RF copper lives in the macro anyway)                                               |
| G-c | Side policy                                                                                                             | `claude/gap-sides`: single/double; single is the default                           | integration        | Single-sided                                                                                                                    |
| G-d | Routed length matching (LVDS)                                                                                           | `claude/gap-lenmatch` eea5686: KiCad-exact lengths, pairs and groups               | integration        | Relax group skew; LVDS is development-only                                                                                      |
| G-e | Router speed and dense completion                                                                                       | `claude/gap-speed` e91d8c8                                                         | integration        | —                                                                                                                               |
| G-f | `region` and `align`                                                                                                    | `claude/gap-constraints`                                                           | integration        | —                                                                                                                               |
| E2  | Multilayer RF macro import as a rigid block (L1 polygons with arcs, L2/L3 windows, fence vias, F.Mask openings, pocket) | Missing (P2 in [T58 §6.7])                                                         | 3–5 d              | Macro as a KiCad footprint with net-tie pads and rule areas (yapnr.rf already exports net-tie footprints and keepouts [RRF §3]) |
| E4  | ABL0161 fanout generator                                                                                                | Missing                                                                            | 3–5 d              | The deterministic pattern without MC variants                                                                                   |
| E5  | Per-layer and via keepouts for the RF region                                                                            | Missing (P4)                                                                       | 1–2 d              | KiCad rule areas emitted with the macro                                                                                         |
| E6  | RF audit (§7.5 R1–R6)                                                                                                   | Missing (P5)                                                                       | 2–3 d              | Standalone script                                                                                                               |
| E9  | Headless board render (`kicad-cli pcb render`, top and isometric)                                                       | New; small                                                                         | 0.5 d              | yapnr viewer3d export                                                                                                           |
| E10 | Order card fields for hybrid material callouts                                                                          | `claude/fab-order` has the card                                                    | 0.5 d              | Fab notes PDF in the bundle                                                                                                     |
| —   | Blind/buried/micro vias                                                                                                 | `claude/gap-vias` (design only)                                                    | **not needed**     | —                                                                                                                               |

### 7.5 Acceptance gates for a candidate board (mechanical, seed-controlled; PNR-04/06)

| Gate                  | Pass                                                                                                                                                                 |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DRC                   | KiCad 10.0.6 DRC under `pcbway-adv-6l-rf`: 0 errors, 0 unconnected, 0 unwaived warnings                                                                              |
| yapnr gate            | Legal placement, 100 % routed, contracts pass (`@pnr-current`, SI, noise screening, decoupling-loop audit)                                                           |
| R1                    | Macro copper sha256 equals the RF result                                                                                                                             |
| R2                    | RF region: no foreign copper, vias or parts on L1–L3; pocket parts only                                                                                              |
| R3                    | No L2 void within 1 mm of RF copper other than the macro's own windows; L3 solid under the windows                                                                   |
| R4                    | No digital net within 5 mm of RF copper on L1–L3; switch nodes ≥ 8 mm from Y1 and ≥ 5 mm from the RF region                                                          |
| R5                    | Radome visibility rule (§5.2)                                                                                                                                        |
| R6                    | Exported phase centres equal ANT-01 to 1 µm (geometry)                                                                                                               |
| Selection             | Among passing candidates: recurring cost, then RF-adjacent loss proxies, then area, then runtime. Report failures with the winner; fixed seed set; hashes per PNR-06 |
| Final copper sign-off | C2 openEMS run on the exported board's RF region plus finite board (PNR-03)                                                                                          |

---

## 8. Panel contents (one PCBWay panel design, ordered ×10)

Panel: **Board A (60 × 46 mm) plus a coupon strip (60 × 25 mm)**, joined by mouse-bite tabs, with
assembly rails as PCBWay asks. Panel outline about 66 × 82 mm [E].

| Coupon group                | Contents                                                                                                                                                                                                                                            | Measured by                                                                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| CP-60 (probe-ready, 60 GHz) | GSG pads (**250 µm pitch**, for 67 GHz probes; review: 150 µm leaves a 50 µm S–G gap, below the 0.10 mm rule [D]; GGB 67A is offered from 25 to 1250 µm [RRF §4.3]); GCPW thru; mTRL lines (+0.25, 0.5, 1.0, 2.0, 4.0 mm); open; short; load option | Booked metrology (D8), G2                                                                                                                        |
| CP-L                        | Launch half-replicas (ball land + RFS-1 to P0, with the land probed as a GSG site), conventional                                                                                                                                                    | G2                                                                                                                                               |
| CP-D                        | Divider back-to-back: conventional and **OPT-D**                                                                                                                                                                                                    | G2 (DEMO-01 primitive)                                                                                                                           |
| CP-T                        | TX feed replica: conventional RFS-3 and **OPT-T** if ready                                                                                                                                                                                          | G2                                                                                                                                               |
| CP-A                        | 1-port columns: RFS-4 corporate, RFS-4S series, single patches; **TI ODS and LEVM patch replicas** re-scaled to this stackup; a patch-length ladder ±25 µm                                                                                          | G2, solver regressions                                                                                                                           |
| CP-S (optional, RF-05)      | L1→L3 stripline transition with L4 grounded locally                                                                                                                                                                                                 | Toolkit benchmark                                                                                                                                |
| CP-6 (DC–6 GHz, LibreVNA)   | SMA edge-launch mTRL lines on the RF layer, a ring resonator (about 5 GHz), a TDR line, 4-wire copper-resistance bars on L1 and L3                                                                                                                  | The owner's LibreVNA. Results labelled "measured coupon (≤ 6 GHz)" only, feeding Dk, thickness and copper into the `order0` extraction loop [O0] |
| CP-M                        | Line/gap combs 75–150 µm, registration targets, microsection coupon                                                                                                                                                                                 | Optical CD measurement, vendor microsection                                                                                                      |

**Quantities.**

- 10 panels fabricated.
- Board A assembled on 6 of them (6 consigned radios + 1 spare radio).
- 4 bare boards kept for coupons, X-ray reference and rework.
- Rev A0 at JLC is a separate order: 5 boards, assembled turnkey, no coupons (FR-4 coupons tell us
  nothing about RF-01).

---

## 9. Fabrication, assembly and ordering

### 9.1 Staging with yapnr (agents dry-run only; the owner uploads and pays)

1. `yapnr fab build --vendor pcbway --profile pcbway-adv-6l-rf --stackup pcbway-6l-ro4835-ro4450f
--allow-draft <board>` writes the bundle:
   - Gerbers, drill, IPC-356, BOM with MPN and LCSC numbers, CPL;
   - the stackup drawing and fab notes: RO4835 LoPro core, RO4450F, ImAg, LDI, no mask over RF,
     width tolerance < ±10 %, CD report, X-ray on U1;
   - an assembly drawing and a sha256 manifest.
2. `yapnr fab preview` renders an SVG check of the zip.
3. `yapnr order stage --vendor pcbway --bundle … --dry-run` prints the order card. It never uploads,
   orders or pays [YAP order].
4. **Owner:** requests PCBWay's manual Rogers quote (the online price is blanked for Rogers [RF §0]),
   reviews the CAM stackup reply, approves, and ships the consigned radios (and the PMIC, if PCBWay
   cannot source it).
5. Rev A0: `yapnr fab build --vendor jlcpcb --profile jlc-6l --stackup jlc06161h-3313`, then
   `yapnr order stage --vendor jlcpcb --dry-run`. The owner uploads to JLC, selects JLC06161H-3313,
   and adds the BOM and CPL with the radios reserved in D2.

### 9.2 Cost and lead time

| Item                                                   | Cost                                                                       | Lead time                                                             | Basis                                                       |
| ------------------------------------------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------- |
| Rev A PCB, 10 hybrid panels                            | $600–1,200 [E]                                                             | 2–3 weeks [E]; not published                                          | Manual quote; scaled from [RF §5]'s $500–1,000 for 5 boards |
| Rev A assembly, 6 boards (BGA, X-ray, stencil)         | $250–450 [E]                                                               | about 1 week [E]                                                      | [RF §5]                                                     |
| Rev A parts: 7 radios (LCSC $21.22) + PMICs + rest     | about $149 + about $100 [E] (review: D2 buys about 20 radios, about $425)  | Now (24 in stock)                                                     | [JLC]                                                       |
| Shipping and duties                                    | $50–100 [E]                                                                | —                                                                     | —                                                           |
| **Rev A total**                                        | **≈ $1.1–1.9k [E]**                                                        | Order about Nov 6, boards about Nov 25–Dec 5                          |                                                             |
| Rev A0 (JLC 6L, 5 boards, turnkey PCBA incl. 5 radios) | **≈ $250–350 [E]** (radios $108, PCB $20–60, assembly and feeders $80–150) | 6-layer from 48 h + PCBA ≥ 4 d [RF §5]; about 1.5 weeks with shipping | [JLC], [T58 §2.4]                                           |
| Bench kit (D3)                                         | $910.69                                                                    | TI stock varies                                                       | [RR §1]                                                     |
| Test range (D9)                                        | $300–600 [E]                                                               | —                                                                     | —                                                           |
| GCP (radar cap)                                        | ≤ $50; plan ≤ $35 [D]                                                      | —                                                                     | §6.4                                                        |
| 60 GHz metrology (D8)                                  | Unknown (quote only)                                                       | Booking                                                               | [RRF §4.3]                                                  |

---

## 10. Firmware and bring-up

### 10.1 Software base

- MMWAVE-SDK 03.06.02.00-LTS (xwr68xx) and mmWave Studio with the DCA1000EVM [RR §7].
- Firmware lives outside yapnr (D10).
- Board configuration:
  - pinmux: CAN on H14/F14, UART N4/N5, I2C G14/F13;
  - flash parameters;
  - at boot, the PMIC init over I2C at address 0x60: `PLL_CTRL` (0x2B) ← 0x01 forces the internal RC
    oscillator (PLL*MODE = 00), which spread spectrum requires; then set only bit 7 (EN_SPREAD_SPEC)
    of `PIN_FUNCTION` (0x2C) by read-modify-write. \_Review:* TI's AOPEVM value 0xD6 also rewrites the
    EN2/EN3/GPIO selections in bits 6:0, whose defaults are OTP-dependent [TI-PMIC Table 49]; copied
    blindly it could change how this board's enable pins behave. Read and log `BUCKx_DELAY`, the PGOOD
    configuration and `OTP_REV`;
  - _before `MMWave_open()`_, call `rlRfSetLdoBypassConfig` with RF-LDO bypass and PA-LDO disable.
    The SDK user guide confirms the API name and that the mmWave API does not call it for you [TI-SDK-UG].
    Every firmware image that can start the RF (including UniFlash test images and mmWave Studio
    scripts) must do this first, because the board feeds 1.0 V into VOUT_PA (§14.4 R6).

### 10.2 Profile (requirements §4.1) and modes (ARCH-02)

- **Chirp:** start 60.3 GHz, slope 50 MHz/µs, ramp 70 µs, ADC start 6 µs, 512 complex samples at
  8 Msps, idle 10 µs (80 µs period).
  - IF margin: 7.2 MHz usable at 0.9·Fs against a 6.67 MHz beat at 20 m (7.4 %, measured at G1)
    [RR §7].
  - The radar cube is 480 KiB of the 768 KB L3 (63 %).
- **Perception mode:** 20 cycles of TX1/TX2/TX3 (TDM, 60 chirps, 4.80 ms burst), one frame every
  50 ms. Motion compensation between TX slots is added if the SDK's AoA chain lacks it.
- **Phased-TX mode:** three blocks of 20 chirps with all three TX on.
  - It needs the LDO-bypass/PA-LDO-disable configuration through mmWaveLink:
    `rlRfSetLdoBypassConfig` / `rlRfLdoBypassCfg_t` (name confirmed in review [TI-SDK-UG]; field
    meanings still to be read in the DFP doxygen at G0).
  - Per-chirp phase uses `rlRfSetPhaseShiftConfig`. At 5.625° per LSB, ±30° needs 87.2° per element
    (15 or 16 LSB) [RR §7].
  - Custom firmware is required: the demo has tested only phase "0".
- **Output (SYS-05):** UART and CAN-FD frames per detection: timestamp, mode, range, radial speed,
  azimuth, SNR/confidence, diagnostics. No elevation (ANT-03).

### 10.3 Regulatory envelope (REG-01/02; 47 CFR 15.255(c)(2)(iii)(A) checked [CFR])

- **Rule text:** peak EIRP ≤ 14 dBm, and the continuous off-times of ≥ 2 ms must sum to ≥ 25.5 ms
  in any 33 ms. The design targets 12 dBm.
- **Off-time [D]:** the worst window has more than 26.2 ms off, a margin of only **0.70 ms**. That
  allows any added emission of at most 0.7 ms (a 5.5 ms burst maximum), and only if it falls inside
  the burst's window.
  - The firmware computes the rolling 33 ms TX-on timeline from the actual schedule, _including
    boot RF calibrations and monitors_, and refuses any configuration that violates the envelope. A
    watchdog kills TX on a timing fault.
  - _Review (corrected):_ the original text scheduled calibrations and monitors "≥ 33 ms away from
    bursts". That is impossible at a 50 ms frame: the inter-burst gap is 45.2 ms, and isolating an
    emission from both neighbouring bursts needs ≥ 66 ms [D] (`review_calc.out` §6). The only two legal
    placements are: (a) contiguous with the burst, total ≤ 5.5 ms (≤ 0.70 ms extra); or (b) in a
    calibration frame that **replaces** one radar burst (≤ 5.5 ms), for example once per second. To
    keep RAD-09's 20 radar frames/s, (b) needs a 47.6 ms frame period (21 slots/s); the gap is then
    42.8 ms, still > 33 ms, so the off-time arithmetic is unchanged [D].
- **Power settings [D].** With the nominal 7 dBi column: TDM ≤ +5 dBm per port; 3-TX coherent
  ≤ −4.5 dBm per port (REG-02's 9.54 dB).
  - Until gain is measured, use a **10 dBi** bound (review; was 9 dBi): **TDM ≤ +2 dBm, coherent
    ≤ −7.5 dBm**. A 2-patch E-plane column at 0.6 λ0 spacing has about 9.4–10.2 dBi of directivity [D]
    (`review_calc.out` §7), so 9 dBi was not an upper bound. That is 10–19.5 dB of backoff from the 12 dBm
    maximum, inside the 26 dB backoff range.
  - Startup and calibration states obey the same caps. _Review:_ whether the boot-time TX power
    calibration in `rlRfInit` radiates at the profile's backoff or at a fixed level is not documented
    in the sources read. Measure the ISK's boot emission at B0 (B5 method). If it can exceed the cap,
    run boot calibration once into absorber and restore it from stored calibration data afterwards
    (O14).
- **Frequency containment (15.255(f)):** the band is 57–64 GHz; the chirp tops out at 63.8 GHz.
  Inter-chirp power saving stays on (ANA#22A) so the PA is off during synthesizer overshoot. Test
  across −20…+50 °C and 85–115 % supply, i.e. 4.25–5.75 V. The eFuse cuts off above about 5.5 V (review; was 5.6 V),
  which simply stops emission.
- **Development units** operate in a controlled area at the 12 dBm design EIRP and are not
  marketed. _Review:_ 47 CFR 2.805(d)(2)(ii) permits operation before authorization for "evaluation of
  performance and determination of customer acceptability, during developmental, design, or
  pre-production states", provided the devices comply with the existing rules, are not marketed, and
  are rendered inoperable or retrieved at the end [CFR-2.805]. Rev A0/A therefore have to meet the
  15.255 envelope themselves; the G1 memo confirms the remaining conditions (O8).

### 10.4 Bring-up sequence

| Step                    | Where                         | Checks                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B0 (G0)                 | ISK + mmWave Studio + DCA1000 | Complex ADC capture; LDO bypass with 3 TX together; phase shifter codes; TX/RX monitors on the Q (non-functional-safety) part. Table 9-1 of the data sheet lists monitors "for Functional Safety-Compliant devices" (checked), so verify. Firmware fields. _Review:_ also the LDO-bypass call order (§10.1) and whether boot calibration radiates above the cap (O14)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| B1 (G1, reference)      | ISK                           | §4.1 profile: IF margin, memory fit, processing time, 3-TX rail behaviour on the ISK's LP87524J                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| B2                      | A0, then A: power             | Current-limited 5 V supply; rails, PGOOD→NRESET; PMIC register dump; eFuse OVLO and ILIM trip                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| B3                      | Boot                          | UART ROM flashing (SOP 101), QSPI boot at 80 MHz (both flash parts), JTAG, CAN-FD loopback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| B4 (ARCH-03)            | Worst TX state                | 3 TX at the coherent cap and at full power (with absorber in front): shunt waveforms, PMIC readout, ripple at VOUT_PA, PMIC spur with spread spectrum on and off, absorber A/B                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| B5 (REG-01)             | Timeline                      | FRAME_START GPIO on a scope, plus a **second unit in RX-only mode as a 60 GHz sniffer** that time-stamps bursts [D: no extra equipment]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| B6 (60 GHz radar-level) | Range with corner reflectors  | Two-way patterns per virtual pair on the rotation stage; −30/0/+30° steering; zero-range leakage; TX on/off noise floor (RF-07); detections at 1–20 m; **sub-band chirps** (for example 8 × 437 MHz across 60.3–63.8 GHz) to read relative two-way gain against frequency, an indirect resonance-centre estimate [D]; A/B against the ISK in the same setup (±1 dB relative gain, estimate [RRF §4.2]). _Review:_ the 48.6 mm (1 m²) trihedral is in its own far field only beyond about 2.0 m, the 27.3 mm one beyond about 0.6 m [D], so 1 m RAD-01 points need the small reflector or a stated RCS correction. Narrow sub-band chirps cross the synthesizer spur frequencies of errata ANA#14 (60.3, 60.75 … 63.45 GHz); the spurs land in non-zero Doppler bins, so use a static reflector. With D12, the same test compares the three frequency variants |
| B7                      | Lot metrology without a VNA   | Optical patch and line dimensions from a calibrated microscope (stage micrometer, about $50–100 [E]); LibreVNA CP-6 coupons                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| B8 (G2)                 | Booked lab                    | CP-60/L/D/T/A coupons                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

**Calibration.** RX gain and phase come from a boresight corner reflector. TX phase per element
comes from TDM virtual channels, and the ±30° codes are then quantized to 5.625° steps. TX3 runs a
background temperature recalibration (errata ANA#13B).

**Test equipment needed beyond the LibreVNA.** An oscilloscope with FFT and a current or
differential probe, if the owner has none (O10).

---

## 11. Risks and stop conditions by gate

| Gate    | Risk                                                                                     | Early signal            | Stop / fallback                                                                                                                                                                                                                |
| ------- | ---------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| G0      | LDO bypass, the per-chirp phase API or the monitors are unavailable on IWR6843**AQ**GABL | B0 on the ISK; DFP docs | If 3-TX coherent mode is not reachable on any external-antenna IWR6843: **stop** (the requirements' G0 rule). If only monitors are missing: switch to IWR6843ABGABL(R) (SIL-2, same package, $25.57 at 1k; COST-01 still fine) |
| G0      | Radio supply: 24 units at LCSC; Digi-Key 26 weeks                                        | D2                      | If fewer than 10 can be secured: build A0 only and wait for the reel lead time; brokers only as a last resort                                                                                                                  |
| G0      | MX25V1635F has no stock                                                                  | —                       | MX25R1635F, checked at B3                                                                                                                                                                                                      |
| G1      | PMIC spur at 12 m above the noise floor even with spread spectrum                        | B4                      | PMIC sync (CLKIN option); an LDO on 1V8_BB (TI's LDO scheme shows no spur [RR §3]); profile slope change as the last resort                                                                                                    |
| G1      | IF margin of 7.4 % too thin                                                              | B1                      | Reduce slope and recompute the profile (requirements §4.1 rule)                                                                                                                                                                |
| G1      | Regulatory: boot calibrations break the 0.7 ms margin                                    | B5                      | Reschedule calibrations; shorter burst. **Stop** if the envelope cannot be met (G1 rule)                                                                                                                                       |
| G1      | 14.4/28.8 GHz spurs over 15.209                                                          | Pre-scan (lab)          | Absorber; Rev B half-can                                                                                                                                                                                                       |
| — (fab) | PCBWay cannot laminate RO4835 + RO4450F, or will not meet ±10 % width                    | Quote reply (D7)        | Fallback stack (§4.1); Eurocircuits or Sierra special quote [RF §1]; RO3003 5 mil variant                                                                                                                                      |
| — (fab) | 6 layers do not close the BGA escape                                                     | First PnR runs          | 8-layer variant (TI's count)                                                                                                                                                                                                   |
| — (PnR) | Engine gaps E2/E4 slip                                                                   | P2                      | Fallbacks in §7.4. **No hand routing**: the date slips instead                                                                                                                                                                 |
| G2      | No metrology booked                                                                      | D8                      | G2 cannot pass. Rev A radar-level results are still reported, labelled "measured system"                                                                                                                                       |
| G2      | Coupons disagree with the models (> 0.5 dB, > 5°, > 1 %)                                 | B8                      | Update the material/process model before any optimization claim (G2 rule); Rev B regenerates                                                                                                                                   |
| G2/G3   | Conventional column misses RL at the band edges (**preregistered**)                      | B6 sub-band data, G2    | Not a stop. It is the DEMO-01 OPT-C target, and option B already widens it                                                                                                                                                     |
| G3      | TX–RX leakage saturates the RX (ANA#15: −10 dBm) or desense > 3 dB                       | B6                      | Wider bank spacing or a taller fence in Rev B; lower TX power. **Stop** if the geometry cannot support the promised angle performance (G3 rule)                                                                                |
| G3      | Pointing error > 5° after calibration                                                    | B6                      | Check the phase quantization and calibration; if the cause is embedded-pattern asymmetry, Rev B macro                                                                                                                          |

---

## 12. Implementation plan

The phases overlap. Effort is in agent-days [E]; owner actions are marked **Owner**. _Review:_ dates
revised (original in brackets), a firmware phase added (P1f, missing before), the column-topology
decision moved to Oct 9, a floorplan render added, and the Rev A order gated on A0 (D13).

| Phase                             | Dates [E]                                                                                                                        | Work                                                                                                                                                                                                                                                                                    | Depends on                        | Effort         |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- | -------------- |
| P0 G0 desk + purchases            | Oct 3–8                                                                                                                          | **Owner:** D0–D3, D9, D12, D13; buy radios (D2); order the ISK kit. Me: freeze ARCH-01 data; DFP API check; regulatory memo draft; part-cache entries (ABL0161 footprint checked against LEVM IPC-356) and others; fab data files (§4.2)                                                | —                                 | 3              |
| P1 atopile source                 | Oct 6–13                                                                                                                         | §3.4 modules, contracts and annotations (review fixes: 1.0 V split + 2 mΩ shunt, OVLO 5.5 V, VIOIN load-switch option, radome lands); adversarial source review; ERC. **Floorplan render ≈ Oct 13** (placement only, labelled "not routed")                                             | P0 parts                          | 4              |
| P1f firmware (separate repo, D10) | Oct 13–Nov 14                                                                                                                    | ISK B0/B1 once the kit arrives; board support (pinmux, PMIC init by read-modify-write, LDO bypass before `MMWave_open`); rolling 33 ms envelope enforcer and TX watchdog (required before any A0 radiates); TDM-MIMO chain with motion compensation; phased-TX mode; UART/CAN-FD output | ISK kit                           | 12–20          |
| P2a RF conventional               | Oct 6–22 [Oct 6–20]                                                                                                              | **Column and TX-feed geometry drawn + one openEMS solve, D5 decided ≈ Oct 9** [Oct 17]; generators RFS-1…6 (KiCad macro export with fences, windows and mask); openEMS models; C0 (incl. the ISK RO4835 patch regression) and C1 on GCP; coupons generator (CP-\*), D12 variants        | GCP image with openEMS (O9)       | 8–10           |
| P2b engine                        | Oct 6–22 [Oct 6–20]                                                                                                              | Integrate gap branches (G-a…f); E2, E4, E5, E6, E9, E10                                                                                                                                                                                                                                 | Gap-fix integration               | 10–14          |
| P2c RF optimized                  | Oct 8–Nov 3                                                                                                                      | OPT-D at 60 GHz (grid, sub-pixel); OPT-T/OPT-C only if the engine features land                                                                                                                                                                                                         | P2a C0 regressions                | 6–10 + compute |
| P3 PnR                            | Oct 20–28 [Oct 20–27]                                                                                                            | Rev A0 and Rev A Monte Carlo runs on GCP; **first routed render ≈ Oct 24–28** (KiCad 3D, top + isometric), labelled with its gate status                                                                                                                                                | P1, P2a macro v1, P2b             | 3–5            |
| P4 closure + package              | Oct 27–Nov 10 [Nov 6]                                                                                                            | DRC/RF audit, C2 sign-off on exported copper (about half a day of wall clock per excitation), adversarial routed-board review, fab bundles. **A0 orderable ≈ Oct 30** [Oct 28]; **Rev A package ≈ Nov 10** [Nov 6]; Board B gate decision (§6.5)                                        | P3                                | 5–7            |
| P5 fab                            | A0: Oct 30–Nov 11; A: quote request Nov 10, order ≈ Nov 14 after A0 B2/B3, boards ≈ mid-Dec (P50), early Jan (P90) [Nov 6–Dec 5] | **Owner:** order A0; request the PCBWay quote; approve after A0 B2/B3 (D13); have the consigned radios shipped to PCBWay                                                                                                                                                                | D7, D13                           | —              |
| P6 bring-up                       | From A0 arrival (≈ Nov 11)                                                                                                       | §10.4 B2–B7 on A0, then Rev A; G1 report                                                                                                                                                                                                                                                | ISK B0/B1 done, P1f board support | 10–15          |
| P7 G2/G3                          | After D8 booking                                                                                                                 | Coupon measurements; model update; array and receiver proof                                                                                                                                                                                                                             | D8                                | —              |
| P8 Rev B                          | After G2                                                                                                                         | DEMO-01 pair (Board A' + Board B), radome, cost-down, frequency correction from D12                                                                                                                                                                                                     | G2                                | —              |

**Critical path to the orderable Rev A package:** P2b (macro import and fanout generator) and P2a
(launch and column sign-off), then P3, then P4. _Review:_ the realistic chance of a Nov 10 package is
moderate, not high [E]: six gap branches are not yet integrated, five engine features (E2, E4–E6, E9)
are unwritten, the openEMS image does not exist yet (O9), and the column topology is still open. Each
slips the package one-for-one; none may be absorbed by hand routing.

**First render:** at the first complete placement-and-route of either build (P3), shown with its
DRC and RF-audit status. **Orderable package:** at the end of P4, once every §7.5 gate passes.

---

## 13. Open items and unverified points

| ID  | Item                                                                                                                                                                                                                                                                                                                                                                               |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| O1  | PCBWay: RO4835 LoPro core availability at 4 mil (LoPro or ED foil; material in stock or to be bought, which sets lead time); the Rogers bondply (PCBWay lists RO4450F for pure-Rogers multilayers and "ROGERS4403 series"/Shengyi prepreg for Rogers + FR-4 [PW-cap]); the hybrid build and its pressed bondply thickness; an absolute L1 etch tolerance; a CD report; X-ray of U1 |
| O2  | PCBWay: 0.15 mm drill with a 3–4 mil ring at 1.2 mm thickness (7.8:1; ≤ 8 is "normal" on the capabilities page [PW-cap]) on the Rogers hybrid, for interstitial GND vias and fences; minimum hole-to-hole spacing for 0.40 mm fence pitch                                                                                                                                          |
| O3  | CAN-FD on H14/F14 (modes 9/8) is untested in TI's examples; bench-check it at B3. The E14/D13 option stays as a DNP footprint                                                                                                                                                                                                                                                      |
| O4  | Whether 6 layers is enough for the escape (first PnR)                                                                                                                                                                                                                                                                                                                              |
| O5  | LP87524J OTP start-up delays and PGOOD sources (register dump on A0)                                                                                                                                                                                                                                                                                                               |
| O6  | The `rlRfLdoBypassCfg_t` and `rlRfSetPhaseShiftConfig` field definitions in the DFP doxygen (the LDO-bypass API name and its call-before-`MMWave_open` rule are confirmed [TI-SDK-UG]); TX/RX monitor availability on the Q part                                                                                                                                                   |
| O7  | The LP-XDS110 "XDS110 out" connector pinout for the J3 footprint                                                                                                                                                                                                                                                                                                                   |
| O8  | Regulatory memo: the development-unit exemption, REG-03 product boundary, 15.255(f) test conditions                                                                                                                                                                                                                                                                                |
| O9  | openEMS in the GCP container (Ubuntu package or source build); measured throughput (C0)                                                                                                                                                                                                                                                                                            |
| O10 | The owner's oscilloscope and probes for B4                                                                                                                                                                                                                                                                                                                                         |
| O11 | Test-radome εr at 60 GHz (the 2.9 assumed is low-frequency)                                                                                                                                                                                                                                                                                                                        |
| O12 | The "useful coverage ±45°" working definition (G_bs − 6 dB) needs owner confirmation                                                                                                                                                                                                                                                                                               |
| O13 | _Review._ Why ISK Rev D delays the radio's 3.3 V with a TPS22917 load switch; Rev A carries a DNP option until known                                                                                                                                                                                                                                                               |
| O14 | _Review._ Whether boot-time and periodic TX calibrations or monitors radiate above the per-port cap (measure on the ISK at B0)                                                                                                                                                                                                                                                     |
| O15 | _Review._ Minimum VOUT_PA during a 3-TX burst against 0.95 V (B4), and the 1.0 V sense-point choice                                                                                                                                                                                                                                                                                |
| O16 | _Review._ Enclosed thermal resistance of the Rev A board with the test radome (hot box at 50 °C, on-die sensors)                                                                                                                                                                                                                                                                   |
| O17 | _Review._ PCBWay's charge for the two D12 frequency-variant panel designs                                                                                                                                                                                                                                                                                                          |
| O18 | _Review._ JST GH is rated 1 A per contact; the 2 A input allowance and the about 1.3 A burst peak suggest an 8-pin GH (SM08B-GHS-TB) with two 5 V and two GND contacts. Check the rating in JST's GH data sheet before P1 closes                                                                                                                                                   |

---

## 14. Review (2026-10-03)

Adversarial review from the standpoint of a senior mm-wave radar and PCB engineer. I re-read the
primary sources listed in §14.7 (TI, Rogers, eCFR and LCSC files fetched on 2026-10-03), redid the
arithmetic in `board-calc/review_calc.py` (output in `review_calc.out`; every number from it is
[D]), and edited the document in place. Every in-place change is marked _Review_. Nothing was
ordered, quoted or requested from anyone.

### 14.1 Verdict

- **The architecture holds:** the radio, PMIC, power tree, hybrid stackup class, PCBWay route,
  coupon strategy and the honesty about 60 GHz metrology are right.
- **It is not yet a buildable layout.**
  - Three RF geometry questions are open and drive the floorplan: the corporate column inside a
    2.342 mm pitch, the TX equal-delay meanders, and the composite dielectric under the L2 windows.
  - Two regulatory statements were wrong: the calibration scheduling and the "9 dBi bound".
  - The 1.0 V RF/PA rail fails its ±50 mV window at the 2.5 A peak in the worst case.
  - The schedule had Rev A0 arriving after Rev A was ordered, so A0 could not protect Rev A.
- **D0 still stands:** order Rev A before G2, with the changes below.

### 14.2 Facts checked against primary sources

| Claim in the plan                                                                                                                                                             | Source re-read                                                                                                                                                                                                                                                                   | Result                                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| IWR6843AQGABLR is an orderable ABL (FCBGA-161, 10.4 × 10.4 mm, 0.65 mm) part                                                                                                  | SWRS219F p. 1 and orderable table                                                                                                                                                                                                                                                | Confirmed                                                                            |
| TX1–3 = B4/B6/B8; RX1–4 = M2/K2/H2/F2                                                                                                                                         | SWRS219F §6.2                                                                                                                                                                                                                                                                    | Confirmed                                                                            |
| 3 TX only in 1-V LDO bypass + PA-LDO disable, 1 V fed on VOUT_PA (A2/B2), 2500 mA peak                                                                                        | SWRS219F Table 5-1 note 1, §6.2, Table 7-3 note 2                                                                                                                                                                                                                                | Confirmed                                                                            |
| Bypass rails 0.95–1.05 V, 1.4 V absolute maximum; VIN_13RF1 G5/H5/J5, VIN_13RF2 C2/D2                                                                                         | SWRS219F §7.1, §7.4, §6.2                                                                                                                                                                                                                                                        | Confirmed                                                                            |
| Ripple limits on the 1.0 V rail                                                                                                                                               | SWRS219F Table 7-2                                                                                                                                                                                                                                                               | Confirmed; the 275 kHz (5 µV) and 550 kHz (3 µV) rows were missing and are now added |
| All rails stable before NRESET release; 8 ms wake-up with a crystal                                                                                                           | SWRS219F §7.12.1, Fig. 7-3                                                                                                                                                                                                                                                       | Confirmed                                                                            |
| RθJA 22.3 °C/W (JEDEC 2S2P); TJ −40…105 °C                                                                                                                                    | SWRS219F §7.11, §7.1                                                                                                                                                                                                                                                             | Confirmed; see R7 for what it means in an enclosure                                  |
| TX 12 dBm, 26 dB backoff; RX out-of-band P1dB −12 dBm; IF 10 MHz; complex-1x 12.5 Msps                                                                                        | SWRS219F §7.9                                                                                                                                                                                                                                                                    | Confirmed                                                                            |
| CAN_FD_TX H14 mode 9, CAN_FD_RX F14 mode 8; I2C G14/F13 mode 2; SOP2/1/0 = P9/G13/N13                                                                                         | SWRS219F §6.3, Table 6-1                                                                                                                                                                                                                                                         | Confirmed                                                                            |
| LP87524J OTP: Buck2 1 V/4 A "RF, with ferrite filter"; LP87524B Buck2/3 = 1.8/2.3 V                                                                                           | SNVSAW2B §7.3 tables                                                                                                                                                                                                                                                             | Confirmed                                                                            |
| Spread spectrum needs PLL_MODE = 00; registers 0x2B/0x2C                                                                                                                      | SNVSAW2B §7.3.1.4, Tables 48–49                                                                                                                                                                                                                                                  | Confirmed; 0xD6 also rewrites EN/GPIO bits, so it is now a read-modify-write         |
| LP87524 VIN limits                                                                                                                                                            | SNVSAW2B §6.1/§6.3: recommended 2.8–5.5 V, absolute maximum 6 V                                                                                                                                                                                                                  | OVLO lowered from 5.6 V to 5.5 V                                                     |
| Buck accuracy                                                                                                                                                                 | SNVSAW2B §6.5: ±2 % DC (PWM, ≥ 1 V); ±3 % for a 0→2 A step                                                                                                                                                                                                                       | Used in the new 1.0 V budget                                                         |
| Errata ANA#13B, #14, #15 (−10 dBm), #19 (47 nF), #21, #22A                                                                                                                    | SWRZ087D                                                                                                                                                                                                                                                                         | Confirmed; ANA#14 now noted for the sub-band chirps                                  |
| 50 Ω point about 1.3 mm from the ball; ENIG "not a good choice"; immersion silver; thin top copper; no mask over RF; radome distance n·λ0/2                                   | SPRACG5 §2.1–2.3, §5                                                                                                                                                                                                                                                             | Confirmed                                                                            |
| 6 layers suffice because there is "no LVDS"                                                                                                                                   | SWRR164 "Revision history of Board changes": ISK Rev C went to 8 layers "mainly due to" the DCA1000 60-pin connector and its LVDS; "Rev B is still a valid reference for 6-layer board design"                                                                                   | **Contradicted** (Rev A has the LVDS header); O4 raised                              |
| ISK Rev D 1.0 V rail                                                                                                                                                          | SWRR164 Rev D schematic (read from the PDF text layer, so topology is inferred from net names): MPZ2012S101A on the RADAR_1P0_RF1 and RF2 branches, 0.002 Ω sense resistors on the 1V0 (and other) rails, TPS22917 "Load switch for delaying 3.3V"                               | New facts, used in §3.3; confirm on the drawing                                      |
| RO4835: process Dk 3.48 ± 0.05 (4 mil: 3.33 ± 0.05), design Dk 3.66 (8–40 GHz), Df 0.0037, 4 mil ± 0.7 mil, ED foil standard                                                  | Rogers 92-160, rev. 1686 092425                                                                                                                                                                                                                                                  | Confirmed                                                                            |
| 15.255(c)(2)(iii)(A) 14 dBm peak EIRP, ≥ 25.5 ms off (intervals ≥ 2 ms) per 33 ms; (d)(2) 15.209 below 40 GHz; (f) −20…+50 °C, 85–115 %; (h) no external phase-locking inputs | eCFR 47 CFR 15.255 as of 2026-09-30                                                                                                                                                                                                                                              | Confirmed                                                                            |
| Development-unit operation                                                                                                                                                    | eCFR 47 CFR 2.805(d)(2)(ii)                                                                                                                                                                                                                                                      | Confirmed and now quoted (§10.3)                                                     |
| PCBWay materials and rules                                                                                                                                                    | PCBWay capabilities and advanced-capabilities pages: RO4835 listed; "Rogers4 series + FR-4 mixed (prepreg ShengYi and ROGERS4403 series)" normal; "Pure ROGERS4 series multi-layer (prepreg 4450F)" medium; 0.15 mm CNC minimum; thickness/diameter ≤ 8 normal; immersion silver | **Partly confirmed:** RO4450F inside an FR-4 hybrid is not a listed combination (O1) |
| Radio stock and price                                                                                                                                                         | LCSC C2866258 page: 24 in stock, $21.86 (1+) / $21.22 (10+)                                                                                                                                                                                                                      | Stock confirmed; LCSC price corrected ($21.66/$21.02 came from the JLC parts search) |
| MPZ2012S101AT000                                                                                                                                                              | LCSC C15957: 100 Ω at 100 MHz, 4 A, 20 mΩ, 129,690 in stock                                                                                                                                                                                                                      | Confirmed; the 20 mΩ drives R5                                                       |
| LDO-bypass API                                                                                                                                                                | MMWAVE SDK User Guide (3.5, 2020-09-30): the mmWave API does not expose `rlRfSetLdoBypassConfig`/`rlRfLdoBypassCfg_t`; call it before `MMWave_open()`                                                                                                                            | API name confirmed (O6 narrowed)                                                     |

Not re-checked in this review: TPS259474A variant table, TCAN1044A, the DCA1000 header part number
(QTH vs QSH), TI tool prices and Digi-Key lead times. These rest on `research-radio.md`.

### 14.3 Changes made in this revision

1. §0, §6.2: preregistered loss predictions now carry the model's ±30 % conductor-loss error and a
   0.3–0.8 dB through-via launch. RX 0.6–1.4 dB and TX 1.2–2.4 dB bands; the central values are unchanged.
2. §0, §12: schedule revised.
   - New: a floorplan render about Oct 13; A0 orderable about Oct 30; Rev A package about Nov 10.
   - The PCBWay order is approved after A0's B2/B3, about Nov 14 (new D13).
   - Rev A boards about mid-December (P50).
   - New firmware phase P1f; it was missing.
   - The D5 column decision moves from Oct 17 to Oct 9.
3. §0.1: D1, D2, D4, D5 and D6 revised; D12 (frequency bracketing) and D13 (A0 gate) added.
4. §2.2: the absorber on the lid is now a bench experiment only. Any absorber thicker than about 0.1 mm
   breaks the 30° rule next to the RX bank.
5. §3.3, §2.1: 1.0 V DC budget added; ISK-style branch ferrites with two in parallel on the PA branch;
   2 mΩ shunt; FB sensed at the buck output; DNP remote-sense and VIOIN load-switch options. OVLO is
   now 5.5 V.
6. §3.3, §3.4: ripple table completed (275 and 550 kHz); thermal paragraph for the enclosed case.
7. §4.1: window dielectric about 0.21–0.22 mm, not 0.2032 mm; absolute etch tolerance and LoPro foil
   to be confirmed.
8. §4.2, §6.2 (RFS-5): fence vias 0.15/0.30 mm at ≥ 0.40 mm pitch (≤ 0.36 mm is not manufacturable);
   interstitial-via rule checked against PCBWay's page.
9. §5.4: test radome over the north part only, so J2 stays reachable with the radome fitted.
10. §6.2: notes on RFS-3 and RFS-4 geometry, and the TX-path mask.
11. §6.3: OPT-D's primary claim is area. The ISK RO4835 patch becomes the first solver regression.
12. §6.4: C2 compute estimate corrected (about 90 VM-hours, still under the cap).
13. §7.2: the "no LVDS" claim is corrected using TI's own revision history; three escape rules added.
14. §8: CP-60 GSG pitch changed from 150 to 250 µm.
15. §10.1, §10.2: PMIC init by read-modify-write; LDO-bypass call order made mandatory.
16. §10.3: calibration placement corrected. The 10 dBi pre-measurement EIRP bound (TDM ≤ +2 dBm,
    coherent ≤ −7.5 dBm) replaces 9 dBi. Boot-calibration emission added (O14); 2.805(d)(2)(ii) quoted.
17. §10.4: B0 and B6 notes (boot emission, reflector far field, ANA#14).
18. §13: O1, O2 and O6 updated; O13–O18 added.

### 14.4 Findings that need work, not just edits

- **R1. The conventional antenna may be off-centre as well as narrowband.**
  - What it is: the centre-frequency uncertainty of a patch on the window is about ±1.1 GHz [D]
    (Dk ±0.10 → ±0.76 GHz, ±13 µm etch → ±0.58 GHz, thickness ±10 % → ±0.59 GHz). That is close to
    the patch's own ±1.3 GHz half-bandwidth.
  - What it means: with no 60 GHz metrology, Rev A can only see this indirectly (sub-band two-way
    gain, B6).
  - Actions:
    - bracket it with D12 (±1.8 % length variants on two assembled boards);
    - make the ISK RO4835 patch the first solver regression (§6.3), because it is the only TI 60 GHz
      antenna on this laminate.
- **R2. The corporate column does not obviously fit.**
  - With W = 1.6 mm at d = 2.342 mm the inter-column gap is 0.74 mm. The input line must run up that
    gap past the lower patch: 0.27 mm from each neighbour on 4 mil, 0.15 mm on 8 mil [D].
  - The λg/2 phase arm has to meander in the 1.7 mm between the facing radiating edges.
  - Expect coupling between columns, a phase-centre offset (mask ±50 µm) and pattern skew.
  - TI's LEVM uses series-fed 2-patch columns at 2.418 mm pitch, probably for this reason.
  - Draw it and solve it once by Oct 9 (D5). Keep W/L ≥ 1.2.
- **R3. Windowed TX feeds buy loss that is thrown away.**
  - They save about 0.8 dB (central) [D] against all-4-mil GCPW, but the EIRP cap removes it again through
    backoff.
  - The cost:
    - three ground-step transitions with no TI precedent;
    - R ≥ 1.5 mm meanders (TX1 needs +6.9 mm, about 6.0 × 5.2 mm of area) beside the RX4 feed and the
      VOUT_PA pocket;
    - more feed radiation into the TX pattern;
    - a larger L3 area forced to GND.
  - Recommendation: windows under the patches only, 4 mil GCPW feeds with fences (TX1 meander about
    2.4 × 4.8 mm [D]), and an RF-03 TX waiver to ≤ 2.7 dB (D4/D6).
  - The 10.9 mm equal-delay layout has not been drawn. Draw it before §5 freezes.
- **R4. The launch has no TI precedent on this laminate.** TI's RO4835 transition uses microvias in
  the GND pads; Rev A uses through vias at interstitial sites (LEVM-style, but on FR408HR). Keep the
  CP-L half-replicas, and treat the 0.3–0.8 dB launch range as the main term in the RF-03 bands.
- **R5. ARCH-03: the 1.0 V window.**
  - Budget [D]:
    - as planned (one ferrite and a 5 mΩ shunt, sensed at the buck): 0.92 V nominal, 0.88 V worst case;
    - with the fix in §3.3: 0.96 V nominal, 0.92 V worst case.
  - The worst case still fails on paper. B4 must measure the minimum at VOUT_PA during a 3-TX burst.
  - A sense point downstream of the ferrite removes the IR term, but puts the ferrite inside a control
    loop the PMIC data sheet does not characterize. Keep that as a DNP option, not the default.
- **R6. LDO-bypass order.**
  - The board feeds 1.0 V into VOUT_PA and VIN_13RF from power-up, as ISK Rev D appears to do.
  - Any firmware that starts the RF without first calling `rlRfSetLdoBypassConfig` runs the device
    outside its documented mode.
  - Make the call part of board support, and use only scripts and images that include it (O6).
- **R7. Thermal.**
  - In an enclosure at 50 °C ambient, TJ is about 89–102 °C at 1.3 W and up to 110 °C at 1.5 W [D/E],
    against TJ max 105 °C.
  - The absorber-on-lid idea makes this worse.
  - Add the via field and bottom copper (§3.3), a hot box (about $50 [E], development equipment) and
    on-die sensor logging for SYS-05.
- **R8. Regulatory.**
  - The "≥ 33 ms from bursts" rule was impossible and is now replaced (§10.3).
  - Boot-calibration emissions are unknown (O14).
  - The EIRP bound is now 10 dBi.
  - The 15.255(f) 115 % condition now means "no emission" above about 5.5 V, which the memo must
    justify.
- **R9. Escape on 6 layers with LVDS (O4).** TI needed 8 layers for exactly this connector. Keep the
  LVDS on outer layers next to J2, use ISK Rev B as the 6-layer reference, and accept 8 layers
  mechanically if PnR does not close.
- **R10. What Rev A can honestly show without 60 GHz metrology.**
  - Shows:
    - ARCH-03 (rail behaviour);
    - the REG-01 timeline, as engineering evidence only, not a compliance measurement;
    - two-way patterns and steering pointing;
    - leakage and desensitization;
    - detections.
  - Shows only roughly:
    - absolute realized gain, about ±2 dB [E], because it is referenced to TI's own approximate
      "~7 dBi" for the ISK;
    - the antenna resonance centre, through the sub-band gain test and D12.
  - Cannot show: return loss, feed loss at reference planes, material Dk/Df at 60 GHz, or absolute
    EIRP.
  - The §1.3 table is consistent with this.

### 14.5 Owner decisions (recommendation in bold)

| ID    | Decision                      | Recommendation                                                                          |
| ----- | ----------------------------- | --------------------------------------------------------------------------------------- |
| D0    | Order Rev A before G2         | **Yes**, as an engineering build that also carries the G2 coupons                       |
| D2    | Radios                        | **Buy about 20 of the 24 in commodity stock now** (about $425), not 12                  |
| D4/D6 | L2 windows                    | **Under the patches only**; 4 mil GCPW TX feeds; TX waiver to ≤ 2.7 dB                  |
| D5    | Column topology               | **Decide by Oct 9 on a drawn and solved geometry**; series fallback needs your sign-off |
| D8    | 60 GHz metrology booking      | **Start now**; G2 cannot pass without it                                                |
| D12   | Frequency-bracketing variants | **Yes**, unless PCBWay charges heavily per design (O17)                                 |
| D13   | Rev A order gated on A0 B2/B3 | **Yes**                                                                                 |

### 14.6 Top risks after review (ranked)

1. **Antenna off-centre and narrowband, invisible without 60 GHz metrology** (R1). Mitigations:
   D12, the ISK regression, sub-band B6, D8.
2. **Schedule.** Six unintegrated gap branches, five unwritten engine features, no openEMS image yet,
   and an open column topology. The Nov 10 package is a moderate-probability date [E]. Every slip
   moves the order one-for-one, because hand routing is not allowed.
3. **RF geometry does not fit the 60 × 46 mm floorplan** (R2, R3). Draw it by Oct 9.
4. **Radio supply.** 24 units are the whole commodity stock; TI is out of stock and Digi-Key quotes
   26 weeks. Mitigation: D2.
5. **PCBWay materials.** RO4450F in an FR-4 hybrid is not a listed combination, and RO4835 LoPro at
   4 mil may need to be bought in, adding lead time. Mitigations: O1, the §4.1 fallback stack, the
   Eurocircuits/Sierra special quote.
6. **ARCH-03 rail window** (R5) and **thermal in the enclosure** (R7).
7. **Regulatory envelope.** Boot and monitor emissions, and the 0.70 ms off-time margin (R8).

### 14.7 Sources added in review (read 2026-10-03)

- **[TI-ISK-SCH]** TI, xWR6843ISK Rev D schematic PROC073D(001_IWR), in SWRR164:
  <https://www.ti.com/lit/zip/SWRR164>
- **[TI-ISK-REV]** TI, "Revision history of Board changes" (ISK Rev A–D, MMWAVEICBOOST), in the same
  SWRR164 archive.
- **[TI-SDK-UG]** TI, _MMWAVE SDK User Guide_, Product Release 3.5, 2020-09-30, "mmWave Front End
  Calibrations":
  <https://software-dl.ti.com/ra-processors/esd/MMWAVE-SDK/latest/exports/mmwave_sdk_user_guide.pdf>
- **[TI-PMIC]** (as in the main list) SNVSAW2B §6.1, §6.3, §6.5, Tables 48–49.
- **[TI-DS]** (as in the main list) SWRS219F §6.2, §6.3 (Table 6-1), §7.1, §7.4, §7.6 (Table 7-2), §7.7 (Table 7-3), §7.9, §7.11, §7.12.1.
- **[TI-ERR]** (as in the main list) SWRZ087D: ANA#13B, ANA#14, ANA#15, ANA#19, ANA#21, ANA#22A.
- **[R-4835]** Rogers, _RO4835 Laminate Data Sheet_, Publication 92-160, revised 1686 092425:
  <https://www.rogerscorp.com/-/media/project/rogerscorp/documents/advanced-electronics-solutions/english/data-sheets/ro4835-laminate-data-sheet.pdf>
- **[CFR-2.805]** 47 CFR 2.805, eCFR as of 2026-09-30:
  <https://www.ecfr.gov/api/versioner/v1/full/2026-09-30/title-47.xml?part=2&section=2.805>
- **[PW-cap]** <https://www.pcbway.com/capabilities.html> and **[PW-adv]**
  <https://www.pcbway.com/advanced-pcb-capabilities.html>
- **[LCSC]** <https://www.lcsc.com/product-detail/C2866258.html> (IWR6843AQGABLR) and
  <https://www.lcsc.com/product-detail/C15957.html> (MPZ2012S101AT000)

---

## Sources

Fetched or read 2026-10-03 unless noted.

- **[REQ]** `requirements.txt` (owner proposal, 2026-10-02).
- **[RR]** `research-radio.md`. **[RF]** `research-fab.md`. **[RRF]** `research-rf.md`.
  **[O0]** `../order0/order0-design.md`. **[T58]** `../transceiver-5g8/system-design.md`.
- **[TI-DS]** TI, _IWR6843, IWR6443 Single-Chip 60- to 64-GHz mmWave Sensor_, SWRS219F (Oct 2018,
  rev. Apr 2025). Re-checked: Table 6-1 (RX1 M2 … RX4 F2, pinmux modes for F14/H14/E14/D13, LVDS
  balls), §6.2.2 VOUT_PA, §7.11 RθJA 22.3 °C/W, §7.12.1 wake-up, §8.3.1 3-TX mode, Table 9-1
  heading. <https://www.ti.com/lit/ds/symlink/iwr6843.pdf>
- **[TI-ERR]** TI, _IWR6843/IWR6443 Silicon Errata_, SWRZ087D (Dec 2022), ANA#21, ANA#22A.
  <https://www.ti.com/lit/pdf/SWRZ087>
- **[TI-PMIC]** TI, _LP87524B/J/P-Q1_, SNVSAW2B (Dec 2018): OTP defaults table, CLKIN 1–24 MHz,
  spread spectrum only with the internal RC oscillator (§7.3.1.4), load-current measurement.
  <https://www.ti.com/lit/ds/symlink/lp87524j-q1.pdf>
- **[TI-EFUSE]** TI, _TPS25947xx eFuse_, SLVSFC9C (rev. May 2026), Table 4 device comparison.
  <https://www.ti.com/lit/ds/symlink/tps25947.pdf>
- **[TI-DCA]** TI, _DCA1000EVM Data Capture Card_ user's guide, SPRUIJ4A (May 2018, rev. May 2019),
  §2.5.3 and Table 5. <https://www.ti.com/lit/pdf/spruij4>
- **[TI-EVM]** TI, _60GHz mmWave Sensor EVMs_, SWRU546E (rev. May 2022) ("Direct DCA1000 via 60Pin
  Samtec Header"). <https://www.ti.com/lit/pdf/swru546>
- **[TI-LEVM]** TI, IWR6843LEVM schematic SWRR178 (PROC116A; CAN/SPIA mux) and design files SWRR179.
  <https://www.ti.com/lit/zip/SWRR178> , <https://www.ti.com/lit/zip/SWRR179>
- **[TI-RF]** TI, _mmWave Radar sensor RF PCB Design, Manufacturing and Validation Guide_, SPRACG5
  (May 2018). <https://www.ti.com/lit/pdf/spracg5>
- **[CFR]** 47 CFR 15.255 (eCFR as of 2026-09-30): (c)(2)(iii), (d), (f), (h).
  <https://www.ecfr.gov/api/versioner/v1/full/2026-09-30/title-47.xml?part=15&section=15.255>
- **[WE]** Würth Elektronik 36103205S WE-SHC frame datasheet.
  <https://www.we-online.com/components/products/datasheet/36103205S.pdf> (LCSC C7386558, $1.08 at
  1k)
- **[JLC]** JLCPCB/LCSC public parts search, 2026-10-03: C2866258, C701982, C655211, C15957,
  C2908116, C254377, C2908148, C5687033, C3234119, C3662807, C133065, C2843756.
  <https://www.lcsc.com/product-detail/C2866258.html> (the other parts follow the same URL pattern)
- **[PW]** PCBWay capability, advanced-capability, order-form and assembly pages, as cited in [RF §1]
  and [RF Sources].
- **[YAP]** yapnr (local clone): `claude/fab-order` (04b735e: `yapnr fab build|check|preview`,
  `yapnr order stage`, fab data incl. `jlc06161h-3313`, `jlc-6l`); `claude/rf-topopt` @ 90f2dc2;
  `claude/rf-gcp` 47f910c; `claude/rf-kernels`; gap tracks `hier/gapfix/results-*.md`;
  `hier/cloud/READY.md`.

## Owner decisions, 2026-10-03/04 (recorded by the main loop)

- **D14, PA supply corner (option 1, owner 2026-10-04):** the RF macro owns the VOUT_PA / 1V0_PA feed.
  The macro generator draws a short feed from balls A2/B2 into the pocket, with vias there down to the
  bottom-side PA decoupling. The board rules stay as they are: no interstitial 0.35/0.15 supply vias
  and no PWR clearance cut under U1. Via count and size are sized for the PA current. The feed is
  part of the simulated macro (RF sign-off covers it). This lands in stage 3b, on top of the
  rf-uniform fix.
- **Meander/keepout principle (owner 2026-10-03):** identical structures across the array inside the
  keepout; no meander may open into the patch cut-out; no unstitched coplanar ground (rf-uniform).
- **Solvers (owner 2026-10-04):** openEMS is the sweep workhorse on GCP; AWS Palace is integrated as
  an independent FEM sign-off; MEEP is not used.
- **Defaults taken, owner may override:**
  - the 183 uF on the 1.0 V rail (TI ISK precedent), checked at bring-up;
  - VOUT_PA caps on the bottom side (double-sided assembly for those parts).
- **D15, ground stitching study (owner 2026-10-04, stage 3b):** keep 0.45 mm fences along feeds,
  launches and the TX/RX isolation line, and the "stitch or remove every edge and finger" rule.
  Compare three open-pour variants by EM before choosing:

  - (A) today's 0.60 mm grid;
  - (B) about a 1.0 mm grid with a hard maximum distance from any L1 GND point to a via (closes the
    2.9 mm unstitched corners of the stage-2 board);
  - (C) no L1 pour in the antenna area except the GCPW ground strips with their fences.

  Evidence: Palace eigenmode of the stitched L1-L2 plane pair (cavity modes vs the 60.3-63.8 GHz
  band), and the bank model (openEMS + Palace) for TX-RX isolation (>= 27 dB required, 35 design),
  active return loss and patterns. Pick by isolation, return loss and pattern first, then via count
  (0.15 mm drills on the hybrid are cost and yield).
