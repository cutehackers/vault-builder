#!/bin/sh
# recall.sh [open|decisions|corrections]
#
# Print the linked wiki vaults' project summary sections for the git repository in $PWD.
# Builder-only asset: install copies it to <config dir>/skills/project-memory/scripts/recall.sh,
# where <config dir> is CLAUDE_CONFIG_DIR, else $HOME/.claude.
#
# Interpreter placeholder: the value on the PYTHON= line below is the name VAULT_BUILDER_PYTHON
# between two @ signs. That token appears nowhere else in this file. Install replaces it with
# shlex.quote(<absolute path of a Python >= 3.10>), so installed bytes differ per machine.
#
# Limits and headings must match tools/wiki/lib/maps.py: PROJECT_SECTION_MAX_TOKENS,
# PROJECT_MAP_MAX_TOKENS, PROJECT_SECTION_HEADINGS, and estimate_tokens.
PYTHON=@VAULT_BUILDER_PYTHON@
# -I keeps the user's repository (the cwd) off sys.path.
exec "$PYTHON" -I - "$@" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

SECTION_MAX_TOKENS = 750
ALL_MAX_TOKENS = 1500
SECTION_HEADINGS = {
    "open": "## Open Work",
    "decisions": "## Current Decisions",
    "corrections": "## Corrections and Rules",
}
USAGE = "usage: recall.sh [open|decisions|corrections]"
PREFIX = "project-memory: "


def estimate_tokens(text):
    """Hangul characters / 2 + all other characters / 4, rounded up. Same rule as maps.py."""
    hangul = sum(1 for char in text if _is_hangul(char))
    other = len(text) - hangul
    return (2 * hangul + other + 3) // 4


def _is_hangul(char):
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7A3
        or 0xD7B0 <= code <= 0xD7FF
    )


class Stop(Exception):
    """A one-line reason why nothing can be printed."""


def project_root(cwd):
    def git(*arguments):
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise Stop(f"{cwd} is not inside a git repository; nothing to recall.")
        return result.stdout.strip()

    common_dir = Path(git("--path-format=absolute", "--git-common-dir"))
    if common_dir.name == ".git":
        return common_dir.parent
    return Path(git("--show-toplevel"))


def config_dir():
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("HOME") or Path.home()) / ".claude"


def linked_vaults(map_path, project_id):
    if not map_path.is_file():
        raise Stop(f"No link config at {map_path}; link a project there first (see docs/usage.md).")
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stop(f"Link config {map_path} is damaged: {exc}".replace("\n", " "))
    damaged = f"Link config {map_path} is damaged"
    if not isinstance(data, dict):
        raise Stop(f"{damaged}: it must be a JSON object.")
    vaults = data.get("vaults")
    projects = data.get("projects")
    if not isinstance(vaults, dict) or not isinstance(projects, dict):
        raise Stop(f"{damaged}: vaults and projects must be objects.")
    if project_id not in projects:
        raise Stop(
            f"Project {project_id} is not linked to a vault; add it under projects in {map_path} (see docs/usage.md)."
        )
    entry = projects[project_id]
    names = entry.get("vaults") if isinstance(entry, dict) else None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise Stop(f"{damaged}: project {project_id} needs a vaults list.")
    linked = []
    for name in names:
        vault = vaults.get(name)
        if not isinstance(vault, dict) or not isinstance(vault.get("path"), str):
            raise Stop(f"{damaged}: project {project_id} links vault {name}, which has no path.")
        linked.append((name, Path(vault["path"]).expanduser()))
    if not linked:
        raise Stop(f"Project {project_id} has no vaults in {map_path} (see docs/usage.md).")
    return linked


def page_body(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def read_sections(text, wanted):
    """Return {key: lines} for each wanted section found, heading included, trailing blank lines removed."""
    lines = page_body(text).splitlines()
    found = {}
    for key in wanted:
        heading = SECTION_HEADINGS[key]
        if heading not in lines:
            continue
        start = lines.index(heading)
        end = start + 1
        while end < len(lines) and not lines[end].startswith(("# ", "## ")):
            end += 1
        block = lines[start:end]
        while block and not block[-1].strip():
            block.pop()
        found[key] = block
    return found


def vault_block(name, vault_path, project_id, wanted):
    """Return (header, sections, notes) for one vault, or (None, None, notes) when it is skipped."""
    page = vault_path / "wiki" / "maps" / "projects" / f"{project_id}.md"
    if not vault_path.is_dir():
        return None, None, [f"{PREFIX}skipped vault {name}: {vault_path} does not exist."]
    try:
        text = page.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None, [f"{PREFIX}skipped vault {name}: no summary page {page}; run maps build there."]
    sections = read_sections(text, wanted)
    notes = [
        f"{PREFIX}vault {name}: section {SECTION_HEADINGS[key]} is missing from {page}."
        for key in wanted
        if key not in sections
    ]
    if not sections:
        return None, None, notes
    return f"# vault {name}: {page}", [sections[key] for key in wanted if key in sections], notes


def render_block(header, sections):
    lines = [header]
    for block in sections:
        lines.append("")
        lines.extend(block)
    return "\n".join(lines) + "\n"


def fit_block(header, sections, budget):
    """Drop item lines from the end of the largest section until the block fits; keep headings."""
    sections = [list(block) for block in sections]
    while estimate_tokens(render_block(header, sections)) > budget:
        candidates = [index for index, block in enumerate(sections) if len(block) > 1]
        if not candidates:
            break
        index = max(candidates, key=lambda item: (estimate_tokens("\n".join(sections[item])), -item))
        sections[index].pop()
    text = render_block(header, sections)
    if estimate_tokens(text) <= budget:
        return text
    # Headings alone do not fit: keep whole lines from the start while they fit.
    kept = ""
    for line in text.splitlines(keepends=True):
        if estimate_tokens(kept + line) > budget:
            break
        kept += line
    return kept


def recall(arguments):
    if len(arguments) > 1 or (arguments and arguments[0] not in SECTION_HEADINGS and arguments[0] != ""):
        print(USAGE, file=sys.stderr)
        return 2
    wanted = [arguments[0]] if arguments and arguments[0] else list(SECTION_HEADINGS)
    limit = SECTION_MAX_TOKENS if len(wanted) == 1 else ALL_MAX_TOKENS
    try:
        root = project_root(os.getcwd())
        project_id = root.name
        vaults = linked_vaults(config_dir() / "vault-builder" / "vault-map.json", project_id)
    except Stop as stop:
        print(PREFIX + str(stop))
        return 0

    notes = []
    blocks = []
    for name, vault_path in vaults:
        header, sections, block_notes = vault_block(name, vault_path, project_id, wanted)
        notes.extend(block_notes)
        if header is not None:
            blocks.append((header, sections))

    note_text = "".join(line + "\n" for line in notes)
    full = note_text + "".join(render_block(header, sections) for header, sections in blocks)
    if estimate_tokens(full) <= limit:
        sys.stdout.write(full)
        return 0

    truncated = (
        f"{PREFIX}output truncated to {limit} estimated tokens across {len(blocks)} vaults; "
        "open the summary pages above for the rest.\n"
    )
    # Estimates of joined texts never exceed the sum of their parts, so per-part budgets bound the total.
    budget = (limit - estimate_tokens(note_text) - estimate_tokens(truncated)) // max(len(blocks), 1)
    output = note_text + "".join(fit_block(header, sections, budget) for header, sections in blocks)
    sys.stdout.write(output + truncated)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(recall(sys.argv[1:]))
PY
