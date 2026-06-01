# LLM Wiki

An immediately usable, quality-gated markdown wiki for humans and LLM agents.

This is not a passive RAG folder. Raw material stays immutable under `raw/`; durable knowledge is compiled into linked markdown pages under `wiki/`; deterministic tools enforce the quality boundary before changes are treated as permanent.

## Quick Start

Create a new vault from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
cd llm-wiki
python3 tools/wiki/cli.py health
```

Create a vault with a custom directory name:

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash -s -- ~/my-llm-wiki
cd ~/my-llm-wiki
python3 tools/wiki/cli.py lint --report
```

Run the full release gate during bootstrap:

```bash
VAULT_BOOTSTRAP_VERIFY=full curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
```

Or create a new vault from a local checkout:

```bash
./init-vault.sh ~/my-llm-wiki
cd ~/my-llm-wiki
python3 tools/wiki/cli.py lint --report
```

Detailed docs:

- `docs/usage.md` - full Korean usage guide.
- `docs/architecture.md` - full Korean architecture guide.
- `docs/agent/OPERATING-SCHEMA.md` - detailed agent operating reference.

Or validate this checkout:

```bash
scripts/release_gate.sh
```

## How To Use It

1. Put source files in `raw/sources/`.
2. Register the source page with hash, index, log, and report:

   ```bash
   python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
   ```

3. Hash a source directly when you only need the digest:

   ```bash
   python3 tools/wiki/cli.py hash-source raw/sources/example.md
   ```

4. Ask an agent to extract reusable knowledge into a draft under `scratch/drafts/`.
   Start from `tools/wiki/templates/draft-upsert-page.json`.
5. Publish the draft through the deterministic boundary:

   ```bash
   python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
   ```

6. Run the release gate before treating the wiki update as complete:

   ```bash
   scripts/release_gate.sh
   ```

## Mental Model

```text
raw source
-> LLM semantic draft
-> deterministic validation
-> durable wiki page
-> index / links / log / reports
-> Git history
```

The important rule: LLM output is only a proposal until validation applies it to `wiki/`.

## Directory Guide

| Path | Purpose |
|---|---|
| `raw/sources/` | Immutable source files. |
| `wiki/` | Durable compiled knowledge. |
| `scratch/drafts/` | JSON drafts proposed by an LLM. |
| `scratch/reports/` | Lint, draft, ingest, query, and repair reports. |
| `scratch/review/` | Items that need human judgment. |
| `tools/wiki/` | Validation, hashing, and draft-application tools. |
| `docs/agent/` | Detailed agent operating schema. |

## Core Commands

Validate one page:

```bash
python3 tools/wiki/cli.py validate-page wiki/overview.md
```

Lint the whole wiki:

```bash
python3 tools/wiki/cli.py lint --report
```

Run the human-friendly health check:

```bash
python3 tools/wiki/cli.py health
```

Rebuild generated navigation maps:

```bash
python3 tools/wiki/cli.py maps build --report
```

Write maintenance metrics:

```bash
python3 tools/wiki/cli.py metrics --report
python3 tools/wiki/cli.py metrics --check --report
python3 tools/wiki/cli.py metrics --check --policy tools/wiki/metrics-policy.json --report
```

When `--policy` is provided, the policy file must exist. Reports are written under `scratch/reports/`.

Hash a raw source:

```bash
python3 tools/wiki/cli.py hash-source raw/sources/example.md
```

Validate a semantic draft without writing:

```bash
python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run
```

Publish a semantic draft with dry-run, apply, lint, and rollback on post-apply lint failure:

```bash
python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
```

Publish a multi-page batch draft transactionally:

```bash
python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
```

Register a raw source:

```bash
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
```

Inspect raw/source-page inventory:

```bash
python3 tools/wiki/cli.py source-registry --report
```

Register multiple raw sources:

```bash
python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report
```

Create, list, and resolve human-review queue items:

