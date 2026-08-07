# codex-brain

A persistent memory plugin for Codex: a local vault with writes always
reviewed through a transaction, source/claim provenance ledgers, and
onboarding/assistant skills that propose content instead of writing on
their own.

Part of the [`guipmourao/agents`](https://github.com/guipmourao/agents)
marketplace — see the [root README](../../README.md) for how to install
this plugin. See `AGENTS.md` in this folder for the rules this plugin
follows internally.

## Skills

| Skill | What it does |
|---|---|
| `brain-init` | Initialize a new vault or adopt an existing directory |
| `brain-save` | Persist a specific result from a conversation |
| `brain-ingest` | Turn a supplied source into connected, cited pages |
| `brain-query` | Answer using only what is already in the vault (read-only) |
| `brain-onboarding` | Entry point: initializes the vault if needed, understands the workspace, proposes `wiki/people/`/`wiki/projects/` |
| `brain-assistant` | Ongoing support after onboarding: context, drafts, next steps |
| `brain-new-person` | Create or update a note in `wiki/people/` |
| `brain-new-project` | Bootstrap a new project or experiment |
| `brain-write-like-me` | Extract a writing-style profile from approved samples |
| `brain-loop` | Design a recurring check orchestrated by an external scheduler |
| `brain-lint` | Deterministic vault health check: dead links, orphans, missing frontmatter (read-only) |

## Hooks

`hooks/hooks.json` wires up two lifecycle hooks (both read-only): `SessionStart`
(injects `wiki/hot.md` into the session, off by default — requires
`CODEX_BRAIN_SESSION_CONTEXT=1`) and `Stop` (warns about an incomplete
transaction). See `AGENTS.md` for details.

## Try the engine without installing anything

```bash
python3 skills/brain-init/scripts/vault.py --help
```

## Publishing a new version

Whenever `.codex-plugin/plugin.json`'s version changes, update the root
`.agents/plugins/marketplace.json` too: the `plugins[].version` field (and
`description`/`author`/`homepage` when relevant) must match `plugin.json`.
Codex Desktop appears to read version/description from the marketplace
catalog rather than re-opening each plugin's own manifest every time — a
stale `marketplace.json` entry can leave the version "stuck" even after
`plugin.json` is fixed and committed. This pattern (two synchronized
copies of the metadata) was confirmed by inspecting the real
[`wshobson/agents`](https://github.com/wshobson/agents) marketplace.

## Status

- Python engine (`skills/brain-init/scripts/`): runs standalone and was
  verified (`--help`, plus real `init`/`hook` dry-runs and applies).
- `.codex-plugin/plugin.json`: valid JSON, follows the documented
  structure (`name`/`version`/`description`/`author`/`skills`/`hooks`).
- Installation via `codex plugin marketplace add`: not yet confirmed
  end-to-end against a real Codex CLI session in this environment.
