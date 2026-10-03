# yapnr documentation

**yapnr** ("yet another place and route") places components and routes copper for KiCad printed
circuit boards. It uses a mechanical, Monte-Carlo-driven search:

- hierarchical block synthesis: blocks become macros, with a library of trials per template;
- power-first placement: power tiers, trunks and hot loops are derived from the design;
- native KiCad routing and design-rule checks (DRC) in the loop, with KiCad's own DRC as the judge;
- electrical contracts (current, differential pairs, plane access) declared next to the design.

The engine grew inside the [Splanc](https://github.com/fughilli/splanc) repository, where it laid
out the Splanc Mini board. It is being migrated here in reviewable pull requests; until that is
done, this repository holds the project scaffolding (build, CI, documentation) and the plan.

## Status

Alpha. The engine's committed history (PR1) and its newer, never committed state (PR2) are
imported from Splanc at their original paths under `hardware/` (see the
[history import manifest](history/import-manifest.md)); the package restructuring follows. The
command line currently offers `yapnr --version` and a `yapnr doctor` stub; the live viewer
(PR4) runs with `bazel run //:viewer`. Progress is tracked in
[WORKLOG.md](../WORKLOG.md) and in GitHub issues.

## Start here

- [Container images](containers.md): run yapnr with Docker and nothing else (KiCad included).
- [The live viewer](viewer.md): watch experiments in a browser; its configuration, optional
  services and the cost and security of the (off by default) Ask agent.
- [The atopile toolchain](frontends/atopile.md): build atopile projects offline, without Nix.
- [The part cache](part-cache.md): where part data lives instead of the repository.
- [Fab bundles and staged orders](fab-and-ordering.md): check a routed board under OSH Park's,
  JLCPCB's or PCBWay's rules, build the files to upload and an order card, and open the vendor's
  page. yapnr never uploads, orders or pays.
- [Releases and versioning](releases.md): version numbers, image tags, what a release publishes.
- [RF fab-model test coupons](rf-fab-coupons.md): coupon boards that measure the fab's stackup
  (εr, loss, heights, copper, etch, mask), and the extraction that turns VNA data into a fitted
  stackup with uncertainties.
- [Cloud and HPC experiments](cloud-experiments.md): `yapnr exp` campaigns on a local pool, Google
  Cloud Batch (Spot VMs) or a Slurm allocation, with cost guards; the owner's bootstrap runbook.
- [Regression ladder](regression-ladder.md): eight boards of rising complexity, up to a TLC555 +
  CD4017B LED chaser, with an animation of each board's place and route.
- [Constraints and hierarchy](constraints-and-hierarchy.md): a line of LEDs, parts held on the
  board edge and a board built from reused blocks, each animated from the engine's own record.
- [Architecture](architecture.md): the planned layout of the package, the test tiers and the
  pipeline.
- [Migration plan](migration-plan.md): how the engine moves out of Splanc, PR by PR.
- [Decisions](decisions.md): the owner's decisions and the pinned tool versions.
- [History import manifest](history/import-manifest.md): what PR1 and PR2 imported from Splanc,
  and how it was rewritten and checked.
- [About the name](about-the-name.md): "yet another place and route", and the circuit tree.
- [Foundations and references](references.md): the papers and open-source projects behind the
  place-and-route loop (DREAMPlace, freerouting, tscircuit, PathFinder and more), and what yapnr
  takes from each.

For contributors and agents:

- [DEVELOPERS.md](../DEVELOPERS.md): setup, everyday commands, Bazel and KiCad notes.
- [CONTRIBUTING.md](../CONTRIBUTING.md): contribution policy, commit style, license terms.
- [AGENTS.md](../AGENTS.md): the rules automated agents follow in this repository.
- [THIRD_PARTY.md](../THIRD_PARTY.md): third-party material and its licenses.

## License

yapnr is free software under the GNU Affero General Public License, version 3 or (at your option)
any later version (`AGPL-3.0-or-later`). See [LICENSE](../LICENSE).

```{toctree}
:hidden:
:caption: Using yapnr

containers
viewer
frontends/atopile
part-cache
fab-and-ordering
releases
rf-fab-coupons
cloud-experiments
```

```{toctree}
:hidden:
:caption: Project

architecture
regression-ladder
constraints-and-hierarchy
migration-plan
decisions
design/animations
design/constraint-and-hier-animations
design/fea-integration
design/rf-fab-coupons
design/cloud-experiments
design/fab-and-ordering
design/gloss
history/import-manifest
about-the-name
references
```

```{toctree}
:hidden:
:caption: Contributing

/DEVELOPERS
/CONTRIBUTING
/AGENTS
/WORKLOG
/THIRD_PARTY
/README
```
