# Design a PCB with an agent

Give your agent the directive “Please use Studio-Fug/yapnr to design X”. Its
engineering procedure is [AGENTS.md](../AGENTS.md), also shipped in the installed
wheel. The agent owns requirements capture, risk analysis, schematic/part selection,
bounded placement/routing experiments and verification of the delivered artifact.
An unavailable validator or vendor model remains an explicit blocker.

## Review and visual feedback

The agent must write the requirements specification and risk analysis before
starting design, then present both in chat for review. It may continue authorized,
reversible implementation speculatively while the designer reviews, recording the
requirements revision and assumptions behind that work. Review is not approval by
silence. Designer refinements become tracked revisions; changed requirements
invalidate dependent evidence and restart affected stages, including schematic
capture and part selection when the change is substantial.

Visual feedback belongs inline in the active agent session, including OpenCode.
Attach renders as schematic, placement, routing and validation artifacts become
available, with provenance and clear concept-versus-CAD labels. Verify attachment
delivery rather than relying on container-local image paths. External viewers
supplement those renders. For requirements alternatives, use small bounded
three.js scenes in hosts that support inline interactive selections; otherwise
provide inline still renders and identify the missing interactive capability.

## Evidence-gated iteration controller

```sh
yapnr workflow init --project article --directive 'Design X'
yapnr workflow query --project article
yapnr workflow next --project article --requirements-specification-ready \
  --evidence reports/requirements-ready.json
yapnr workflow next --project article --user-accepted-requirements-specification \
  --evidence reports/user-review.json
```

`init` creates or resumes the Git project and engineering files without overwriting
existing contracts. `query` returns JSON: current state and revision, whether the
contract is current, whether work is speculative, available event flags, transition
history and a receipt template. `next` accepts exactly one event flag and a
project-relative evidence receipt. The declarative transition table ships with
the package; it cannot execute arbitrary scripts. State/history are atomically
stored under `.yapnr/workflow/`, with a per-project writer lock and immutable
content-addressed copies of receipts, contract documents and supplied artifacts.

Receipts use this shape, with hashes from `query` and actual artifact SHA-256s:

```json
{
  "schema": "yapnr-workflow-evidence-v1",
  "event": "user-accepted-requirements-specification",
  "revision": 1,
  "contract": { "manifest": "<SHA-256>", "risks": "<SHA-256>" },
  "source": "user",
  "reviewer": "designer",
  "note": "Explicit user acceptance; reference the review decision.",
  "artifacts": []
}
```

| Event flag                                        | Guard and resulting stage                                                                                                                                                                                                                                              |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--requirements-specification-ready`              | Both documents written; receipt supplies the complete unique `requirements` ID list. Enter review and increment the contract revision.                                                                                                                                 |
| `--speculative-work-started`                      | Review already presented. Enter schematic while acceptance remains pending.                                                                                                                                                                                            |
| `--user-accepted-requirements-specification`      | User receipt and reviewer for the current exact contract. Start schematic or accept the current speculative stage.                                                                                                                                                     |
| `--user-revised-requirements-specification`       | User receipt, reviewer, changed contract documents and updated requirement IDs. Invalidate acceptance and current artifacts, increment revision and restart review. This initial controller conservatively repeats schematic and downstream work after every revision. |
| `--schematic-ready`                               | Hashed implementation artifacts. Enter placement/routing.                                                                                                                                                                                                              |
| `--routing-finished`                              | Hashed routed artifacts. Enter verification.                                                                                                                                                                                                                           |
| `--verification-passed`                           | Current revision accepted; complete passing requirement checks, native DRC and connectivity gates on the same unchanged routed artifact; hashed verification reports. Enter complete.                                                                                  |
| `--verification-failed`                           | Hashed failure reports and explanation. Enter fixup.                                                                                                                                                                                                                   |
| `--fixup-applied`                                 | Hashed revised implementation. Repeat schematic and downstream verification.                                                                                                                                                                                           |
| `--engine-repair-required`                        | Hashed repair reports/reproducers. Enter engine repair.                                                                                                                                                                                                                |
| `--engine-repair-ready`                           | Hashed repaired inputs. Repeat schematic and downstream verification.                                                                                                                                                                                                  |
| `--blocked`, `--methods-exhausted`, `--cancelled` | Record the reason and preserve the previous stage.                                                                                                                                                                                                                     |
| `--resume`                                        | Explicit user receipt; return to the preserved stage.                                                                                                                                                                                                                  |

An artifact entry is `{"path": "design/board.kicad_pcb", "sha256": "<SHA-256>"}`.
A completion receipt supplies `final_artifact_sha256`, a `checks` list containing
exactly one entry per accepted requirement (`requirement_id`, `status: "pass"`,
`artifact_sha256`), and `gates.native_drc` and `gates.connectivity`, each containing
`status: "pass"` and the same `artifact_sha256`. Save the underlying reports in
its `artifacts` list. Missing, skipped, stale and mixed-artifact checks are rejected.

This controller validates transitions, receipt structure and preserved hashes; it
does not authenticate the claimed reviewer or independently reproduce simulations,
native DRC or physical measurements. Agents must faithfully record real user
decisions and authoritative tool/bench reports. It does not sandbox the agent,
launch solver jobs, enforce their resource consumption or add an interactive
three.js renderer to OpenCode. The agent must still follow the engineering policy
and bounded experiment interfaces. An engine `_DONE` event is not workflow success.
Revising requirements while blocked, exhausted or cancelled preserves that stop
state; an explicit user resume then returns to requirements review.

## Start a container chat

The application image bundles checksum-pinned Codex and OpenCode CLIs for both Linux
architectures. No credentials or default paid sessions are built into it. Use a
release tag, resolve and record its digest, then start an interactive terminal:

```sh
IMG=ghcr.io/studio-fug/yapnr:0.1.0
mkdir -p article
# Persist provider authentication separately from the design repository.
docker volume create yapnr-agent-home
docker run --rm -it --name yapnr-design --cpus 4 --memory 8g --shm-size 1g \
  --pids-limit 4096 --stop-timeout 120 \
  -v "$PWD/article:/project" -v yapnr-agent-home:/var/lib/yapnr \
  "$IMG" agent chat
