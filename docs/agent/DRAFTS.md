# Semantic Drafts

Drafts are LLM proposals. They are not durable knowledge until `publish-draft`
validates, writes them under `wiki/`, runs whole-vault lint, and leaves a
report. Treat `publish-draft` as the durable publication gate.

Use `tools/wiki/templates/draft-upsert-page.json` as the starting shape.
Use `tools/wiki/templates/draft-batch-upsert-pages.json` when one semantic operation must update multiple durable pages transactionally.
In a batch draft, put the durable operation record in the top-level `log`; individual `pages[]` entries must not include `log`.
If a page sets `quality.provenance` to `claim`, include a markdown table with `Claim`, `Status`, and `Evidence` columns. Allowed row statuses are `stated`, `inferred`, `contested`, and `deprecated`.

## Required Flow

1. Read `wiki/index.md`.
2. Read related pages to avoid duplicates.
3. Prepare a JSON draft under `scratch/drafts/`.
4. For optional low-level validation before publication, run:

   ```bash
   python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run
   ```

5. For durable publication, run:

   ```bash
   python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
   ```
   For multi-page changes, publish the batch draft instead:
   ```bash
   python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
   ```

## Draft Rules

- `path` must stay under `wiki/`.
- `frontmatter` must satisfy the required wiki schema.
- `body` must be complete markdown, including provenance when `quality.provenance` is `section` or `claim`.
- `index` should point to the section where the page should be discoverable.
- `log` should explain the durable operation.
- Contested or judgment-heavy claims go to `scratch/review/`, not directly to stable wiki text.
- Use `review create/list/resolve` for contradiction, merge, contested-claim, and human-lock decisions that need a lifecycle and log entry.
- Use `merge scan --report --create-review` before manual merge proposals when duplicate pages or shared aliases are suspected.
- Use `workflow query --mode answer-and-capture` when a reusable query answer should become a draft scaffold before publication.
- After durable publication, `scripts/release_gate.sh` must succeed before the
  wiki work is reported complete.
