"""Read-only lookup over durable wiki pages.

Search ranks pages by how often the query terms appear in the title, summary,
tags and body. Matching is by substring so Korean words with particles
attached still match. Deprecated pages are left out unless asked for, and
superseded claim rows are left out so readers see current claims. Asking for
deprecated pages also brings superseded claims back, for questions about history.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import parse_page
from .lint import claim_evidence_table, fenced_line_flags

_TERM = re.compile(r"[^\W_]{2,}", re.UNICODE)
# Generated listings repeat every page summary and would outrank the pages themselves.
_SKIP_TYPES = {"log", "map", "index"}
# Korean particles a query word may carry that the page text does not.
_KOREAN_PARTICLES = ("에서", "으로", "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도")
_BODY_HITS_CAP = 10
_FIELD_WEIGHTS = {"title": 6, "summary": 3, "tags": 3, "body": 1}


@dataclass(frozen=True)
class SearchHit:
    path: str
    title: str
    type: str
    status: str
    updated: str
    summary: str
    score: int
    snippet: str


def search_wiki(
    root: Path,
    query: str,
    *,
    page_type: str | None = None,
    limit: int = 10,
    include_deprecated: bool = False,
) -> list[SearchHit]:
    if limit < 1:
        raise ValueError("Search limit must be 1 or more.")
    words = [word.lower() for word in _TERM.findall(query)]
    # Short Latin words such as "of" match almost everywhere, so they count only when nothing longer is given.
    terms = sorted({_strip_particle(word) for word in words if len(word) >= 3 or not word.isascii()})
    if not terms:
        terms = sorted(set(words))
    if not terms:
        raise ValueError("Search query needs at least one word of two or more characters.")
    hits: list[SearchHit] = []
    for path in sorted((root / "wiki").rglob("*.md")):
        try:
            page = parse_page(path)
        except (OSError, ValueError):
            continue
        frontmatter = page.frontmatter or {}
        kind = str(frontmatter.get("type", ""))
        status = str(frontmatter.get("status", ""))
        if kind in _SKIP_TYPES or (page_type and kind != page_type):
            continue
        if status == "deprecated" and not include_deprecated:
            continue
        current_body = _current_body(page.body, include_superseded=include_deprecated)
        score = _score(terms, frontmatter, current_body)
        if score == 0:
            continue
        hits.append(
            SearchHit(
                path=path.relative_to(root).as_posix(),
                title=str(frontmatter.get("title", path.stem)),
                type=kind,
                status=status,
                updated=str(frontmatter.get("updated", "")),
                summary=str(frontmatter.get("summary", "")),
                score=score,
                snippet=_snippet(terms, current_body),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return hits[:limit]


def read_wiki_page(root: Path, relative: str, section: str | None = None) -> str:
    wiki_root = (root / "wiki").resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(wiki_root)
    except ValueError:
        raise ValueError("Read path must stay under wiki/.") from None
    if path.suffix != ".md" or not path.is_file():
        raise ValueError(f"Wiki page does not exist: {relative}")
    text = path.read_text()
    if section is None:
        return text
    return _section_text(text, section)


def render_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "No matching wiki pages."
    lines = []
    for hit in hits:
        lines.append(f"- {hit.path} [{hit.type}, {hit.status}, updated {hit.updated}] score={hit.score}")
        lines.append(f"  {hit.title}: {hit.summary}")
        if hit.snippet:
            lines.append(f"  > {hit.snippet}")
    return "\n".join(lines)


def _strip_particle(word: str) -> str:
    if word.isascii():
        return word
    for particle in _KOREAN_PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def _score(terms: list[str], frontmatter: dict, body: str) -> int:
    fields = {
        "title": str(frontmatter.get("title", "")).lower(),
        "summary": str(frontmatter.get("summary", "")).lower(),
        "tags": " ".join(str(tag) for tag in frontmatter.get("tags", []) or []).lower(),
        "body": body.lower(),
    }
    score = 0
    for term in terms:
        for field, text in fields.items():
            count = text.count(term)
            if field == "body":
                count = min(count, _BODY_HITS_CAP)
            score += _FIELD_WEIGHTS[field] * count
    return score


def _current_body(body: str, include_superseded: bool = False) -> str:
    """Keep only the claim text of current claim rows.

    Superseded rows are dropped so readers see current claims. Status and evidence cells are
    dropped too, because words such as "stated" or "Human instruction" appear on every page.
    """
    table = claim_evidence_table(body)
    if table is None:
        return body
    rows = {row.line: row for row in table.rows}
    kept = []
    for number, line in enumerate(body.splitlines(), start=1):
        if table.start_line <= number <= table.end_line:
            row = rows.get(number)
            if row is not None and (include_superseded or row.status.strip().lower() != "superseded"):
                kept.append(row.claim)
            continue
        kept.append(line)
    return "\n".join(kept)


def _snippet(terms: list[str], body: str) -> str:
    for line in body.splitlines():
        lowered = line.lower()
        if line.strip() and not line.lstrip().startswith("#") and any(term in lowered for term in terms):
            snippet = line.strip()
            return snippet if len(snippet) <= 200 else snippet[:197] + "..."
    return ""


def _section_text(text: str, heading: str) -> str:
    lines = text.splitlines()
    fenced = fenced_line_flags(lines)
    match = re.match(r"^(#{1,6})\s+\S", heading.strip())
    if not match:
        raise ValueError(f"Section must be a markdown heading line such as '## Notes': {heading}")
    level = len(match.group(1))
    starts = [index for index, line in enumerate(lines) if not fenced[index] and line.strip() == heading.strip()]
    if not starts:
        raise ValueError(f"Section not found: {heading}")
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        other = re.match(r"^(#{1,6})\s+\S", lines[index])
        if other and not fenced[index] and len(other.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).rstrip() + "\n"
