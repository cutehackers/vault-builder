---
name: project-memory
description: Use when writing a spec, design, or plan, or when asked about past decisions or open work (spec, 설계, 계획, 지난 결정, 남은 일). Prints this repository's wiki project summary.
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/recall.sh *)
---

# Project Memory

Print the project summary that the linked wiki vaults keep for the current git repository.

Run this command once, passing the user's argument unchanged:

```bash
${CLAUDE_SKILL_DIR}/scripts/recall.sh $ARGUMENTS
```

The argument is one of:

- `open`: open work.
- `decisions`: current decisions.
- `corrections`: corrections and rules.
- nothing: all three sections.

When you invoke this skill yourself, pass only the section the task needs.

Reading the output:

- Each vault starts with a `# vault <name>: <summary page path>` line.
- Item lines end with the evidence page path `(wiki/<path>.md)`, relative to that vault. Open an evidence page only when you need more than its summary line.
- A single `project-memory:` line means nothing was printed for a stated reason, such as no git repository or no linked vault. Report that line and continue without it.
- A last `project-memory: output truncated` line means items were cut to fit the size limit.
