---
name: wiki-ingest
description: Reflect a new raw source into the LLM Wiki by registering the source, extracting durable knowledge into drafts, and publishing only through CLI validation.
---

# Wiki Ingest

Use this skill when the user asks to add, ingest, reflect, import, or turn a new source into wiki knowledge.

The user-facing request usually looks like:

```text
raw/sources/example.md를 wiki에 반영해주세요.
```

## Boundary

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

This skill may read raw sources, inspect wiki pages, identify reusable concepts, and write semantic drafts or reports. It must not directly finalize durable wiki updates without the CLI validation path.

## Workflow

1. Confirm the source exists under `raw/`.
2. Read `wiki/index.md` before proposing new pages.
3. Check whether a matching `wiki/sources/` page already exists.
4. Classify source registration before writing:
   - same canonical_source and same raw_sha256: unchanged source; do not create duplicate index or log entries.
   - same canonical_source with a different raw_sha256: source-drift; stop and report the drift instead of overwriting.
   - same generated slug with a different canonical_source: slug-collision; stop and ask for a new slug or human review.
5. If needed, run `python3 tools/wiki/cli.py ingest-source <raw-path> --report`.
6. Read related pages from `wiki/` and avoid duplicate concept/entity/system/workflow pages.
7. Extract durable concepts, entities, systems, workflows, decisions, claims, contradictions, and open questions.
8. For every substantive extracted page, set `source_count` and `quality.provenance` to match the actual evidence, then cite evidence in the body.
9. Write JSON drafts under `scratch/drafts/` using `tools/wiki/templates/draft-upsert-page.json`.
10. Publish drafts with `python3 tools/wiki/cli.py publish-draft <draft-path> --report`.
11. Run `scripts/release_gate.sh` before reporting that durable wiki state is ready. Use `python3 tools/wiki/cli.py lint --report` or `python3 tools/wiki/cli.py health` as faster prechecks while drafting.
12. Report changed pages, generated reports, unresolved contradictions, and human review items.

## Quality Gates

- Do not mutate files under `raw/`.
- Do not create a duplicate page if an existing page should be updated.
- Preserve human-locked pages and sections.
- Mark inferred and contested claims explicitly.
- Keep provenance in the body, not only in frontmatter.
- Do not hide high-impact contradictions in stable text; record the tension or move it to `scratch/review/`.
- Run `scripts/release_gate.sh` before reporting that durable wiki state is ready.
