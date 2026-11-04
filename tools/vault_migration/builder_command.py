"""The `vault-builder` command file that scripts/install.sh writes (spec 4.3, decision 10).

The file is $HOME/.local/bin/vault-builder. It is a short shell script with three fixed lines after the
shebang: the marker line, the recorded builder checkout, and the exec line that runs that checkout's
tools/vault_migration/cli.py. scripts/install.sh must write exactly the text command_text returns.

- The marker line says vault-builder wrote the file. install replaces and uninstall deletes only a
  file that has it. A file without it belongs to the user and is never changed.
- The checkout line is `VAULT_BUILDER_CHECKOUT=<shell-quoted absolute path>`. doctor reads it back
  with read_checkout to check that the command points at a builder checkout. uninstall --all reads it
  to find the checkout it may delete: only default_checkout, never one the user chose with
  VAULT_BUILDER_HOME.

This module only builds and reads text. It writes no file.
"""

from __future__ import annotations

import shlex
from pathlib import Path

MARKER = "# vault-builder command file: written by vault-builder scripts/install.sh; uninstall deletes it."
CHECKOUT_VARIABLE = "VAULT_BUILDER_CHECKOUT"
CLI_RELATIVE = Path("tools/vault_migration/cli.py")
INSTALL_COMMAND = "curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash"


def command_path(home: Path) -> Path:
    return home / ".local" / "bin" / "vault-builder"


def default_checkout(home: Path) -> Path:
    """The builder checkout scripts/install.sh clones when VAULT_BUILDER_HOME is not set (spec 3)."""
    return home / ".local" / "share" / "vault-builder"


def command_text(checkout: Path, python: str) -> str:
    """The whole file for a checkout and the Python that runs its cli.py."""
    return (
        "#!/bin/sh\n"
        f"{MARKER}\n"
        f"{CHECKOUT_VARIABLE}={shlex.quote(str(checkout))}\n"
        f'exec {shlex.quote(python)} "${CHECKOUT_VARIABLE}/{CLI_RELATIVE}" "$@"\n'
    )


def has_marker(contents: bytes) -> bool:
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeError:
        return False
    return MARKER in lines


def read_checkout(contents: bytes) -> Path | None:
    """The recorded checkout of a marked file, or None when the file has no marker or no single checkout line."""
    if not has_marker(contents):
        return None
    prefix = f"{CHECKOUT_VARIABLE}="
    lines = [line for line in contents.decode("utf-8").splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        return None
    try:
        words = shlex.split(lines[0][len(prefix):])
    except ValueError:
        return None
    if len(words) != 1 or not words[0] or not Path(words[0]).is_absolute():
        return None
    return Path(words[0])
