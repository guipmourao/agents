# agents

A marketplace of specialized plugins for Claude Code and Codex: software
engineering, data, infrastructure, security, automation, and personal
memory workflows.

## Plugins

| Plugin | Hosts | Description |
|---|---|---|
| [`data-engineering`](plugins/data-engineering) | Claude Code | Dimensional modeling, data warehouse design, data quality, ETL/ELT, analytical architecture. |
| [`codex-brain`](plugins/codex-brain) | Claude Code, Codex | Local-first persistent memory vault: transaction-reviewed writes, source/claim provenance ledgers, onboarding and assistant skills. |

## Installing

**Claude Code:** the marketplace manifest lives at `.claude-plugin/marketplace.json`.

**Codex:** the marketplace manifest lives at `.agents/plugins/marketplace.json`
(only lists plugins that are actually Codex-compatible — currently
`codex-brain`).

```bash
codex plugin marketplace add guipmourao/agents
```

Or, for local development, clone first and point at the local path:

```bash
git clone https://github.com/guipmourao/agents.git
codex plugin marketplace add ./agents
```

## Adding a plugin

A plugin that should work on both Claude Code and Codex needs both
`plugins/<name>/.claude-plugin/plugin.json` and
`plugins/<name>/.codex-plugin/plugin.json`, and an entry in both root
marketplace files (`.claude-plugin/marketplace.json` and
`.agents/plugins/marketplace.json`). Keep `version`/`description`/`author`
in sync between a plugin's own manifest and its marketplace catalog
entries — some install surfaces read the catalog entry rather than
re-fetching the plugin's own manifest, so a stale catalog entry can make a
real update look like it never happened.
