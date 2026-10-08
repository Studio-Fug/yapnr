"""Bootstrap an article without overwriting it; launch the operator's agent chat."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def instructions(guide=False):
    name = "workflow.md" if guide else "AGENTS.md"
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def initialize(project, directive=""):
    root = Path(project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    installed = root / ".yapnr/agent"
    installed.mkdir(parents=True, exist_ok=True)
    guide = instructions()
    # Immutable copy keyed by content: resumed projects retain earlier engine guidance.
    digest = hashlib.sha256(guide.encode()).hexdigest()
    context = installed / ("instructions-" + digest[:16] + ".md")
    context.write_text(guide, encoding="utf-8")
    created = []
    templates = {
        "AGENTS.md": guide,
        "CLAUDE.md": "Read @AGENTS.md and follow the article's engineering workflow.\n",
        "requirements/manifest.md": (
            "# Accepted requirements\n\nRecord request/source, stable IDs, measurable "
            "thresholds, verification methods, demanded evidence, assumptions and approval history. "
            "No requirements are approved by this template.\n"
        ),
        "requirements/risks.md": (
            "# Risks and mitigations\n\nRecord failure, consequence, mitigation, "
            "implementing requirement IDs and residual-risk decision.\n"
        ),
        ".yapnr/agent/checkpoint.json": json.dumps(
            {
                "schema": "yapnr-agent-workflow-v1",
                "status": "blocked",
                "stage": "requirements_capture",
                "requested_feature": directive,
                "requirements_approved": False,
                "budgets": {},
                "resume_conditions": [
                    "Capture and establish the engineering contract and resource limits"
                ],
                "instructions_sha256": digest,
            },
            indent=2,
        )
        + "\n",
    }
    for name, content in templates.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as f:
                f.write(content)
            created.append(name)
        except FileExistsError:
            pass
    for folder in ("design", "constraints", "experiments", "reports", "evidence"):
        (root / folder).mkdir(exist_ok=True)
    with (installed / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "event": "initialized",
                    "created": created,
                    "instructions_sha256": digest,
                }
            )
            + "\n"
        )
    return {
        "project": str(root),
        "context": str(context),
        "created": created,
        "instructions_sha256": digest,
    }


def chat_command(provider, executable, context, directive):
    prompt = (
        "Read the project's AGENTS.md and " + str(context) + ". "
        "Follow the persistent prompt-to-PCB engineering workflow using the installed yapnr. "
        "Reconcile existing checkpoints/jobs first. Preserve accepted requirements and evidence. "
        "Begin requirements capture for the following request, or ask for the design directive "
        "if none is supplied. Do not treat missing verification as success.\n" + directive
    )
    # Both CLIs accept one positional initial prompt. No permission-bypass flags.
    return [executable, prompt]


def run(args):
    try:
        if args.action == "instructions":
            print(instructions(args.guide), end="")
            return 0
        if args.action == "init":
            print(json.dumps(initialize(args.project, args.directive), indent=2))
            return 0
        executable = shutil.which(args.provider)
        if not executable:
            print(
                f"{args.provider} CLI is not installed. Use the yapnr container (bundled Codex), "
                "or install/authenticate the selected provider CLI; see yapnr agent instructions --guide.",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            # Planning does not create directories or expose auth/provider environment values.
            print(
                json.dumps(
                    {
                        "provider": args.provider,
                        "executable": executable,
                        "project": str(Path(args.project).resolve()),
                        "instructions_sha256": hashlib.sha256(instructions().encode()).hexdigest(),
                        "interactive": True,
                        "launches_agent": False,
                    },
                    indent=2,
                )
            )
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("agent chat needs an interactive terminal (docker run -it).", file=sys.stderr)
            return 2
        state = initialize(args.project, args.directive)
        os.chdir(state["project"])
        # The interactive operator owns this process, rather than a background solver worker.
        # exec preserves terminal control, exit codes and container PID-1 signal forwarding.
        os.execv(
            executable, chat_command(args.provider, executable, state["context"], args.directive)
        )
    except (OSError, ValueError) as err:
        print(str(err), file=sys.stderr)
        return 2
    return 0


def register(commands):
    parser = commands.add_parser(
        "agent", help="engineering guidance, article bootstrap and agent chat"
    )
    subs = parser.add_subparsers(dest="action", required=True)
    guide = subs.add_parser("instructions", help="print installed AGENTS.md or execution guide")
    guide.add_argument("--guide", action="store_true")
    guide.set_defaults(func=run)
    for name in ("init", "chat"):
        child = subs.add_parser(name)
        child.add_argument("--project", default=".")
        child.add_argument("--directive", default="")
        if name == "chat":
            child.add_argument("--provider", choices=("codex", "claude"), default="codex")
            child.add_argument("--dry-run", action="store_true")
        child.set_defaults(func=run)
