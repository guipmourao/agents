"""Shared fixtures for the engine test suite.

Inserts the engine package's parent directory onto ``sys.path`` so tests can
import it the same way the runtime does (``from . import cli`` inside the
package implies ``engine`` is imported as a package, which requires its
parent directory -- not ``engine/`` itself -- on the path).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_ROOT = _ENGINE_ROOT.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from engine.paths import VAULT_SCHEMA, WORKSPACE_CONFIG  # noqa: E402


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """A real, minimal on-disk vault: adoptable and initialized.

    Satisfies both ``paths.is_initialized_vault`` (``wiki/`` plus
    ``.obsidian/`` or ``.raw/``) and ``paths.is_adoptable_vault``, and
    declares a workspace config so ``resolve_vault_root`` can find it without
    relying on cwd-discovery.
    """

    root = tmp_path / "vault"
    (root / "wiki").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    config = {"schema": VAULT_SCHEMA, "vault": "."}
    (root / WORKSPACE_CONFIG).write_text(json.dumps(config), encoding="utf-8")
    return root


@pytest.fixture
def posix_only() -> None:
    if os.name == "nt":
        pytest.skip("POSIX-only test")


@pytest.fixture
def windows_only() -> None:
    if os.name != "nt":
        pytest.skip("Windows-only test")
