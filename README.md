# LLM Wiki

LLM Wiki is a quality-gated markdown wiki for humans and LLM agents.

It keeps raw source material separate from durable knowledge, then uses
deterministic validation so agent-written drafts become permanent only after
they pass the vault's quality boundary.

## Install

Quit Claude Code, then run one command:

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
```

The command checks git and Python 3.10 or newer, clones the builder
checkout into `$HOME/.local/share/vault-builder`, creates the
`$HOME/.local/bin/vault-builder` command, and runs `vault-builder setup`. Setup
creates your Vaults, installs the Vault hooks, links the repository, registers
the `vault-wiki` MCP server in Claude Code, and ends with
`vault-builder doctor`. It installs no other tool. Press Enter at every
question to get a personal Vault at `$HOME/vault` and, when the current folder
is a git repository, a project Vault for it at
`$HOME/vaults/<repository folder name>`. Vaults made by an older
vault-builder are upgraded in the same run. Your wiki pages and raw sources
stay as they are. When you edited a file that vault-builder manages, setup
lists those files and stops before it writes anything. The command stops before it writes
`$HOME/.claude.json` while Claude Code is running.

When the command finishes:

1. Start Claude Code again.
2. Run `/mcp` in Claude Code and check that `vault-wiki` is listed as
   connected.

Link another repository later with:

```bash
vault-builder link --repo /path/to/other-repo
```

Without a Vault name, `link` links every shared Vault (roles `personal`,
`team`, and roles not in the known role table). Link a project Vault by name:
`vault-builder link <name> --repo /path/to/other-repo`. Add a Vault later with
`vault-builder setup --vault team="$HOME/team-vault" --role team=team --repo /path/to/app`.

Other options go after `bash -s --`, for example
`--repo /path/to/app --yes` to run without questions. `--help` lists every
option and environment variable. `--uninstall` removes the builder files of every
Vault, the hooks, the `vault-wiki` MCP server, the `vault-builder` command and
the builder checkout. Each Vault keeps `raw/` and `wiki/`. See
[docs/usage.md](docs/usage.md) sections 1 and 17 for details.

## Create a vault folder only

To create only a Vault folder, without hooks, repository links or the MCP
server, use `bootstrap.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
```

This downloads `cutehackers/vault-builder` and creates a ready-to-use `vault/`
folder in the current directory.

## Use a vault

Open the vault and work through the bundled agent skills:

- `wiki-ingest` for turning raw source material into durable wiki pages.
- `wiki-update` for revising existing pages from trusted sources.
- `wiki-query` for answering questions from the compiled wiki and capturing
  reusable results when needed.
- `session-capture` for saving the current agent session into the project
  Vault when you ask for it.

Optional Stenc fixed-format specs and plans are skipped by default. Enable them
only for vaults that intentionally keep that workflow.

## Features

Each feature below is described in the same four parts: what it does, when it
runs, the command, and where it writes. `<config dir>` means the folder in
`CLAUDE_CONFIG_DIR`, or `$HOME/.claude` when that variable is not set.
`vault-builder` commands run from any folder. `python3 tools/wiki/cli.py`
commands run inside a Vault folder. Full details are in
[docs/usage.md](docs/usage.md) sections 16 and 17.

### Setup and doctor

- What it does: `setup` creates or upgrades every Vault, installs the hooks,
  links the repositories, registers the MCP server, and then runs `doctor`.
  `doctor` checks the Vaults, the MCP server entry, the router, the hooks, and
  the `vault-builder` command, and prints one fix command per problem.
- When it runs: manually. The install command runs `setup` once. Quit Claude
  Code before `setup`, because it writes `$HOME/.claude.json`.
- Command: `vault-builder setup`, `vault-builder doctor`.
- Where it writes: `setup` writes the Vault folders, `<config dir>/vault-builder/`,
  the hook entries in `<config dir>/settings.json`, and `mcpServers.vault-wiki`
  in `$HOME/.claude.json`. `doctor` writes nothing and exits 1 when it finds a
  problem.

### Vault roles

- What it does: every Vault has a role. `tools/vault_migration/roles.py` knows
  three roles. `personal` is one person's notes and `team` is a team's shared
  knowledge. Both are shared: `link` without Vault names links them to every
  repository. `project` holds one repository's history and session
  transcripts, and is linked only by name. A role that is not in the table is
  accepted and treated as shared.
- When it runs: manually.
- Command: `vault-builder set-role --vault <name> --role <role>`.
- Where it writes: `vaults.<name>.role` in `<config dir>/vault-builder/vault-map.json`.

### Linking repositories

- What it does: records which Vaults a repository uses. A repository is found
  by its root path, then its `origin` remote URL, then its folder name.
  `set-default-write` picks the Vault that vault-graph writes memories to for
  one project. `set-sharable` decides whether a Vault linked to two or more
  projects shares its pages that have no `project` field with all of them.
  vault-graph reads these two fields. The MCP router does not.
- When it runs: manually. `setup` also runs `link` for each repository.
- Command: `vault-builder link [<vault> ...] --repo <path>`,
  `vault-builder unlink <vault> --repo <path>`, `vault-builder list`,
  `vault-builder set-default-write --project <id> (--vault <name> | --clear)`,
  `vault-builder set-sharable --vault <name> (--value true|false | --clear)`.
- Where it writes: `<config dir>/vault-builder/vault-map.json` only. `list`
  writes nothing.

### Session hooks

- What it does: the hook launcher `<config dir>/vault-builder/vault_hooks.py`
  runs on three Claude Code events. At SessionStart, `session-start` runs
  `vg hook session-start` and passes its output into the session when it exits
  0. When vg is missing or fails, nothing is added. At PreCompact and
  SessionEnd, `capture` runs `vg hook capture`. When vg is missing or fails, it
  falls back to `capture --direct`, which runs `sessions capture` in the one
  `project` Vault linked to the session's repository. A hook never blocks the
  session.
- When it runs: automatically, after `setup` or `vault-builder install <path> --with-hooks`.
- Command: none to run by hand. The vg path is looked up in
  `VAULT_GRAPH_BIN`, then `<config dir>/vault-builder/vault-hooks.json`, then
  `PATH`.
- Where it writes: the launcher itself writes nothing. Capture writes through
  `sessions capture` (next section).

### Session capture

- What it does: saves the session transcript (user messages, agent replies,
  subagent turns, tool names) as a new part file. Thinking blocks and tool
  input and output are left out. Known secret formats are replaced with
  `[REDACTED:<kind>]`. A later capture of the same session writes only the new
  turns, as the next part.
- When it runs: automatically from the PreCompact and SessionEnd hooks, or
  manually through the `session-capture` skill.
- Command: `python3 tools/wiki/cli.py sessions capture --transcript <path>`.
  `sessions status` shows counts and uncommitted files. `sessions git-check`
  fails when a committed session file was changed or deleted.
- Where it writes: `raw/sessions/<agent>/<YYYY-MM-DD>-<session id>-part<N>.md`
  in the one `project` Vault linked to the repository. It never writes to a
  `personal` or `team` Vault, never edits an existing file, and never commits.
  When the repository has no `project` Vault, or has two, nothing is written.

### Session fact attachments

- What it does: adds a fact attachment next to one session part file. The
  command checks the facts JSON against the part file (hash, session id,
  exchange ids, dates, length, secret rules) before writing.
- When it runs: manually. vault-graph calls it after it extracts facts from a
  session.
- Command: `python3 tools/wiki/cli.py sessions attach-facts --session <part> --facts <json>`.
- Where it writes: `raw/sessions/<agent>/<part name without .md>.facts.md`.
  It never overwrites. An existing attachment gives `exists`. A failed check
  gives `rejected` with a reason code, exit 1, and no file.

### Memory review

- What it does: keeps a memory that an agent inferred out of the wiki until a
  person accepts it. `review propose` checks the draft and stores it as a
  pending `memory-proposal` item. `review resolve --status accepted` publishes
  the draft. `--status rejected` or `--status resolved` closes the item
  without changing wiki pages.
- When it runs: `review propose` runs when vault-graph or an agent saves an
  inferred memory. `review list` and `review resolve` are run by a person.
- Command: `python3 tools/wiki/cli.py review propose --draft <draft> --summary <text>`,
  `python3 tools/wiki/cli.py review list`,
  `python3 tools/wiki/cli.py review resolve <item> --status accepted|rejected|resolved --resolution <text>`.
- Where it writes: `propose` writes the item and a copy of the draft under
  `scratch/review/`. `resolve` updates the item and appends `wiki/log.md`.
  Only `accepted` writes the wiki page.

### MCP router

- What it does: one MCP server, `vault-wiki`, serves every Vault. It runs the
  router `<config dir>/vault-builder/llm_wiki_router.py`. On each tool call the
  router reads `vault-map.json` again and runs the target Vault's own
  `tools/wiki/cli.py`. Reads reach only the Vaults linked to the session's
  repository, or the session's own Vault. Writes need the `vault` argument and
  may target any Vault in `vault-map.json`. Path arguments must stay inside
  that Vault.
- When it runs: automatically. Claude Code starts the server, and each tool
  call (`mcp__vault-wiki__<tool>`) runs the router once.
- Command: none to run by hand. `setup` registers it. Run `/mcp` to check that `vault-wiki` is connected.
- Where it writes: the router writes nothing itself. A write tool changes the
  target Vault through that Vault's CLI and its `publish-draft` checks.

## Existing Vault Migration

One Body Owner, Many References: `vault-builder` owns the generated structure,
managed-file contract, and migration policy; a Vault owns raw evidence and
durable wiki knowledge. A new Vault records operational metadata in
`.vault-builder/manifest.json`; it is not durable knowledge. To audit an
existing Vault, run the latest builder checkout and inspect its external plan
before applying it:

```bash
cd /path/to/vault-builder

