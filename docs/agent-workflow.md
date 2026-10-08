# Design a PCB with an agent

Give your agent the directive “Please use Studio-Fug/yapnr to design X”. Its
engineering procedure is [AGENTS.md](../AGENTS.md), also shipped in the installed
wheel. The agent owns requirements capture, risk analysis, schematic/part selection,
bounded placement/routing experiments and verification of the delivered artifact.
An unavailable validator or vendor model remains an explicit blocker.

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
follow the CLI's device-login or browser callback instructions. Claude Code is
operator-installed and is not redistributed in the image. Persist its installation
and authentication in the separate home volume. See the official
[Codex authentication](https://developers.openai.com/codex/auth/)
and [Claude Code setup](https://code.claude.com/docs/en/setup) instructions.

For **OpenAI, Anthropic, Google Gemini and other provider API keys**, select
`agent chat --provider opencode`. Use OpenCode's `/connect` to supply your own
key and `/models` to select a provider/model. You can also select an already
configured model with `--model provider/model`. No yapnr-managed model account
is required. See [OpenCode providers](https://opencode.ai/docs/providers/).

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
yapnr agent init --project article --directive 'Design X'
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
