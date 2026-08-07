"""Coverage for the runtime/operation store's COMPATIBLE (native Windows)
path-mode, added in port phase 4e.

Most of the primitive functions (_open_runtime_directory_at,
_atomic_runtime_write_at, _remove_pinned_runtime_tree_at, ...) branch
internally on ``isinstance(directory, Path)`` with no Windows-specific call
inside that branch, so they run for real here on Linux. Only
_RuntimeStore._from_lock_compatible needs a held COMPATIBLE-tier
MutationLock, which needs the same mocked win32file/win32con/pywintypes
pattern as test_mutation_lock_compatible.py.

Control-flow and record-format coverage, not proof this works on real
Windows -- see that file's module docstring for the same caveat.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from engine.transaction import (
    MutationLock,
    _OperationStore,
    _RuntimeStore,
    _atomic_runtime_write_at,
    _bounded_runtime_names,
    _open_runtime_directory_at,
    _read_runtime_bytes_at,
    _remove_pinned_runtime_tree_at,
    _runtime_entry_metadata,
)


def test_open_runtime_directory_at_path_mode_creates(tmp_path):
    child = _open_runtime_directory_at(tmp_path, "transactions", create=True)
    assert isinstance(child, Path)
    assert child.is_dir()


def test_open_runtime_directory_at_path_mode_missing_without_create(tmp_path):
    with pytest.raises(FileNotFoundError):
        _open_runtime_directory_at(tmp_path, "transactions", create=False)


def test_open_runtime_directory_at_path_mode_rejects_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "transactions"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _open_runtime_directory_at(tmp_path, "transactions", create=False)


def test_runtime_entry_metadata_path_mode(tmp_path):
    assert _runtime_entry_metadata(tmp_path, "missing") is None
    (tmp_path / "present").write_text("x")
    assert _runtime_entry_metadata(tmp_path, "present") is not None


def test_bounded_runtime_names_path_mode(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _bounded_runtime_names(tmp_path, limit=10, label="test") == ["a", "b"]


def test_atomic_runtime_write_and_read_path_mode_roundtrip(tmp_path):
    _atomic_runtime_write_at(tmp_path, "bundle.json", b'{"a": 1}')
    raw = _read_runtime_bytes_at(tmp_path, "bundle.json", label="bundle", limit=1024)
    assert raw == b'{"a": 1}'
    # No leftover temp file.
    assert [p.name for p in tmp_path.iterdir()] == ["bundle.json"]


def test_remove_pinned_runtime_tree_at_path_mode(tmp_path):
    operation = tmp_path / "op-1"
    operation.mkdir()
    (operation / "bundle.json").write_text("{}")
    backups = operation / "backups"
    backups.mkdir()
    (backups / "file.bak").write_text("x")

    _remove_pinned_runtime_tree_at(tmp_path, "op-1", operation)
    assert not operation.exists()


def test_remove_pinned_runtime_tree_at_path_mode_rejects_identity_change(
    tmp_path, monkeypatch
):
    """The entry-identity re-check (captured at the top of the call as
    ``entry_identity``, compared again right before ``rmdir``) must catch a
    concurrent replace. Drives the two ``Path.lstat`` calls this function
    makes on ``directory`` directly: real identity first, a decoy's identity
    second -- exactly what "the directory got swapped mid-call" looks like.
    """

    operation = tmp_path / "op-1"
    operation.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    from engine.transaction import _LockIdentityChanged

    real_lstat = Path.lstat
    calls = {"count": 0}

    def _lstat_real_then_decoy(self: Path):
        if self == operation:
            calls["count"] += 1
            return real_lstat(self) if calls["count"] == 1 else real_lstat(decoy)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat_real_then_decoy)
    with pytest.raises(_LockIdentityChanged):
        _remove_pinned_runtime_tree_at(tmp_path, "op-1", operation)


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
        GENERIC_WRITE=2,
        FILE_SHARE_READ=1,
        FILE_SHARE_WRITE=2,
        FILE_SHARE_DELETE=4,
        OPEN_EXISTING=3,
        OPEN_ALWAYS=4,
        FILE_ATTRIBUTE_NORMAL=0x80,
        FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
        LOCKFILE_EXCLUSIVE_LOCK=2,
        LOCKFILE_FAIL_IMMEDIATELY=1,
    )
    fake_pywintypes = types.SimpleNamespace(OVERLAPPED=lambda: object())
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "pywintypes", fake_pywintypes)


def test_runtime_store_full_cycle_compatible(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock._acquire_compatible()
    try:
        store = _RuntimeStore.from_lock(lock, create=True)
        try:
            assert store.operation_names() == []
            operation = store.open_operation("op-1", create=True)
            assert isinstance(operation, _OperationStore)
            operation.write_bundle({"schema": "test"})
            assert operation.exists("bundle.json")
            operation.assert_current()
            store.assert_current()
            assert store.operation_names() == ["op-1"]
            store.remove_operation(operation)
            assert store.operation_names() == []
        finally:
            store.close()
    finally:
        lock._release_compatible()
