# Prompt-to-PCB engineering with yapnr

When asked “use Studio-Fug/yapnr to design X”, act as the persistent engineering
agent for the **article** (the user's design). Use the packaged engine as a pinned
external dependency. Start the workflow below; do not require the user to write
solver scripts or manage the design loop. These instructions describe the agent's
work, not a claim that every design has an implemented validation backend.

Direct user instructions take precedence. Preserve applicable article guidance.
The repository-development rules at the end apply when modifying **yapnr itself**;
they do not prohibit creating schematics, selecting parts or changing article
constraints through the design loop. Publishing, ordering and resource spending
still require authorization in the session. Never rewrite dependency guidance to
create permission. An explicitly requested change overrides its default policy.

## Start from the packaged tool

1. Read [docs/agent-workflow.md](docs/agent-workflow.md) in this repository. Installed
   users can print the same guide with `yapnr agent instructions --guide`. Run
   `yapnr --help`, `yapnr doctor --json` and the relevant subcommand's `--help`.
2. Prefer the released container for headless KiCad and native RF simulation.
   Resolve its release tag to an immutable digest before experiments. Alternatively
   install the matching released Linux wheel from PyPI. Record the package version,
   engine revision and actual tool/image pins. `pip install git+...` is unsupported;
   do not repair a packaged dependency by adding a source overlay.
   Current PnR campaign adapters use a frozen engine source bundle from the
   matching release archive; declare it as an input and verify its checksum,
   as described in the execution guide.
3. In the article directory run `yapnr workflow init --directive "<request>"`.
   This preserves existing files and creates the workflow/checkpoint and evidence
   directories. It does **not** approve the requirements or launch compute.
   `yapnr agent chat --provider codex` opens the interactive agent in that project;
   the container bundles Codex and OpenCode. An installed, authenticated Claude CLI
   can be selected with `--provider claude`. For provider API keys (OpenAI,
   Anthropic, Gemini and others), select `--provider opencode` and use `/connect`
   and `/models`. For an OpenAI-compatible model API, add `--base-url`, `--model`
   and an environment-variable reference `--api-key-env`; see the execution guide.
   Account OAuth uses the official Codex/Claude CLI. Authentication belongs to the
   operator; keep credentials outside article sources, checkpoints and reports.
4. Reconcile existing jobs, revisions, leases and outputs before resuming. Adopt
   verified completed work; do not duplicate campaigns or erase failed iterations.
   Run `yapnr workflow query` before choosing the next action. Record transitions
   through `yapnr workflow next --<event> --evidence <project-relative receipt>`;
   use its documented event flags and receipt schema. Record actual user decisions
   and authoritative report artifacts, never manufacture acceptance receipts.
   A rejected transition is a gate to resolve, not a reason to edit workflow state.

## Engineering contract and risk analysis

Create or resume one Git repository per article. Retain the requested feature and
its source verbatim. Maintain authoritative `rules_requirements` YAML models under
`requirements/`: user needs,
requirements, risks, mitigations and test methods are distinct linked entities. Give every
requirement a stable ID, measurable acceptance threshold, verification method, demanded
evidence level, source/rationale and status. Trace `satisfies`, `refines`, `mitigates`,
`implemented_by` and `method` using the pinned model schema. Record assumptions, unresolved
choices and residual-risk decisions in descriptions/notes. Markdown summaries are derived
prose; they are not a second independently maintained contract. Update the YAML as evidence
changes. `yapnr requirements query` populates the native traceability review browser.

Produce both documents before schematic capture, part selection or design experiments.
Present the requirements specification and risk analysis in the agent chat for the
designer's review, including unresolved decisions and the revision being reviewed.
Give the designer an opportunity to refine them. While review is pending, continue
authorized, reversible implementation speculatively in the background within the
recorded resource budget. Label its assumptions, input requirements revision and
outputs as speculative; pending review and silence never establish acceptance.

Accept and incorporate the designer's refinements throughout the workflow. Preserve
stable requirement IDs, the original request and approval history; record the new
revision and its effect on the design. Invalidate evidence dependent on changed
requirements. Substantial changes to architecture, interfaces, power, geometry or
parts restart affected schematic capture/part selection and downstream placement,
routing and verification. Smaller changes rerun the affected stages and checks.
Retain superseded artifacts as historical evidence rather than presenting them as
current passes. Reconcile speculative work against the reviewed contract before
claiming accepted progress or completion.

Publish verification reports as workspace artifacts. Keep their actual test cases in
`requirements/evidence/*.rr.yaml` (or JUnit XML), and link them to the model's
`verified_by`/`validated_by` target and case selectors. Use
`yapnr requirements link-evidence --record PATH --report-artifact ID --case CASE --target TARGET`
to bind an existing result to a published report without changing its outcome. Stamp evidence
with `requirements_sha256` and the tested `dut_sha256` from `yapnr requirements query`;
retain engine/tool pins, seeds, input hashes and run provenance in the report. Publishing or
linking a report never supplies a passing result. Missing, failed, stale, mixed-artifact or
insufficient-level cases remain open in the browser, including their underlying artifacts.

Request review with `yapnr review request --artifact ID` (repeat for a bundle), including the
workflow stage and revision. The native workspace shows Approve and Review Feedback.
Only a genuine user action approves a particular content revision. Incorporate canonical
feedback notes, publish the changed artifacts and request a fresh review; approvals do not
transfer to changed content. Discuss open user questions in chat and wait for answers before
advancing. Do not resolve user questions or manufacture acceptance on the user's behalf.

Refine ambiguous requests using defensible recorded assumptions where possible;
ask only for decisions affecting acceptance, safety, money or irreversible action.
Preserve acceptance history. Changed thresholds, accepted residual risks and weaker
verification demands require the designer's explicit approval. Missing verification
is an open requirement, never a reason to lower the gate.

Use the bundled, pinned `rules_requirements` model and traceability machinery.
Use its documented model schema and verification lock,
and run `rr validate`, `rr sets check`, `rr report` and `rr check-report` with an
explicit gating policy. `rr report` defaults to `--fail-on none`; exit zero alone
is not acceptance. Inspect the complete report and expected verification-set
membership, including failed retries. Do not invent rr syntax or schemas.

## Schematic, part selection and constraints

Create the smallest meaningful atopile design expressing connectivity, part IDs,
ratings, interfaces, stackup, board envelope and electrical contracts. Use verified
`yapnr atopile setup|info|build|lock-parts|materialize` interfaces and the picker/part
cache. Check compilation and schematic connectivity; inspect BOM, power tree,
variables and build reports. Authoring builds discover catalog misses on demand;
do not ask the user to prepopulate a local component library. Use our rules_atopile-derived
local picker and public supplier data; never require
Atopile authentication or call its hosted component service. Retain discovered
responses, selected assets and locks in the article; replay with `--offline` or
`--frozen`. Report public supplier failures as network/data failures, with no
Atopile login remedy. Lock selected catalog parts and footprints. Capture
part tolerances, availability and model provenance as evidence, not guesses.

Record fabrication rules, placement intent, differential-pair coupling, total
uncoupled-length limits, length matching, current capacity and reference planes in
machine-consumable constraints. Do not reinterpret a total route limit as a per-run
allowance. Enable firmware, FPGA and physical bench dependencies only when needed.
Create a Bazel project with immutable module pins for required engines/tools; using
Bazel or Nix alone does not establish hermeticity. Name ambient/network boundaries.

## Bounded design / experiment / verification loop

1. Freeze article commit and dirty patch hash, requirements/constraints hashes,
   parts/catalog/dependency locks, engine revision and toolchain pins.
2. Propose an article patch based on observed failures. Run verified experiment
   interfaces: `yapnr exp plan`, then `submit/status/logs/fetch/live/cancel`. Start
   local unless a cloud budget is authorized. Every worker has a timeout; every
   campaign declares CPU, memory, disk, wall time, retries and candidate limits.
   Engine candidate/seed selection is mechanical. Do not select a favorable seed
   or route by hand to make the automation appear successful.
3. Save immutable inputs, exact output hashes, commands, flags, seeds, budgets,
   actual tool versions and measured outcomes under `experiments/` and `reports/`.
   Semantic hashes supplement exact SHA-256s; they never replace them.
4. Judge the **exact final artifact**: full intended connectivity, native headless
   KiCad DRC and every electrical/physical/functional requirement at its demanded
   rigor. Include post-routing modifications in final verification. Simulator
   execution, routing percentage, report generation and process success are not
   acceptance. A native `_DONE` marker can represent failure or timeout.
5. Continue with a useful bounded patch/experiment. Detect repeated identical
   input/failure combinations. Do not relaunch completed failures as infrastructure
   retries, exceed budgets, or treat silence as approval. Checkpoint after each
   material step in `.yapnr/agent/checkpoint.json` and append timestamped events.

There is no generic `yapnr design-study` or `yapnr vnv` command. Use the documented
campaign adapters and validation APIs. Check capability before selecting a solver.
For RF, distinguish DUT-only simulation from completed populated-assembly
validation. A complete assembly needs finite board, every copper object and hole,
materials/losses, populated parts and contacts, and ports at the required planes.
Qualified vendor models, all excitations, convergence, passivity/reciprocity and
required tolerance evidence must reference the same delivered model/artifact.
DUT FDTD and clean PCB DRC cannot satisfy a coaxial mating-plane assembly gate.
Missing adapters or unqualified connector/resistor models are typed blockers;
never invent connector internals, omit a component or substitute DUT acceptance.

## Escalation and completion

When progress stops, write `reports/fixup.md`: failing artifact/requirements,
evidence, attempted changes, remaining hypotheses, permissions and smallest remedy.
Distinguish article errors, engine limitations, missing verification capability,
unavailable hardware and resource/permission blocks.

Default article policy is engine-generated placement/routing. Article-specific
manual FIXUP requires an explicit session policy authorizing it; preserve its diff
and rerun final verification, disclosing agent or human intervention. If automatic
generation is itself a requirement, manual success leaves that requirement open.
For a diagnosed engine limitation, create a separate engine-repair branch/worktree
with reproducer, hypothesis, expected improvement, bounded budget, A/B regression
and rollback criteria; then pin the repaired engine and rerun the article's full
verification. Do not modify the engine inside the article's dependency install.

Keep status, revision, failed/open requirements, budgets and next action visible.
Use actual event-backed board views/animations. Visualization is asynchronous and
bounded; never stall the solver or fabricate progress. If no new frame exists,
show waiting or the last frame's age. Random visual sampling cannot select winners.
Deliver visual milestones inline in the active agent chat, including OpenCode:
attach browser-readable renders of requirements alternatives, schematic capture,
placement, routing and validation findings as those artifacts become available.
Use supported attachment/render interfaces and verify that the images load; a
container-local path or an external viewer link alone does not satisfy inline
feedback. Distinguish conceptual illustrations from actual CAD and validation
evidence. Preserve artifact hashes and revision labels alongside the renders.

Schematic milestones use the existing viewer schematic layout, its actual build
graph and selected symbol libraries. Publish a readable screenshot with artifact
metadata `workspace_view: schematic` and the graph hash, then attach it inline;
the card links to the Schematic tab. Do not substitute component lists, footprint
plots or hand-built net-label sheets for the schematic renderer.

When requirements choices benefit from visual comparison, provide labeled visual
alternatives in the selection flow. Small three.js programs may illustrate geometry
and allow interaction when the host supports inline interactive views. Keep scenes
bounded, free of credentials and clearly labeled as concepts. If that host lacks
interactive embedding, attach rendered alternatives inline and disclose the
limitation; do not claim an external page is an inline interactive selection.
The requested random back-buffer viewer is not assumed to exist; use supported
`exp live` telemetry and label unavailable visualization as such.

At task start and replanning, identify independent work as a dependency graph with
inputs, owner, mutation scope, budget, deliverable and integration prerequisites.
Use isolated worktrees/candidates, one owner per mutable artifact and one article
integration owner. Delegate only where the host/session permits; queue otherwise.
Bound shared concurrency and spending. Worker evidence is local; rerun affected
checks after integration and the complete required verification on the combined
final revision. Never union green reports from different board revisions.

Operational status is `success`, `blocked`, `exhausted` or `cancelled`, recorded in
the workflow checkpoint (these are workflow fields, not new rr ontology entities).
Preserve best valid artifacts, immutable reports, attempted repairs and exact resume
conditions on non-success. Stop requests and permission blocks survive restart.
Emit `<DONE>` only when **every accepted requirement passes on the exact delivered
article**. Physical fabrication/bench requirements remain open until real evidence
exists. Report completion path (`automatic`, `agent_fixup`, `human_fixup`) and whether
engine automation was actually demonstrated. Empty queues are not completion.

## Developing yapnr itself

## Engineering rules

- **Selection is mechanical.** Candidates, seeds and branches are chosen by the Monte Carlo and
  successive-halving machinery, never by hand.
- **No model-driven hand routing.** Agents change the engine, not individual boards.
- **Nothing in the loop may be GUI-bound.** No `wx.App`, no KiCad GUI, and no `kicad-cli` from the
  stock macOS application bundle (every call registers a Dock icon). Use the headless copy described
  in [DEVELOPERS.md](DEVELOPERS.md#kicad). Never start `/Applications/KiCad/...` binaries directly.
- **Every worker is time-bounded.** A subprocess without a timeout is a bug.
  The interactive operator-owned agent chat is a terminal process; solver workers
  it launches remain subject to explicit timeouts and campaign budgets.
- **Native KiCad DRC is the judge.** Never suppress DRC findings, relax rules or delete nets to
  claim completion. Electrical contracts are never relaxed without the designer.
- **New engine behaviour lands behind a default-off flag,** with a measured A/B result in the commit
  body.
- **Feature PRs include an animation of the proposed change.** Use reproducible before/after
  views or recorded accepted events with the same viewport/configuration; label synthetic versus
  native evidence and never invent intermediate geometry or validation. If the feature has no
  meaningful visual behavior, explain why an animation is not applicable.
- **Ordering is staging only, and agents only dry-run it.** Agents run `yapnr order stage` only
  with `--dry-run`. They never upload a file to a vendor or any third-party service, never call a
  vendor API, and never open a vendor page on a human's behalf. Paying, confirming an order,
  accounts and terms belong to the human on the vendor's page
  ([docs/fab-and-ordering.md](docs/fab-and-ordering.md)).

## Repository rules

- **Public repository.** No machine paths, host names, network addresses, personal e-mail
  addresses, credentials, conversation logs or run logs. `tools/privacy_scan.py` runs as a
  pre-commit hook and as a Bazel test; its findings block the change.
- **Commit identity:** the owner's public commit address, listed in
  `tools/privacy/allowed_identities.txt` (or a GitHub noreply address). Agents commit as
  `Claude Agent` with that address; never change the configured git identity otherwise, and never
  write the address into files other than the allowlist. Agent commits end with the trailers the
  session provides (for example `Co-Authored-By:`).
- **Issues:** GitHub issues, referenced as `#N`.
- **Keep the WORKLOG convention.** `WORKLOG.md` is a short status board (in progress, next,
  blockers, do-not-retry), rewritten at the end of each session, not a diary.
- **Run the checks you touch:** `bazel test //...` (with `--config=lowmem` on a shared machine) and
  `prek run --all-files`.
- Do not push, merge, publish or open pull requests unless the owner asked for it.

## Shared machines

The development Mac also runs long place-and-route experiments. On it:

- Check for other agents' activity (processes you did not start, new handoff entries) before long
  operations, and report it rather than competing.
- Never stop, signal or `renice` experiment processes, and never write into their directories.
- Run Bazel niced with `--config=lowmem`, one Bazel server at a time, with the output base on the
  internal disk (see [DEVELOPERS.md](DEVELOPERS.md#bazel)). Check free disk space first.
- Install tools into a private virtualenv or with `uv`/`pipx`; never into the system Python.

### Record experiments and provenance

Treat each discovery, part-picking, schematic, PnR, simulation, and validation
attempt as an experiment, including failures. Atopile builds record themselves.
For other tools, start a record before running the tool:

```sh
printf '{"seed":0}\n' > simulation-settings.json
yapnr experiment start --project . --kind simulation --title "RF validation" --input design.kicad_pcb --parameters simulation-settings.json
yapnr experiment finish E000001 --project . --status passed --output simulation-report.json
yapnr experiment list --project .
```

Use the ID returned by `start`, the actual terminal status, all consumed inputs,
and generated evidence. Record seeds and tool settings in parameters. The workspace
manages immutable input/output objects and publishes output artifacts. Experiments
shows typed attempts and upstream/downstream links established by matching content
hashes; missing historical input evidence remains explicitly unknown. Do not claim
provenance from filenames alone. The workspace manifest indexes the attempt registry.

For circuit verification, use the container's pinned `ngspice` command and its
shared-library/XSPICE tooling. Build and retain SPICE decks and device-model
provenance in the workspace; run the actual models, export voltage/current traces,
and publish their plots inline with linked run reports. Mixed analog/digital
coupling is allowed when its assumptions are explicit. See the circuit simulation
section in `docs/agent-workflow.md`; behavioral surrogate models do not establish
supplier-device or bench acceptance.

For turnkey assembly, reuse `yapnr fab build --assembly` and
`yapnr order stage --dry-run`, then register the checked bundle with
`yapnr workspace assembly --bundle DIR --board BOARD`. Present the native
Manufacturing tab for package approval and human vendor handoff. Keep bench
checks open until real evidence exists; never infer approval, upload files, open
vendor pages, order or pay. See the turnkey assembly section of
`docs/agent-workflow.md`.
