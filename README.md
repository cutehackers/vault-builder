# LLM Wiki

LLM Wiki is a quality-gated markdown wiki for humans and LLM agents.

It keeps raw source material separate from durable knowledge, then uses
deterministic validation so agent-written drafts become permanent only after
they pass the vault's quality boundary.

## Simple Setup

Install a new vault with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
```

This downloads `cutehackers/vault-builder` and creates a ready-to-use `vault/`
folder in the current directory.

After setup, open the vault and work through the bundled agent skills:

- `wiki-ingest` for turning raw source material into durable wiki pages.
- `wiki-update` for revising existing pages from trusted sources.
- `wiki-query` for answering questions from the compiled wiki and capturing
  reusable results when needed.

Optional Stenc fixed-format specs and plans are skipped by default. Enable them
only for vaults that intentionally keep that workflow.

## Example Prompts

Send these prompts to your agent as-is. Change only the file names to match
your vault.

```text
Use the wiki-ingest skill to incorporate raw/sources/project-notes.md into the wiki.
Extract the important concepts, decisions, and follow-up questions into durable wiki pages, then complete validation.
```

```text
Use the wiki-update skill to update wiki/concepts/product-strategy.md from the latest trusted sources.
Preserve human-locked sections, check related sources, and create a review item if you find contradictions.
```

```text
Use the wiki-query skill to answer: "What are the key decisions and remaining risks for this project?"
Include the wiki pages and sources used, and propose a reusable draft if the answer should become durable knowledge.
```

## How It Works

The vault keeps raw material separate from durable knowledge:

```text
raw source
-> LLM semantic draft
-> deterministic validation
-> durable wiki page
-> index, links, logs, reports
```

The important rule: LLM output is only a proposal until validation accepts it.

## Essential Folders

| Path               | Purpose                                                                |
| ------------------ | ---------------------------------------------------------------------- |
| `raw/`             | Source material. Agents must not rewrite it unless you explicitly ask. |
| `wiki/`            | Durable compiled knowledge after validation.                           |
| `scratch/drafts/`  | Proposed wiki changes before publication.                              |
| `scratch/reports/` | Validation, maintenance, ingest, and query reports.                    |
| `scratch/review/`  | Items that need human judgment.                                        |
| `agents/skills/`   | User-facing workflows for ingest, update, and query.                   |
| `tools/wiki/`      | Validation and publication internals used by the skills.               |
| `docs/agent/`      | Detailed operating rules for agents.                                   |

## What To Remember

- Treat LLM output as a proposal until validation accepts it.
- Keep important claims tied to source material or prior durable wiki pages.
- Preserve `human-locked` pages and sections.
- Record contradictions instead of silently resolving them.
- Completion is checked through `scripts/release_gate.sh`; agents and CI handle
  that gate before wiki work is considered complete.
- Use the skills as the public interface; deterministic tools are the
  implementation layer behind them.

## More Detail

- [docs/usage.md](docs/usage.md) - Korean usage guide.
- [docs/architecture.md](docs/architecture.md) - Korean architecture guide.
- [docs/agent/OPERATING-SCHEMA.md](docs/agent/OPERATING-SCHEMA.md) - detailed
  agent operating contract.

---

Korean: [README-kr.md](README-kr.md)
