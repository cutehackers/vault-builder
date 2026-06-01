# Wiki Tools

Deterministic tooling for the LLM Wiki belongs here.

The boundary is:

- LLM agents propose semantic drafts.
- `tools/wiki` validates, normalizes, and applies durable writes.
- `wiki/` stores compiled knowledge after quality checks.

Implemented commands:

```bash
scripts/release_gate.sh
python3 tools/wiki/cli.py validate-page wiki/overview.md
python3 tools/wiki/cli.py hash-source raw/sources/example.md
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
python3 tools/wiki/cli.py source-registry --report
python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report
python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
python3 tools/wiki/cli.py review create --type contradiction --summary "Example Review" --related wiki/overview.md --context "Why human judgment is needed."
python3 tools/wiki/cli.py review list
python3 tools/wiki/cli.py review resolve scratch/review/example-review.md --status accepted --resolution "Accepted with rationale."
python3 tools/wiki/cli.py merge scan --report --create-review
python3 tools/wiki/cli.py maps build --report
python3 tools/wiki/cli.py maps build --check --report
python3 tools/wiki/cli.py metrics --report
python3 tools/wiki/cli.py metrics --check --report
python3 tools/wiki/cli.py metrics --check --policy tools/wiki/metrics-policy.json --report
python3 tools/wiki/cli.py health
python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run
python3 tools/wiki/cli.py workflow ingest --source raw/sources/example.md --report
python3 tools/wiki/cli.py workflow update --target wiki/concepts/example.md --preflight --report
python3 tools/wiki/cli.py workflow query --question "..." --prepare-report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report
python3 tools/wiki/mcp_server.py
python3 tools/wiki/cli.py lint
python3 tools/wiki/cli.py lint --report scratch/reports/YYYY-MM-DD-lint.md
python3 tools/wiki/cli.py lint --report
```

Exit codes:

- `0` means no validation issues were found.
- non-zero means one or more validation issues were found.
- `--report` without a path writes `scratch/reports/YYYY-MM-DD-lint.md`.

Current checks:

- required frontmatter fields
- required `quality` fields
- allowed enum values
- required field types for `source_count`, `tags`, `related`, and `quality.review_required`
- Obsidian-style wiki link resolution with `wiki/` root boundary checks
- required core index links
- orphan durable pages not linked from `wiki/index.md` or `wiki/maps/*.md`
- parseable log event prefixes
- basic provenance signals
- claim-level provenance tables with allowed statuses, evidence refs, source_count consistency, and source hash linkage
- raw source drift when source pages declare `canonical_source` and `raw_sha256`
- unmatched human-lock markers
- source registry inventory across raw files and source pages
- bulk source registration with per-source outcomes
- section-level human-lock exact content preservation during draft application
- review queue item schema and lifecycle status validation
- lifecycle metadata checks for aliases, redirects, supersedes, superseded_by, and deprecated pages
- duplicate candidate scan with merge proposal reports and optional review item creation
- query report mode and reusable answer capture draft scaffolding
- generated navigation maps for topics, sources, decisions, pending reviews, and lifecycle signals
- stale generated map detection through `maps build --check`
- maintenance metrics for pending reviews, contested claims, stale sources, orphan pages, provenance coverage, deprecated links, and last health report
- configurable maintenance metric thresholds through `tools/wiki/metrics-policy.json` or `metrics --policy`
- report and generated query-capture draft path confinement under `scratch/reports/` and `scratch/drafts/`
- release gate wrapper for tests, lint, health, map freshness, repo-local Stenc validation/render checks, and `git diff --check`; validation runs in a temporary repo copy

Intentional limits:

- The validator does not decide whether a claim is true.
- The validator does not auto-fix pages.
- The validator does not mutate `raw/`.
- `ingest-source` registers a raw source with hash, source page, index, log, and report.
- `source-registry` reports raw/source-page inventory; it does not create semantic wiki pages.
- `bulk-ingest` repeatedly applies source registration; it does not perform semantic extraction.
- `apply-draft` is validation-only by default; use `publish-draft` for the normal durable publication path.
- `publish-draft` validates, applies, lints, and rolls back page/index/log changes when post-apply lint fails; it does not perform semantic extraction.
- `publish-batch` transactionally validates, applies, lints, and rolls back multi-page draft changes; it does not perform semantic extraction.
- Batch page entries must not include their own `log`; use the top-level batch `log` for the single durable operation record.
- Claim-level provenance validation checks evidence structure and hash linkage; it does not decide whether the claim is true.
- `review` commands manage the human-judgment queue and resolution log entries; they do not make the judgment for the human.
- `merge scan` detects duplicate candidates and can create an idempotent merge review item; it never merges pages automatically.
- `maps build` deterministically rebuilds generated navigation maps, updates index/log, and can fail in `--check` mode when generated maps are stale.
- `metrics` writes an operational dashboard; it reports maintenance pressure but does not decide or repair content.
- `metrics --policy` requires the explicit policy file to exist.
- `workflow query --mode` records supplied query synthesis into reports or draft scaffolds; it does not synthesize the answer itself.
- `health` runs lint with the default dated report path.
- `scripts/release_gate.sh` is the repo-level gate for completion and release checks; it composes existing validators instead of adding separate semantics.
- `.github/workflows/release-gate.yml` runs the same gate in CI.
- `mcp_server.py` exposes the stable CLI contract as stdio MCP tools.

Next hardening priorities:

1. Higher-level semantic extraction prompts and review dashboards.
2. Stronger contradiction triage reports.
3. Operator-oriented docs for long-running vault maintenance.

MCP tools:

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
