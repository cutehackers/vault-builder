from __future__ import annotations

import re
from pathlib import Path


WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


def find_wiki_links(text: str) -> list[str]:
    links: list[str] = []
    for match in WIKI_LINK_PATTERN.finditer(text):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            links.append(target)
    return links


def resolve_wiki_link(target: str, wiki_root: Path) -> Path:
    normalized = target.strip().removeprefix("/")
    if normalized.endswith(".md"):
        return wiki_root / normalized
    return wiki_root / f"{normalized}.md"


def wiki_link_stays_in_root(target: str, wiki_root: Path) -> bool:
    resolved = resolve_wiki_link(target, wiki_root)
    try:
        resolved.resolve().relative_to(wiki_root.resolve())
    except ValueError:
        return False
    return True
