"""Release version of this builder checkout, read from the VERSION file at the repository root."""
from __future__ import annotations

import re
from pathlib import Path

from tools.wiki.lib.vault_manifest import MANIFEST_CONTRACT_VERSION

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = "VERSION"
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def read_version(root: Path = ROOT) -> str:
    """Return the X.Y.Z release version; the file holds exactly one such line."""
    path = root / VERSION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Missing {VERSION_FILE} file in {root}.") from exc
    version = text.strip()
    if text != version + "\n" or not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"{path} must hold one X.Y.Z version line.")
    return version


def version_label(root: Path = ROOT) -> str:
    """Release version with the contract version, for command output."""
    return f"vault-builder {read_version(root)} (contract {MANIFEST_CONTRACT_VERSION})"
