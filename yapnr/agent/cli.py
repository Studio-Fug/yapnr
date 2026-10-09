"""Bootstrap an article without overwriting it; launch the operator's agent chat."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def instructions(guide=False):
    name = "workflow.md" if guide else "AGENTS.md"
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def initialize(project, directive=""):
    root = Path(project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    from yapnr.agent.workspace import exclude_runtime_snapshots

    exclude_runtime_snapshots(root)
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
    if not (root / "requirements/manifest.md").exists() and not list(
        (root / "requirements").glob("*.y*ml")
    ):
        templates["requirements/model.yaml"] = (
            "project:\n  name: New design\nuser_needs: []\nrequirements: []\n"
            "risks: []\nmitigations: []\ntest_methods: []\n"
        )
    for name, content in templates.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as f:
                f.write(content)
            created.append(name)
        except FileExistsError:
            pass
    (root / "requirements/evidence").mkdir(parents=True, exist_ok=True)
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


def chat_command(provider, executable, context, directive, model=""):
    prompt = (
        "Read the project's AGENTS.md and " + str(context) + ". "
        "Follow the persistent prompt-to-PCB engineering workflow using the installed yapnr. "
        "Reconcile existing checkpoints/jobs first. Preserve accepted requirements and evidence. "
        "Write and present the requirements specification and risk analysis for review before "
        "design. While review is pending, continue authorized reversible work speculatively, "
        "labeling its input revision; silence is not approval. Incorporate refinements and "
        "restart affected stages when requirements change. Deliver real visual milestones "
        "inline in this chat and offer visual alternatives for relevant requirements choices. "
        "Run yapnr workflow query before actions and use yapnr workflow next with real "
        "evidence receipts for transitions; never fabricate a user acceptance receipt. "
        "Begin requirements capture for the following request, or ask for the design directive "
        "if none is supplied. Do not treat missing verification as success.\n" + directive
    )
    command = [executable, "--prompt", prompt] if provider == "opencode" else [executable, prompt]
    if model:
        command.extend(["--model", model])
    return command


def opencode_environment():
    """Read bundled dependency packages without asking for external-directory access."""
    from importlib.util import find_spec

    environment = os.environ.copy()
    config = json.loads(environment.get("OPENCODE_CONFIG_CONTENT", "{}"))
    for name in ("yapnr", "rules_requirements"):
        spec = find_spec(name)
        if not spec or not spec.origin or "site-packages" not in Path(spec.origin).parts:
            continue  # A source checkout is governed by its project permissions.
        pattern = str(Path(spec.origin).parent) + "/*"
        permission = config.setdefault("permission", {})
        for key, value in (("external_directory", "allow"), ("edit", "deny")):
            current = permission.get(key, {})
            if isinstance(current, str):
                current = {"*": current}
            permission[key] = {**current, pattern: value}
    cache = Path(environment.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "yapnr/atopile"
    pattern = str(cache) + "/*/venv/lib/python*/site-packages/*"
    permission = config.setdefault("permission", {})
    for key, value in (("external_directory", "allow"), ("edit", "deny")):
        current = permission.get(key, {})
        if isinstance(current, str):
            current = {"*": current}
        permission[key] = {**current, pattern: value}
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return environment


def provider_environment(args):
    """Configure a compatible API using an environment reference, never a stored key."""
    endpoint = getattr(args, "base_url", "")
    model = getattr(args, "model", "")
    if not endpoint:
        return (opencode_environment() if args.provider == "opencode" else None), model
    if args.provider != "opencode":
        raise ValueError("--base-url requires --provider opencode")
    url = urlsplit(endpoint)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Endpoint must be an HTTP(S) base URL without credentials, query or fragment"
        )
    key_env = getattr(args, "api_key_env", "YAPNR_MODEL_API_KEY")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("--api-key-env must be an environment variable name")
    if not model:
        raise ValueError("Custom endpoints require --model with the server's model ID")
    config = {
        "provider": {
            "yapnr_endpoint": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "User endpoint",
                "options": {"baseURL": endpoint, "apiKey": "{env:" + key_env + "}"},
                "models": {model: {"name": model}},
            }
        }
    }
    environment = opencode_environment()
    existing = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    existing.setdefault("provider", {}).update(config["provider"])
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(existing)
    return environment, "yapnr_endpoint/" + model


def run(args):
    try:
        if args.action == "instructions":
            print(instructions(args.guide), end="")
            return 0
        if args.action == "init":
            print(json.dumps(initialize(args.project, args.directive), indent=2))
            return 0
        environment, model = provider_environment(args)
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
        from yapnr.agent.workflow import init as initialize_workflow

        initialize_workflow(state["project"], args.directive)
        os.chdir(state["project"])
        # The interactive operator owns this process, rather than a background solver worker.
        # exec preserves terminal control, exit codes and container PID-1 signal forwarding.
        command = chat_command(args.provider, executable, state["context"], args.directive, model)
        if environment is None:
            os.execv(executable, command)
        else:
            os.execve(executable, command, environment)
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
            child.add_argument(
                "--provider", choices=("codex", "claude", "opencode"), default="codex"
            )
            child.add_argument("--model", default="", help="model ID; OpenCode uses provider/model")
            child.add_argument(
                "--base-url", default="", help="OpenAI-compatible API base URL (OpenCode)"
            )
            child.add_argument(
                "--api-key-env",
                default="YAPNR_MODEL_API_KEY",
                help="environment variable containing endpoint key",
            )
            child.add_argument("--dry-run", action="store_true")
        child.set_defaults(func=run)