python3 tools/vault_migration/cli.py audit \
  --target /path/to/vault \
  --plan-out /tmp/vault-migration-plan.json

python3 tools/vault_migration/cli.py apply \
  --target /path/to/vault \
  --plan /tmp/vault-migration-plan.json
```

Exit `0` means no blocking actions, `1` means failure, and `2` means the
read-only audit succeeded but needs human review (`review-required`). Apply
never merges wiki knowledge or rebuilds indexes/maps. Vault Graph is a
read-only consumer and never edits or migrates a Vault.
If any migration recovery artifact remains, every apply is blocked before
target writes and prints the exact `recover --migration-id` command for each
outstanding migration.
Apply and recover also hold one target-wide process lock for their full
operation, so concurrent migration commands fail without writing the target.

If apply reports `Migration committed, cleanup pending`, the version 1 files
and manifest are already committed. Run the exact printed `recover
--migration-id` command; that recovery finishes cleanup only and does not
restore legacy bytes.

If a failed apply reports `Automatic rollback: completed; cleanup pending`,
the prior Vault bytes are already restored. Run the printed `recover` command
to finish removing only the durable preparation or rollback journal.

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
| `raw/sessions/`    | Session transcripts and fact attachments. Add-only, project Vaults only. |
| `wiki/`            | Durable compiled knowledge after validation.                           |
| `scratch/drafts/`  | Proposed wiki changes before publication.                              |
| `scratch/reports/` | Validation, maintenance, ingest, and query reports.                    |
| `scratch/review/`  | Items that need human judgment.                                        |
| `.agents/skills/`  | User-facing workflows for ingest, update, query, and session capture.  |
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
- [docs/LLM-WIKI.md](docs/LLM-WIKI.md) - canonical architecture and vault structure.
- [docs/USER_PROMPTS.md](docs/USER_PROMPTS.md) - prompts for ordinary LLM Wiki use.
- [docs/VAULT_PROMPTS.md](docs/VAULT_PROMPTS.md) - prompts for vault operation and maintenance.
- [docs/agent/OPERATING-SCHEMA.md](docs/agent/OPERATING-SCHEMA.md) - detailed
  agent operating contract.

---

한글: [README-kr.md](README-kr.md)
