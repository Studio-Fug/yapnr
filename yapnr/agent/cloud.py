"""Project cloud settings use the experiment engine's actual configuration schema."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from yapnr.agent import workspace
from yapnr.exp import config

RELATIVE = ".yapnr/workspace/cloud.toml"


def read(root):
    path = Path(root) / RELATIVE
    if not path.is_file():
        path = config.config_path()
    text = path.read_text() if path.is_file() else ""
    data = tomllib.loads(text)
    config.parse(data)
    # Credential lookup commands belong to the operator, not an exported article.
    if "prices" in data:
        data["prices"] = {k: v for k, v in data["prices"].items() if k != "price_api_key_command"}
    return {
        "settings": data,
        "sha256": workspace.sha(text.encode()),
        "scope": "project" if path == Path(root) / RELATIVE else "operator",
        "providers": ["gcp-batch"],
        "credentials_in_workspace": False,
    }


def dumps(data):
    lines = []

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        raise ValueError("Unsupported cloud configuration value")

    def table(data, prefix):
        if prefix:
            lines.extend(["", "[" + ".".join(json.dumps(k) for k in prefix) + "]"])
        for key, item in data.items():
            if not isinstance(item, dict):
                lines.append(json.dumps(key) + " = " + value(item))
        for key, item in data.items():
            if isinstance(item, dict):
                table(item, prefix + [key])

    table(data, [])
    return "\n".join(lines).strip() + "\n"


def save(root, settings, expected):
    if settings.get("prices", {}).get("price_api_key_command"):
        raise ValueError("Credential lookup commands must remain in the operator configuration")
    config.parse(settings)
    with workspace.lock(root) as folder:
        if read(root)["sha256"] != expected:
            raise ValueError("Cloud settings changed; reload before saving")
        workspace.atomic(folder / "cloud.toml", dumps(settings).encode())
    item = workspace.publish(root, RELATIVE, "source", "Project cloud execution settings")
    workspace.record(root, {"source": "user-cloud-settings", "artifact": item["id"]})
    return read(root)


def sdk(root):
    settings = read(root)["settings"].get("gcp", {})
    executable = settings.get("gcloud", "gcloud")
    executable = shutil.which(os.path.expanduser(executable)) or (
        str(Path.home() / ".local/bin/gcloud")
        if executable == "gcloud" and (Path.home() / ".local/bin/gcloud").is_file()
        else None
    )
    if not executable:
        return {
            "available": False,
            "profiles": [],
            "accounts": [],
            "message": "Google Cloud SDK is unavailable on this server host.",
        }
    result = {
        "available": True,
        "profiles": [],
        "accounts": [],
        "message": "Credentials remain in the server host's Google Cloud SDK.",
    }
    try:
        for name, args in [
            ("profiles", ["config", "configurations", "list"]),
            ("accounts", ["auth", "list"]),
        ]:
            process = subprocess.run(
                [executable, *args, "--format=json", "--quiet"],
                capture_output=True,
                timeout=8,
                check=True,
            )
            if len(process.stdout) > 1024 * 1024:
                raise ValueError("SDK response exceeds the display limit")
            rows = json.loads(process.stdout)
            if name == "profiles":
                result[name] = [
                    {
                        "name": r["name"],
                        "active": r.get("is_active", False),
                        "properties": {
                            section: {
                                key: value
                                for key, value in r.get("properties", {}).get(section, {}).items()
                                if key in allowed
                            }
                            for section, allowed in [
                                ("core", {"project", "account"}),
                                ("compute", {"region", "zone"}),
                            ]
                        },
                    }
                    for r in rows
                ]
            else:
                result[name] = [
                    {"account": r.get("account"), "status": r.get("status")} for r in rows
                ]
    except (OSError, ValueError, subprocess.SubprocessError):
        result["message"] = (
            "Google Cloud SDK configuration could not be read; check host authentication."
        )
    return result