```

Authenticate with the provider when prompted, then type the design directive into
chat. To supply it immediately, add `--directive "Design a ..."`. Account/model
usage limits belong to the provider; Docker's limits only bound local computation.
The agent must record an explicit experiment budget before launching campaigns.
Use `docker stop yapnr-design` to interrupt. Resume using the same project/home
mounts and the checkpoint. No socket to the host Docker daemon is required.

### Choose authentication and a model

Use `--provider codex` for the official Codex account-login flow, or
`--provider claude` for the official Claude Code account-login flow. Account login
is handled by those tools, not by a yapnr OAuth proxy. In a headless container,
prefer device-code login: run `codex login --device-auth` inside the container.
Enable device-code login in your ChatGPT security settings or workspace permissions
if required. Claude Code is
operator-installed and is not redistributed in the image. Persist its installation
and authentication in the separate home volume. See the official
[Codex authentication](https://developers.openai.com/codex/auth/)
and [Claude Code setup](https://code.claude.com/docs/en/setup) instructions.

For **OpenAI, Anthropic, Google Gemini and other provider API keys**, select
`agent chat --provider opencode`. Use OpenCode's `/connect` to supply your own
key and `/models` to select a provider/model. You can also select an already
configured model with `--model provider/model`. No yapnr-managed model account
is required. See [OpenCode providers](https://opencode.ai/docs/providers/).

For **OpenCode's browser UI running in Docker or on a remote machine**, choose
**OpenAI → ChatGPT Pro/Plus (headless)**. Open the authorization page and enter
the one-time code shown in OpenCode, keeping the connection dialog open until
it completes. Then select an OpenAI model in the model picker. This flow does
not require a callback listener on the browser's computer.

The **ChatGPT Pro/Plus (browser)** method returns to `localhost:1455` on the
computer running your browser. It only works if that computer can reach the
callback server running alongside OpenCode. Publishing the web UI's port alone
does not publish the callback. Remote desktop also needs a container-to-host
callback mapping; browsing from another computer additionally needs a tunnel
to that host. Prefer the headless method rather than exposing the callback
publicly. After changing the connection method, start a fresh authorization;
do not copy callback codes or credential files into chat or the article.

For an **OpenAI-compatible model endpoint**, select OpenCode and provide the
server's base URL and exact model ID:

```sh
# Supply YAPNR_MODEL_API_KEY in your shell or a private secrets manager.
# Add `-e YAPNR_MODEL_API_KEY` to the docker run command above, and replace
# its final `agent chat` arguments with:
agent chat --provider opencode --base-url https://models.example.com/v1 \
  --model your-model-id --api-key-env YAPNR_MODEL_API_KEY
