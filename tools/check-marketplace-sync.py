#!/usr/bin/env python3
"""Check that .agents/plugins/marketplace.json stays in sync with each
plugin's own .codex-plugin/plugin.json.

Codex Desktop appears to read version/description/author from the
marketplace catalog entry rather than re-opening each plugin's own
manifest on every view, so a stale catalog entry can leave a plugin's
displayed version "stuck" even after plugin.json is fixed and committed.
This script catches that drift before it reaches users.

Exit 0: everything in sync. Exit 1: at least one mismatch, printed to
stderr with enough detail to fix it directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
SYNCED_FIELDS = ("version", "description", "author")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}") from exc


def main() -> int:
    marketplace = _load_json(MARKETPLACE_PATH)
    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        print(f"ERROR: {MARKETPLACE_PATH} has no 'plugins' array", file=sys.stderr)
        return 1

    problems: list[str] = []

    for entry in entries:
        name = entry.get("name")
        source = entry.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            problems.append(f"marketplace entry missing name/source: {entry!r}")
            continue

        plugin_json_path = (REPO_ROOT / source / ".codex-plugin" / "plugin.json").resolve()
        if not plugin_json_path.is_file():
            problems.append(
                f"[{name}] marketplace source={source!r} has no "
                f".codex-plugin/plugin.json at {plugin_json_path}"
            )
            continue

        plugin = _load_json(plugin_json_path)

        for field in SYNCED_FIELDS:
            marketplace_value = entry.get(field)
            plugin_value = plugin.get(field)
            if marketplace_value != plugin_value:
                problems.append(
                    f"[{name}] {field} drift: marketplace.json has "
                    f"{marketplace_value!r}, plugin.json has {plugin_value!r}"
                )

        if plugin.get("name") != name:
            problems.append(
                f"[{name}] name drift: marketplace entry key is {name!r}, "
                f"plugin.json name is {plugin.get('name')!r}"
            )

    if problems:
        print("Marketplace/plugin metadata drift found:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"\nFix: update {MARKETPLACE_PATH.relative_to(REPO_ROOT)} "
            "to match each plugin's .codex-plugin/plugin.json exactly.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(entries)} plugin(s) in sync with marketplace.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
