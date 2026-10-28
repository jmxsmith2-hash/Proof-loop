"""Add a site to the allowlist in config/settings.yaml, keeping the file's comments.

Only ever called when the user explicitly asks (`synthux test URL --allow`,
`synthux init`). New entries are not sandboxes: payment, email and delete
actions stay blocked on them.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlsplit

from synthux.core.config import ConfigError, load_yaml_file

DEFAULT_PORTS = {"http": 80, "https": 443}


def host_entry(url: str) -> tuple[str, list[int]]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{url!r} is not an http(s) address")
    if parts.username or parts.password:
        raise ConfigError("addresses with a user name or password in them are not accepted")
    port = parts.port
    return parts.hostname.lower(), [port] if port and port != DEFAULT_PORTS[parts.scheme] else []


def add_to_allowlist(config_dir: Path, url: str, note: str = "added on request") -> tuple[Path, bool]:
    """Returns (settings file, whether an entry was added). Creates settings.yaml from the example if needed."""
    config_dir = Path(config_dir)
    settings = config_dir / "settings.yaml"
    if not settings.exists():
        example = config_dir / "settings.example.yaml"
        if not example.exists():
            raise ConfigError(f"no {settings} or {example} to add the site to")
        shutil.copyfile(example, settings)
    host, ports = host_entry(url)
    current = (load_yaml_file(settings) or {}).get("safety", {}).get("allowlist", []) or []
    for entry in current:
        if str(entry.get("host", "")).lower() == host and (not entry.get("ports") or not ports
                                                             or set(ports) <= set(entry.get("ports"))):
            return settings, False
    lines = settings.read_text(encoding="utf-8").splitlines()
    block = [f"    - host: {host}"] + ([f"      ports: {ports}"] if ports else []) + \
        ["      sandbox: false           # real site: payments, emails and deletes stay blocked",
         f"      note: {note}"]
    for i, line in enumerate(lines):
        if line.strip() == "allowlist:":
            lines[i + 1:i + 1] = block
            break
        if line.strip() in ("allowlist: []", "allowlist: [ ]"):
            lines[i] = line.replace("[]", "").replace("[ ]", "").rstrip()
            lines[i + 1:i + 1] = block
            break
    else:
        if not any(line.startswith("safety:") for line in lines):
            lines += ["", "safety:", "  allowlist:"] + block
        else:
            idx = next(i for i, line in enumerate(lines) if line.startswith("safety:"))
            lines[idx + 1:idx + 1] = ["  allowlist:"] + block
    settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return settings, True