```

The launcher references the named environment variable in process-local
configuration; it never copies its value into the article or dry-run output.
An unauthenticated local server can leave the variable unset. In Docker,
`localhost` refers to the container: use a reachable host address for a host model
server. Custom models must support the tools required for the engineering loop;
API compatibility alone does not establish that capability. Endpoint URLs must
not contain credentials or secret query parameters.

Avoid putting keys in the directive, command arguments or design repository.
Provider credentials persist in the separately mounted home. The image has no
agent permission-bypass flags. Agent CLI approval settings remain effective.

`yapnr agent chat --provider claude` selects an operator-installed Claude Code CLI
instead. Claude Code is not redistributed in the image. For an already authenticated
host agent, it can read this repository's AGENTS.md and execute yapnr in the
container against the mounted article, without another model session inside it.
The viewer's “Ask” chat remains read-only and is not the design agent launcher.

## Installed-wheel or source-independent use

```sh
python -m pip install 'yapnr==0.1.0' --extra-index-url https://download.pytorch.org/whl/cpu
yapnr doctor --json
yapnr agent instructions
yapnr agent instructions --guide
yapnr workflow init --project article --directive 'Design X'
yapnr agent chat --project article --provider codex
```

Published wheels support Linux x86-64/ARM64; headless KiCad and external EM solver
runtimes are separate. `agent init` creates missing files/directories and preserves
existing instructions, requirements and checkpoints. It stores a content-addressed
copy of current engine guidance for explicit loading by either provider. The
bootstrap manifest/checkpoint is an operational record, not a requirements-model
schema or approved contract. `chat --dry-run` checks the executable and reports
launch intent without writing files, calling a model or revealing credentials.

## Execute the article loop

The agent establishes acceptance before optimization. Maintain stable requirements
and risk/mitigation links; lock parts, toolchains and engine revisions. Check these
installed interfaces rather than assuming migration-plan commands are available:

| Stage                          | Supported interface / expected evidence                                                                                           |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Environment                    | `yapnr doctor --json`, package version, immutable container digest                                                                |
| Requirements / risk            | Article manifest and risk record; optionally pinned `rules_requirements`, its validation/report/verification-set commands         |
| Schematic / parts              | `yapnr atopile --help`; setup, build, lock-parts, materialize; compilation, connectivity, BOM and ratings                         |
| Placement / routing            | `yapnr exp --help`; declarative bounded campaign, plan/submit/status/logs/fetch, mechanical winner selection                      |
| Progress                       | `yapnr exp live --help`; `python -m yapnr.viewer --help`; real events, board views and immutable reports                          |
| Native physical verification   | `yapnr fab check --help`, headless KiCad DRC and connectivity on exact output                                                     |
| RF DUT design / validation     | Installed `yapnr.rf.driver.design`, `yapnr.rf.validate.resimulate`, footprint/Touchstone export; specification and result hashes  |
| Assembly / system verification | Only implemented, capability-checked adapters with qualified models; all required excitations, convergence and tolerance evidence |
| Fabrication handoff            | `yapnr fab build/preview`; ordering is `yapnr order stage --dry-run` only                                                         |

The local campaign backend is a bounded process pool inside the container; it
uses the packaged Python and headless KiCad, without a nested Docker daemon.
Current PnR task kinds execute a **frozen engine source bundle**: retrieve the
matching released source archive and SHA256SUMS from GitHub Releases, verify the
archive, and declare that snapshot as a campaign input. The release archive is a
packaged dependency artifact, not a source overlay for the installed controller
or RF modules. The current `ladder-cell` adapter requires a committed source
checkout; use the documented `mc-eval` snapshot/stage-plan interface for an external
article. Record the archive hash and release engine revision. Do not invent an
installed generic board-routing CLI while that migration remains incomplete.

Use the campaign configuration and task kinds in
[cloud experiments](cloud-experiments.md); do not invent a generic `run`,
`design-study` or `vnv` command. Local execution does not authorize cloud spend.
Run every subprocess with a timeout and retain failures. Interactive agent chat
is an operator-controlled terminal process, not an unattended solver worker.
Firmware/FPGA/bench targets are enabled only for requirements needing them.

For RF APIs, see [inverse design](rf-inverse-design.md),
[PALACE](rf-palace.md) and [solver runtimes](rf-solver-backends.md).
External solver images must be separately pinned; the application image does not
claim to include every solver. Never generate project-specific connector/model
scripts to conceal a missing reusable assembly adapter.

## 28 GHz power-divider coverage

The [downstream divider](https://github.com/Studio-Fug/28ghz-2way-power-divider)
requires 24–32 GHz three-port results at the actual coaxial mating planes. Installed
RF design/export/native DUT revalidation is packaging-tested on both architectures
([#96](https://github.com/Studio-Fug/yapnr/issues/96)). That smoke test is not RF
performance qualification. Revision A's RF acceptance failures remain failures.

Complete populated-board simulation remains
[#97](https://github.com/Studio-Fug/yapnr/issues/97): shared assembly geometry,
floating copper, NPTH/PTH, finite substrate, materials/losses, qualified launch and
packaged-resistor models, contacts/reference planes, adapters and numerical evidence.
The agent must report those gates as blocked until supported and evidenced. Clean
DRC, DUT-only Touchstone or successfully generated solver input does not close
assembly acceptance. A physical bench requirement remains open until measurement.

On inability to progress, retain the best artifact, failed/open requirements,
fixup report, attempted engine repairs and exact resume conditions. Emit `<DONE>`
only after final verification of every accepted requirement on one exact revision.
