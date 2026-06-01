---
name: wiki-query
description: Answer questions from the LLM Wiki using indexed durable pages and optionally file reusable answers back into reports or drafts.
---

# Wiki Query

Use this skill when the user asks a question against the wiki, asks for synthesis, asks for a comparison, or asks to preserve an answer as durable knowledge.

The user-facing request usually looks like:

```text
현재 wiki 기준으로 ingest와 lint의 관계를 설명해주세요.
```

## Boundary

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

This skill answers from durable wiki pages and source-backed evidence. It may create reports or drafts when an answer has reusable value. Durable capture must go through `publish-draft`, which performs dry-run validation, durable application, and lint reporting in sequence.

## Query Modes

| Mode | Use When | Result |
|---|---|---|
| `answer-only` | The user only needs an answer now. | Respond in chat with wiki/source references where useful. |
| `answer-with-report` | The user needs an audit trail. | Write a query report under `scratch/reports/YYYY-MM-DD-query-<slug>.md`. |
| `answer-and-capture` | The answer should become durable wiki knowledge. | Write a JSON draft from `tools/wiki/templates/draft-upsert-page.json` under `scratch/drafts/` and publish through CLI validation when appropriate. |

## Workflow

1. Read `wiki/index.md` first.
2. Identify candidate pages by type: sources, concepts, entities, systems, workflows, decisions, maps.
3. Read relevant durable wiki pages.
4. Inspect `wiki/sources/` or `raw/sources/` only when the answer needs source-level evidence.
5. Separate stated facts, inferences, contested claims, and missing evidence.
6. Answer with synthesis, not a list of retrieved snippets.
7. If mode is `answer-with-report`, write a report under `scratch/reports/YYYY-MM-DD-query-<slug>.md`.
8. Query reports must include: Question, Answer Summary, Pages Consulted, Sources Consulted, Confidence, Contradictions, and Reusable Capture Recommendation.
9. If mode is `answer-and-capture`, write a draft under `scratch/drafts/`.
10. Publish reusable drafts with `python3 tools/wiki/cli.py publish-draft <draft-path> --report`.
11. Run `scripts/release_gate.sh` before reporting that captured durable wiki state is ready. Use `python3 tools/wiki/cli.py lint --report` or `python3 tools/wiki/cli.py health` as faster prechecks while drafting.

## Quality Gates

- Start from `wiki/index.md`; do not answer from memory alone.
- Cite durable wiki pages and raw/source evidence when the answer depends on them.
- Substantive synthesized answers require citations to durable pages or source evidence.
- Do not present inferred claims as direct source facts.
- Do not file an answer back into the wiki unless it has reusable value.
- Run `scripts/release_gate.sh` before reporting that captured durable wiki state is ready.
