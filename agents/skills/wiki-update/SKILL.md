---
name: wiki-update
description: Update existing durable wiki pages from sources or human instruction while preserving human locks, provenance, links, and contradiction history.
---

# Wiki Update

Use this skill when the user asks to update, revise, repair, merge, or extend existing wiki pages.

The user-facing request usually looks like:

```text
wiki/concepts/example.md를 최신 원본 기준으로 업데이트해주세요.
```

## Boundary

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

This skill reasons about meaning, source support, contradictions, and draft changes. Durable updates must go through `publish-draft`, which performs dry-run validation, durable application, and lint reporting in sequence.

## Workflow

1. Read the target wiki page and its frontmatter.
2. Read `wiki/index.md` and related pages listed in `related` or body links.
3. Inspect supporting `wiki/sources/` pages and raw sources when needed.
4. Check for page-level and section-level human locks.
5. Classify the change as simple update, contradiction handling, merge proposal, decision update, or human-review item.
6. For section-level locks, require exact content preservation between `<!-- human-locked:start -->` and `<!-- human-locked:end -->`.
7. If content can be changed safely, write a JSON draft under `scratch/drafts/` using `tools/wiki/templates/draft-upsert-page.json`.
8. If a locked section needs a proposed patch, write it under `scratch/reports/`.
9. If the issue is a contested decision, important contradiction, merge judgment, or unresolved source conflict, write it under `scratch/review/`.
10. Publish safe drafts with `python3 tools/wiki/cli.py publish-draft <draft-path> --report`.
11. Include index and log changes in the draft when navigation or durable history changes.
12. Run `scripts/release_gate.sh` before reporting that durable wiki state is ready. Use `python3 tools/wiki/cli.py lint --report` or `python3 tools/wiki/cli.py health` as faster prechecks while drafting.
13. Report the exact pages updated, provenance added, contradictions found, and remaining review items.

## Quality Gates

- Do not erase older views without recording why they changed.
- Do not silently resolve contradictions.
- Do not overwrite human-locked content.
- Keep index and log changes tied to durable wiki changes.
- Run `scripts/release_gate.sh` before reporting that durable wiki state is ready.
