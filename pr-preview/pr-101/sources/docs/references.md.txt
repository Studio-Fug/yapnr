# Foundations and references

yapnr is an open-source place-and-route engine for KiCad printed circuit boards. It searches
mechanically: many Monte-Carlo placement starts are ranked by measured results and promoted through
stages of rising cost, subcircuits are synthesised as reusable blocks and placed as rigid macros,
and native KiCad routing and design-rule checks (DRC) run inside the loop, with KiCad's own DRC as
the judge.

This page lists the research papers and open-source projects that the place-and-route loop builds
on, and what yapnr takes from each. yapnr's source does not copy code from any of them: the
algorithms were reimplemented from the publications and documentation, and tools such as KiCad and
ngspice are used as external programs or libraries. Third-party material that yapnr uses or ships
is listed in [THIRD_PARTY.md](../THIRD_PARTY.md).

## The three projects yapnr learned most from

### DREAMPlace

Yibo Lin, Shounak Dhar, Wuxi Li, Haoxing Ren, Brucek Khailany and David Z. Pan, "DREAMPlace: Deep
Learning Toolkit-Enabled GPU Acceleration for Modern VLSI Placement", _Proceedings of the 56th
Design Automation Conference (DAC)_, 2019,
[doi:10.1145/3316781.3317803](https://doi.org/10.1145/3316781.3317803). Extended version: Yibo Lin,
Zixuan Jiang, Jiaqi Gu, Wuxi Li, Shounak Dhar, Haoxing Ren, Brucek Khailany and David Z. Pan, _IEEE
Transactions on Computer-Aided Design of Integrated Circuits and Systems_ 40(4):748–761, 2021,
[doi:10.1109/TCAD.2020.3003843](https://doi.org/10.1109/TCAD.2020.3003843). Code:
[github.com/limbo018/DREAMPlace](https://github.com/limbo018/DREAMPlace).

DREAMPlace casts global placement as training a neural network: a smooth wirelength model and a
spreading term are differentiable tensor operations, and an optimizer from a deep learning toolkit
moves the cells. yapnr's global placer takes the same view. It relaxes part positions in PyTorch by
gradient descent on a smooth (log-sum-exp) half-perimeter wirelength plus penalties for courtyard
overlap, the board outline, keep-outs, edge alignment and grouping, and then legalizes the result
to a strictly non-overlapping placement. yapnr runs this on the CPU, single-threaded under a fixed
seed, so the same inputs give the same placement on a given platform.

### freerouting

[github.com/freerouting/freerouting](https://github.com/freerouting/freerouting): an open-source
PCB autorouter for any design tool that speaks the Specctra (or Electra) DSN interface. It reads a
`.dsn` design and writes the routed result as a `.ses` session file.

freerouting was yapnr's first detailed router. The early pipeline placed the board, wrote the
placement and per-class track widths back into KiCad, and handed the board to freerouting through
a DSN/SES round trip. Measuring where that run left connections unrouted on a dense, fine-pitch
four-layer board shaped the next steps: most of the open connections were high-fanout ground and
power nets, which led to plane fanout, and the rest led to yapnr's own negotiated detailed router,
which now replaces freerouting in the main pipeline. The Specctra round trip remains as an
optional, external comparison: because such a round trip can merge net names that differ only in
case, yapnr exports unique ASCII net names and restores the originals on import.

### tscircuit

[tscircuit.com](https://tscircuit.com) and [github.com/tscircuit](https://github.com/tscircuit):
an open-source (MIT) project for designing electronics with TypeScript and React. Its built-in
autorouter is [capacity-autorouter](https://github.com/tscircuit/capacity-autorouter).

yapnr's developers ran the tscircuit autorouter on a dense four-layer test board, through a
converter, and read its source. Two ideas from that review are now part of yapnr's detailed
router, and it confirmed a third that yapnr already followed:

- **Plan pin escapes and region-boundary crossings before detailed geometry.** yapnr assigns
  terminal access for the pads of an interacting cluster jointly, and coordinates bounded portal
  choices before the expensive trunk search, instead of committing one escape at a time.
- **Repair several nets together inside a bounded region.** yapnr's regional repair reopens the
  copper in a region, keeps its external terminals fixed, searches route orders and resolves
  conflicts between the provisional routes, and commits only a complete, conflict-free result.
- **Keep geometric cleanup as a separate, checked stage.** yapnr's shortcut relaxation runs after
  a route is found, and every shortened segment is checked by the same exact clearance oracle as
  the search.

## Placement

- **ePlace.** Jingwei Lu, Pengwen Chen, Chin-Chih Chang, Lu Sha, Dennis Jen-Hsin Huang, Chin-Chi
  Teng and Chung-Kuan Cheng, "ePlace: Electrostatics-Based Placement Using Fast Fourier Transform
  and Nesterov's Method", _ACM Transactions on Design Automation of Electronic Systems_ 20(2), 2015,
  [doi:10.1145/2699873](https://doi.org/10.1145/2699873). The analytical formulation DREAMPlace
  builds on: minimize wirelength plus a density penalty, then legalize. yapnr keeps that structure
  but uses a simpler courtyard-overlap penalty in place of the electrostatic density.
- **RePlAce.** Chung-Kuan Cheng, Andrew B. Kahng, Ilgweon Kang and Lutong Wang, "RePlAce:
  Advancing Solution Quality and Routability Validation in Global Placement", _IEEE Transactions on
  Computer-Aided Design of Integrated Circuits and Systems_ 38(9):1717–1730, 2019,
  [doi:10.1109/TCAD.2018.2859220](https://doi.org/10.1109/TCAD.2018.2859220). yapnr uses its cell
  inflation for routability: a part that sits in a region that stays congested in the lookahead
  route gets a larger reserved footprint, so the next placement round spreads it out.
- **Cypress.** Niansong Zhang, Anthony Agnesina, Noor Shbat, Yuval Leader, Zhiru Zhang and Haoxing
  Ren, "Cypress: VLSI-Inspired PCB Placement with GPU Acceleration", _Proceedings of the 2025
  International Symposium on Physical Design (ISPD)_, pp. 31–41,
  [doi:10.1145/3698364.3705346](https://doi.org/10.1145/3698364.3705346). Code:
  [github.com/NVlabs/Cypress](https://github.com/NVlabs/Cypress). The closest prior art: GPU
  analytical placement adapted to PCBs. Following Cypress, yapnr relaxes each part's discrete
  orientation into a continuous one, so that orientation is optimized together with position. In
  yapnr, a movable part carries a categorical distribution over the four 90-degree rotations, its
  pin offsets and courtyard become expectations under that distribution, and the angle snaps to
  the most likely rotation at the end. yapnr reimplements this from the paper.
- **Gumbel-Softmax and the Concrete distribution.** Eric Jang, Shixiang Gu and Ben Poole,
  "Categorical Reparameterization with Gumbel-Softmax", ICLR 2017,
  [arXiv:1611.01144](https://arxiv.org/abs/1611.01144); Chris J. Maddison, Andriy Mnih and Yee
  Whye Teh, "The Concrete Distribution: A Continuous Relaxation of Discrete Random Variables", ICLR
  2017, [arXiv:1611.00712](https://arxiv.org/abs/1611.00712). The continuous relaxation behind the
  orientation choice. yapnr uses a deterministic variant: a softmax whose temperature is annealed
  toward one-hot, without sampled noise.

## Routing

- **Lee's maze router.** C. Y. Lee, "An Algorithm for Path Connections and Its Applications", _IRE
  Transactions on Electronic Computers_ EC-10(3):346–365, 1961,
  [doi:10.1109/TEC.1961.5219222](https://doi.org/10.1109/TEC.1961.5219222). Grid-based maze
  routing. yapnr's detailed grid uses a pitch of at least track width plus clearance, so a route
  that does not share a cell with another net is clear of it by construction.
- **A\* search.** Peter E. Hart, Nils J. Nilsson and Bertram Raphael, "A Formal Basis for the
  Heuristic Determination of Minimum Cost Paths", _IEEE Transactions on Systems Science and
  Cybernetics_ 4(2):100–107, 1968,
  [doi:10.1109/TSSC.1968.300136](https://doi.org/10.1109/TSSC.1968.300136). The path search in the
  multi-layer detailed router, where a via is a move with its own cost, and in the octilinear
  repair router, whose search state includes the heading so that bends carry a cost.
- **PathFinder.** Larry McMurchie and Carl Ebeling, "PathFinder: A Negotiation-Based
  Performance-Driven Router for FPGAs", _Proceedings of the 3rd ACM International Symposium on
  Field-Programmable Gate Arrays (FPGA)_, pp. 111–117, 1995,
  [doi:10.1145/201310.201328](https://doi.org/10.1145/201310.201328). Negotiated congestion: rip
  up and reroute every net each pass, with a present-sharing cost that grows with overuse and a
  history cost that accumulates and never forgets. yapnr uses it twice: in the coarse lookahead
  global router that judges each placement, and in the detailed maze router, where nets negotiate
  until no grid cell is shared. The global router's history map is also the feedback signal that
  drives the cell inflation above, which damps the place-and-route hand-off instead of letting it
  oscillate.
- **Prim's algorithm.** R. C. Prim, "Shortest Connection Networks and Some Generalizations",
  _Bell System Technical Journal_ 36(6):1389–1401, 1957,
  [doi:10.1002/j.1538-7305.1957.tb01515.x](https://doi.org/10.1002/j.1538-7305.1957.tb01515.x).
  Multi-pin nets are split into two-pin connections by a rectilinear minimum spanning tree, and the
  detailed router grows each net's tree by connecting the next pin to the copper already routed.
- **TritonRoute.** Andrew B. Kahng, Lutong Wang and Bangqi Xu, "TritonRoute: The Open-Source
  Detailed Router", _IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems_
  40(3):547–559, 2021, [doi:10.1109/TCAD.2020.3003234](https://doi.org/10.1109/TCAD.2020.3003234),
  part of [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD). Its separation of
  pin-access analysis from track assignment and search-and-repair also informed yapnr's joint
  terminal-access stage.
- **Conflict-based search.** Guni Sharon, Roni Stern, Ariel Felner and Nathan R. Sturtevant,
  "Conflict-based search for optimal multi-agent pathfinding", _Artificial Intelligence_
  219:40–66, 2015,
  [doi:10.1016/j.artint.2014.11.006](https://doi.org/10.1016/j.artint.2014.11.006). yapnr's joint
  regional repair borrows its branching: when two planned routes conflict, the search branches on
  rerouting either one around the conflicting copper. It is a bounded geometric heuristic, not a
  complete or optimal implementation of the algorithm.
- **Hildreth's quadratic programming procedure.** Clifford Hildreth, "A quadratic programming
  procedure", _Naval Research Logistics Quarterly_ 4(1):79–85, 1957,
  [doi:10.1002/nav.3800040113](https://doi.org/10.1002/nav.3800040113). The shove stage, which
  makes room for a blocked power route by moving copper and parts, solves a small convex quadratic
  program for the displacements by Hildreth's dual coordinate ascent. It needs no linear algebra
  library, so it runs inside KiCad's Python, and its dual multipliers name the binding constraints.

## Search and orchestration

- **Successive halving.** Zohar Karnin, Tomer Koren and Oren Somekh, "Almost Optimal Exploration
  in Multi-Armed Bandits", _Proceedings of the 30th International Conference on Machine Learning
  (ICML)_, PMLR 28(3):1238–1246, 2013,
  [proceedings.mlr.press/v28/karnin13.html](https://proceedings.mlr.press/v28/karnin13.html);
  Kevin Jamieson and Ameet Talwalkar, "Non-stochastic Best Arm Identification and Hyperparameter
  Optimization", _Proceedings of the 19th International Conference on Artificial Intelligence and
  Statistics (AISTATS)_, PMLR 51:240–248, 2016,
  [proceedings.mlr.press/v51/jamieson16.html](https://proceedings.mlr.press/v51/jamieson16.html).
  yapnr's Monte-Carlo driver is successive halving over complete placements: many independent
  starts are legalized and scored by a cheap routability proxy, and each later stage (a fast
  detailed-route screen, a short native KiCad run, then the full electrical pipeline) ranks the
  survivors by its own measured objective and promotes a fixed fraction. No candidate is chosen by
  hand. A seeded random control sample, and the rank agreement between stages, measure how well
  each cheap stage predicts the next.
- **Hyperband.** Lisha Li, Kevin Jamieson, Giulia DeSalvo, Afshin Rostamizadeh and Ameet
  Talwalkar, "Hyperband: A Novel Bandit-Based Approach to Hyperparameter Optimization", _Journal of
  Machine Learning Research_ 18(185):1–52, 2018,
  [jmlr.org/papers/v18/16-558.html](https://jmlr.org/papers/v18/16-558.html). Runs several
  successive-halving brackets with different trade-offs between the number of candidates and the
  budget per candidate; yapnr currently runs a single bracket with fixed stage budgets.
- **Simulated annealing.** S. Kirkpatrick, C. D. Gelatt and M. P. Vecchi, "Optimization by
  Simulated Annealing", _Science_ 220(4598):671–680, 1983,
  [doi:10.1126/science.220.4598.671](https://doi.org/10.1126/science.220.4598.671). Relocation
  moves for parts are chosen by Boltzmann sampling at a temperature, with a seeded random generator
  and a finite-window plateau test; a plateau is reported as a lack of improvement under the budget,
  not as a proof of optimality.

## Electrical and signal integrity

- **IPC-2221B, IPC-2152 and IPC-2141A** ([IPC](https://www.electronics.org/)). IPC-2221B,
  _Generic Standard on Printed Board Design_: its current-versus-cross-section relation turns a
  net class's declared current into a minimum track width for inner and outer copper. This is a
  screening model; IPC-2152, _Standard for Determining Current-Carrying Capacity in Printed Board
  Design_, remains the thermal sign-off that it does not replace. IPC-2141A supplies the
  embedded-microstrip and stripline impedance formulas for inner layers.
- **Hammerstad and Jensen.** E. Hammerstad and Ø. Jensen, "Accurate Models for Microstrip
  Computer-Aided Design", _1980 IEEE MTT-S International Microwave Symposium Digest_, pp. 407–409,
  [doi:10.1109/MWSYM.1980.1124303](https://doi.org/10.1109/MWSYM.1980.1124303). Characteristic
  impedance and effective permittivity of surface microstrip, with the thickness correction, for
  the signal-integrity (SI) decks.
- **Kirschning and Jansen.** M. Kirschning and R. H. Jansen, "Accurate Wide-Range Design Equations
  for the Frequency-Dependent Characteristic of Parallel Coupled Microstrip Lines", _IEEE
  Transactions on Microwave Theory and Techniques_ 32(1):83–90, 1984,
  [doi:10.1109/TMTT.1984.1132616](https://doi.org/10.1109/TMTT.1984.1132616). A published reference
  for checking the planned 2D field solver, together with Hammerstad and Jensen.
- **Johnson and Graham.** Howard Johnson and Martin Graham, _High-Speed Digital Design: A Handbook
  of Black Magic_, Prentice Hall, 1993. Via inductance and capacitance for the lumped SI model.
- **IBIS and KiCad's KIBIS.** The I/O Buffer Information Specification, maintained by the
  [IBIS Open Forum](https://www.ibis.org/). Driver behaviour comes from vendors' IBIS models,
  converted to SPICE by KiCad's built-in IBIS support (KIBIS) through the headless `kicad-cli`.
- **ngspice.** [ngspice.sourceforge.io](https://ngspice.sourceforge.io/), the open-source SPICE
  simulator. yapnr runs each SI deck as a time-bounded ngspice job, first on the ideal
  (zero-length) board and then on the routed copper, so that a failure can be classified as caused
  by the layout or by the design.

## Software yapnr builds on

- **KiCad** ([kicad.org](https://www.kicad.org/)). The board database, the `pcbnew` Python API
  for reading and writing boards, `kicad-cli` for exports, and KiCad's own DRC, which judges every
  candidate inside the loop.
- **PyTorch.** Adam Paszke et al., "PyTorch: An Imperative Style, High-Performance Deep Learning
  Library", _Advances in Neural Information Processing Systems 32 (NeurIPS 2019)_,
  [arXiv:1912.01703](https://arxiv.org/abs/1912.01703). The automatic differentiation behind the
  global placer.
