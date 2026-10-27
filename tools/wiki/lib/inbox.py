"""Read items from wiki/inbox.md.

An inbox item starts with a `## [YYYY-MM-DD] Short Title` heading and ends at the next level-two
heading. `Status: ...` and the optional `Project: <project id>` line are read from the item body.
Lines inside fenced code blocks are ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import parse_page
from .lint import fenced_line_flags


OPEN_INBOX_STATUSES = frozenset({"unprocessed", "deferred"})
_ITEM_HEADING = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] (.+?)\s*$")
_FIELD_LINE = re.compile(r"^(Status|Project):\s*(.*?)\s*$")


@dataclass(frozen=True)
class InboxItem:
    date: str
    title: str
    status: str
    project: str | None
    line: int

    @property
    def is_open(self) -> bool:
        """True for remaining work: Status is unprocessed or deferred."""
        return self.status.lower() in OPEN_INBOX_STATUSES


def parse_inbox_items(body: str) -> list[InboxItem]:
    lines = body.splitlines()
    fenced = fenced_line_flags(lines)
    items: list[InboxItem] = []
    current: dict[str, object] | None = None

    def finish() -> None:
        if current is not None:
            items.append(
                InboxItem(
                    date=str(current["date"]),
                    title=str(current["title"]),
                    status=str(current.get("Status", "")),
                    project=str(current.get("Project", "")) or None,
                    line=int(str(current["line"])),
                )
            )

    for index, line in enumerate(lines):
        if fenced[index]:
            continue
        if line.startswith("## "):
            finish()
            current = None
            heading = _ITEM_HEADING.match(line)
            if heading:
                current = {"date": heading.group(1), "title": heading.group(2), "line": index + 1}
            continue
        if current is None:
            continue
        field = _FIELD_LINE.match(line)
        if field and field.group(1) not in current:
            current[field.group(1)] = field.group(2)
    finish()
    return items


def load_inbox_items(root: Path) -> list[InboxItem]:
    inbox_path = root / "wiki" / "inbox.md"
    if not inbox_path.exists():
        return []
    return parse_inbox_items(parse_page(inbox_path).body)