```bash
python3 tools/wiki/cli.py review create --type contradiction --summary "Example Review" --related wiki/overview.md --context "Why human judgment is needed."
python3 tools/wiki/cli.py review list
python3 tools/wiki/cli.py review resolve scratch/review/example-review.md --status accepted --resolution "Accepted with rationale."
```

Scan duplicate candidates without merging automatically:

```bash
python3 tools/wiki/cli.py merge scan --report --create-review
```

Rebuild/check generated navigation maps and write metrics:

```bash
python3 tools/wiki/cli.py maps build --check --report
python3 tools/wiki/cli.py metrics --check --report
```

Create query reports or reusable answer capture drafts:

```bash
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report
```

Run the MCP wrapper for agents that can use MCP stdio tools:

```bash
python3 tools/wiki/mcp_server.py
```

`scripts/release_gate.sh` runs validation in a temporary copy of the repo so tests and generated reports do not mutate the working vault. The same gate is wired into `.github/workflows/release-gate.yml`.

Exposed MCP tools:

- `wiki_validate_page`
- `wiki_lint`
- `wiki_hash_source`
- `wiki_ingest_source`
- `wiki_source_registry`
- `wiki_bulk_ingest`
- `wiki_apply_draft`
- `wiki_publish_draft`
- `wiki_publish_batch`
- `wiki_review_create`
- `wiki_review_list`
- `wiki_review_resolve`
- `wiki_merge_scan`
- `wiki_maps_build`
- `wiki_metrics`
- `wiki_health`
- `wiki_workflow_ingest`
- `wiki_workflow_update_preflight`
- `wiki_workflow_update_publish`
- `wiki_workflow_query_prepare`
- `wiki_workflow_query_capture`
- `wiki_workflow_query_publish`

## Draft Format

`publish-draft` accepts JSON like this for durable publication. `apply-draft --dry-run` validates the same shape without writing. The reusable template is `tools/wiki/templates/draft-upsert-page.json`.
For multi-page operations, use `tools/wiki/templates/draft-batch-upsert-pages.json`; batch page entries omit page-level `log` and use the top-level batch `log`.
When `quality.provenance` is `claim`, the body must include a `Claim | Status | Evidence` table with allowed statuses `stated`, `inferred`, `contested`, or `deprecated`.

```json
{
  "version": 1,
  "operation": "upsert-page",
  "path": "wiki/concepts/example.md",
  "frontmatter": {
    "title": "Example",
    "type": "concept",
    "status": "active",
    "created": "2026-05-28",
    "updated": "2026-05-28",
    "owner": "agent",
    "summary": "One-sentence summary.",
    "source_count": 0,
    "tags": [],
    "related": [],
    "confidence": "medium",
    "quality": {
      "provenance": "none",
      "links": "unchecked",
      "contradictions": "none",
      "review_required": false
    }
  },
  "body": "# Example\n\nDraft body.\n",
  "index": {
    "section": "Concepts",
    "target": "concepts/example",
    "summary": "One-sentence summary."
  },
  "log": {
    "event_type": "repair",
    "title": "Apply Example Draft",
    "items": ["Added [[concepts/example]]."]
  }
}
```

## Agent Entry Points

- `AGENTS.md` is the short operational contract.
- `docs/usage.md` is the full human usage guide.
- `docs/architecture.md` explains how the system works.
- `docs/agent/OPERATING-SCHEMA.md` is the full reference.
- `docs/LLM-WIKI.md` explains the architecture and quality model.
- `wiki/index.md` and `wiki/overview.md` are the wiki reading entry points.

## Quality Standard

A wiki update is incomplete until:

- raw files were not modified,
- frontmatter and links validate,
- source hashes match when declared,
- contradictions are recorded instead of erased,
- human-locked content is preserved,
- index/log/report updates exist when required,
- `scripts/release_gate.sh` exits successfully.
