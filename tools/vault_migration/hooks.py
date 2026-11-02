"""User-scope Claude Code hook entries for the vault_hooks.py launcher (contract 6).

install --with-hooks adds one entry to each of hooks.SessionStart, hooks.PreCompact and hooks.SessionEnd
in <config dir>/settings.json. Each entry runs `<python> -I <config dir>/vault-builder/vault_hooks.py
<subcommand>`. The capture entries (PreCompact, SessionEnd) have a 60 second timeout. uninstall removes
only hook items whose command contains the launcher path. Every other key and hook entry is kept.

This module only builds and reads values. installing.py and uninstalling.py do the writes, with backups.
It does not import map_settings.py, and map_settings.py does not import this module (spec 4.4).
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path

LAUNCHER_ASSET = Path("tools/vault_migration/assets/vault-hooks/vault_hooks.py")
LAUNCHER_NAME = "vault_hooks.py"
HOOKS_CONFIG_NAME = "vault-hooks.json"
SETTINGS_NAME = "settings.json"
HOOKS_CONFIG_VERSION = 1
CAPTURE_TIMEOUT_S = 60

# (event, launcher subcommand, timeout in seconds or None)
EVENTS: tuple[tuple[str, str, int | None], ...] = (
    ("SessionStart", "session-start", None),
    ("PreCompact", "capture", CAPTURE_TIMEOUT_S),
    ("SessionEnd", "capture", CAPTURE_TIMEOUT_S),
)


class HooksError(ValueError):
    """settings.json has a form this module will not change; the message says what to fix."""


def settings_path(config_dir: Path) -> Path:
    return config_dir / SETTINGS_NAME


def launcher_path(config_dir: Path) -> Path:
    return config_dir / "vault-builder" / LAUNCHER_NAME


def hooks_config_path(config_dir: Path) -> Path:
    return config_dir / "vault-builder" / HOOKS_CONFIG_NAME


def hook_command(python: str, launcher: Path, subcommand: str) -> str:
    return shlex.join([python, "-I", str(launcher), subcommand])


def launcher_group(python: str, launcher: Path, subcommand: str, timeout: int | None) -> dict[str, object]:
    item: dict[str, object] = {"type": "command", "command": hook_command(python, launcher, subcommand)}
    if timeout is not None:
        item["timeout"] = timeout
    return {"hooks": [item]}


def load_settings(path: Path) -> dict[str, object]:
    """Read settings.json. Absent means {}. Raise HooksError when it is not a JSON object with a hooks object."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HooksError(f"Claude Code settings file {path} is not valid JSON: {exc}.") from exc
    if not isinstance(value, dict):
        raise HooksError(f"Claude Code settings file {path} must contain a JSON object.")
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HooksError(f"Claude Code settings file {path}: hooks must be a JSON object.")
    for event, _subcommand, _timeout in EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise HooksError(f"Claude Code settings file {path}: hooks.{event} must be a JSON array.")
    return value


def settings_bytes(settings: Mapping[str, object]) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _is_launcher_item(item: object, launcher: Path) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and str(launcher) in item["command"]
    )


def _strip_launcher_items(groups: list[object], launcher: Path) -> tuple[list[object], int]:
    """Remove launcher items from each group; drop a group only when the removal empties it."""
    kept: list[object] = []
    removed = 0
    for group in groups:
        items = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(items, list):
            kept.append(group)
            continue
        remaining = [item for item in items if not _is_launcher_item(item, launcher)]
        removed += len(items) - len(remaining)
        if len(remaining) == len(items):
            kept.append(group)
        elif remaining:
            kept.append({**group, "hooks": remaining})
    return kept, removed


def add_launcher_hooks(
    settings: Mapping[str, object], python: str, launcher: Path
) -> tuple[dict[str, object], bool]:
    """Return (new settings, changed). An event that already has exactly the wanted entry is not touched."""
    result = copy.deepcopy(dict(settings))
    hooks = result.setdefault("hooks", {})
    assert isinstance(hooks, dict)
    changed = False
    for event, subcommand, timeout in EVENTS:
        groups = list(hooks.get(event, []))
        wanted = launcher_group(python, launcher, subcommand, timeout)
        stripped, removed = _strip_launcher_items(groups, launcher)
        if removed == 1 and wanted in groups:
            continue
        hooks[event] = [*stripped, wanted]
        changed = True
    return result, changed


def remove_launcher_hooks(settings: Mapping[str, object], launcher: Path) -> tuple[dict[str, object], int]:
    """Return (new settings, number of removed hook items). An event list emptied by the removal is dropped."""
    result = copy.deepcopy(dict(settings))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    total = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        stripped, removed = _strip_launcher_items(groups, launcher)
        if not removed:
            continue
        total += removed
        if stripped:
            hooks[event] = stripped
        else:
            del hooks[event]
    return result, total


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_vg(environ: Mapping[str, str]) -> Path | None:
    """The vg to record: an executable VAULT_GRAPH_BIN, else vg on PATH. Always absolute."""
    configured = environ.get("VAULT_GRAPH_BIN", "")
    if configured:
        candidate = Path(os.path.expanduser(configured))
        if candidate.is_absolute() and _executable(candidate):
            return candidate
    found = shutil.which("vg", path=environ.get("PATH", ""))
    return Path(os.path.abspath(found)) if found else None


def hooks_config_bytes(vg: Path) -> bytes:
    return settings_bytes({"version": HOOKS_CONFIG_VERSION, "vg": str(vg)})


def is_hooks_config(contents: bytes) -> bool:
    """True when the file has the form install writes, so uninstall may delete it."""
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and set(value) <= {"version", "vg"}
