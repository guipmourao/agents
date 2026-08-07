# agents

A marketplace of specialized Codex plugins: software engineering, data,
infrastructure, security, automation, and personal memory workflows.

## Plugins

| Plugin | Description |
|---|---|
| [`data-engineering`](plugins/data-engineering) | Dimensional modeling, data warehouse design, data quality, ETL/ELT, analytical architecture. |
| [`codex-brain`](plugins/codex-brain) | Local-first persistent memory vault: transaction-reviewed writes, source/claim provenance ledgers, onboarding and assistant skills. |

## Installing

The marketplace manifest lives at `.agents/plugins/marketplace.json`.

```bash
codex plugin marketplace add guipmourao/agents
```

Or, for local development, clone first and point at the local path:

```bash
git clone https://github.com/guipmourao/agents.git
codex plugin marketplace add ./agents
```

## Adding a plugin

A plugin needs `plugins/<name>/.codex-plugin/plugin.json` plus an entry in
`.agents/plugins/marketplace.json`. Keep `version`/`description`/`author`
in sync between a plugin's own manifest and its marketplace catalog entry
— some install surfaces read the catalog entry rather than re-fetching the
plugin's own manifest, so a stale catalog entry can make a real update
look like it never happened.
