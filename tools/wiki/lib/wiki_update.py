from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any


def apply_index_entry(index_path: Path, index_entry: Any) -> None:
    proposed_text = render_index_entry_update(index_path, index_entry)
    if proposed_text is None:
        return
    _write_text_preserving_human_locks(index_path, proposed_text)


def assert_can_apply_index_entry(index_path: Path, index_entry: Any) -> None:
    proposed_text = render_index_entry_update(index_path, index_entry)
    if proposed_text is not None:
        _assert_human_locks_preserved(index_path, proposed_text)


def render_index_entry_update(index_path: Path, index_entry: Any) -> str | None:
    if not index_entry:
        return None
    section = str(index_entry["section"])
    target = str(index_entry["target"])
    summary = str(index_entry["summary"])
    entry = f"- [[{target}]] - {summary}"
    text = index_path.read_text()
    if f"[[{target}]]" in text:
        return None

    section_heading = f"## {section}"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line != section_heading:
            continue
        insert_at = len(lines)
        for next_index in range(index + 1, len(lines)):
            if lines[next_index].startswith("## "):
                insert_at = next_index
                break
        while insert_at > index + 1 and lines[insert_at - 1] == "":
            insert_at -= 1
        lines.insert(insert_at, entry)
        return "\n".join(lines) + "\n"

    if not text.endswith("\n"):
        text += "\n"
    return f"{text}\n{section_heading}\n\n{entry}\n"


def append_log_entry(log_path: Path, log_entry: Any) -> None:
    proposed_text = render_log_entry_update(log_path, log_entry)
    if proposed_text is None:
        return
    _write_text_preserving_human_locks(log_path, proposed_text)


def assert_can_append_log_entry(log_path: Path, log_entry: Any) -> None:
    proposed_text = render_log_entry_update(log_path, log_entry)
    if proposed_text is not None:
        _assert_human_locks_preserved(log_path, proposed_text)


def render_log_entry_update(log_path: Path, log_entry: Any) -> str | None:
    if not log_entry:
        return None
    event_type = str(log_entry["event_type"])
    title = str(log_entry["title"])
    items = log_entry.get("items") or []
    lines = ["", f"## [{date.today().isoformat()}] {event_type} | {title}", ""]
    for item in items:
        lines.append(f"- {item}")
    if not items:
        lines.append("- Applied update.")
    lines.append("")
    return log_path.read_text().rstrip() + "\n" + "\n".join(lines)


def _write_text_preserving_human_locks(path: Path, proposed_text: str) -> None:
    _assert_human_locks_preserved(path, proposed_text)
    path.write_text(proposed_text)


def _assert_human_locks_preserved(path: Path, proposed_text: str) -> None:
    if not path.exists():
        return
    existing_text = path.read_text()
    if "<!-- human-locked -->" in existing_text:
        raise ValueError(f"human-locked page blocks update: {path}")
    if _locked_section_blocks(existing_text) != _locked_section_blocks(proposed_text):
        raise ValueError(f"human-locked section blocks update: {path}")


def _locked_section_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    sections: list[str] = []
    stack: list[int] = []
    for index, line in enumerate(lines):
        if "<!-- human-locked:start -->" in line:
            stack.append(index)
        if "<!-- human-locked:end -->" in line and stack:
            start = stack.pop()
            sections.append("\n".join(lines[start : index + 1]))
    return sections
