"""End-to-end coverage of transaction.apply_bundle on the COMPATIBLE
(native Windows) tier, added in port phase 4g/4h/4i.

Runs the real apply_bundle orchestration (MutationLock -> RuntimeStore ->
_prepare_writes -> per-file write -> journal/result) against a real
filesystem, with only the win32-specific primitives (CreateFile/
GetFileInformationByHandle/LockFileEx) mocked -- everything else (mkdir,
file writes, hashing, journal bookkeeping) runs for real.

This is control-flow and record-format coverage, not proof this works on
real Windows -- that needs the Windows CI job (task #8). What it does prove:
the full apply_bundle call graph audited in phase 4i (MutationLock,
_RuntimeStore/_OperationStore, and every content-write/identity function
transitively reachable from apply_bundle) is internally consistent when
root_fd/meta_fd are Path instead of int, end to end, not just at each
individual layer in isolation.
"""

from __future__ import annotations

import sys
import types

import pytest

from engine.transaction import _WINDOWS_WRITE_OPT_IN_ENV_VAR, apply_bundle


class _FakeWinHandle:
    def Close(self) -> None:
        pass


def _install_fake_win32(monkeypatch):
    fake_win32file = types.SimpleNamespace(
        CreateFile=lambda *a, **k: _FakeWinHandle(),
        GetFileInformationByHandle=lambda h: (0, None, None, None, 1, 0, 0, 1, 2, 3),
        LockFileEx=lambda *a, **k: None,
        UnlockFileEx=lambda *a, **k: None,
    )
    fake_win32con = types.SimpleNamespace(
        GENERIC_READ=1,
        FILE_SHARE_READ=1,
        FILE_SHARE_WRITE=2,
        FILE_SHARE_DELETE=4,
        OPEN_EXISTING=3,
        FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
        LOCKFILE_EXCLUSIVE_LOCK=2,
        LOCKFILE_FAIL_IMMEDIATELY=1,
    )
    fake_pywintypes = types.SimpleNamespace(OVERLAPPED=lambda: object())
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "pywintypes", fake_pywintypes)


@pytest.fixture
def force_compatible_tier(monkeypatch):
    """Force transaction.py's capability_for to report COMPATIBLE regardless
    of host OS, so this test exercises the Windows code path on this Linux
    dev machine. Patched on the engine.transaction module (where it was
    imported via `from .paths import capability_for`), not engine.paths --
    that binding, not the origin, is what apply_bundle/MutationLock call."""

    import engine.transaction as transaction_module
    from engine.hostplatform.capability import compatible_capability

    monkeypatch.setattr(
        transaction_module, "capability_for", lambda vault_root: compatible_capability()
    )
    monkeypatch.setenv(_WINDOWS_WRITE_OPT_IN_ENV_VAR, "1")


def _minimal_bundle(operation_id: str, path: str, content: str) -> dict:
    return {
        "schema": "codex-brain.transaction.v1",
        "operation_id": operation_id,
        "operation_type": "generic",
        "writes": [{"path": path, "mode": "create", "content": content}],
        "expected_hashes": {path: None},
    }


def test_apply_bundle_compatible_tier_creates_file(
    monkeypatch, tmp_vault, force_compatible_tier
):
    _install_fake_win32(monkeypatch)
    bundle = _minimal_bundle("op-generic-1", "wiki/note.md", "hello world\n")

    result = apply_bundle(tmp_vault, bundle)

    assert result["status"] == "complete"
    assert result["changed_paths"] == ["wiki/note.md"]
    assert (tmp_vault / "wiki" / "note.md").read_text() == "hello world\n"


def test_apply_bundle_compatible_tier_idempotent_replay(
    monkeypatch, tmp_vault, force_compatible_tier
):
    _install_fake_win32(monkeypatch)
    bundle = _minimal_bundle("op-generic-2", "wiki/note2.md", "content\n")

    first = apply_bundle(tmp_vault, bundle)
    second = apply_bundle(tmp_vault, bundle)

    assert first == second


def test_apply_bundle_refuses_without_opt_in_env_var(monkeypatch, tmp_vault):
    import engine.transaction as transaction_module
    from engine.hostplatform.capability import compatible_capability
    from engine.transaction import TransactionValidationError

    monkeypatch.setattr(
        transaction_module, "capability_for", lambda vault_root: compatible_capability()
    )
    monkeypatch.delenv(_WINDOWS_WRITE_OPT_IN_ENV_VAR, raising=False)
    bundle = _minimal_bundle("op-generic-3", "wiki/note3.md", "content\n")
    with pytest.raises(TransactionValidationError) as excinfo:
        apply_bundle(tmp_vault, bundle)
    assert excinfo.value.code == "UNSUPPORTED_PLATFORM"
