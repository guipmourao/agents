"""Coverage for _CheckpointStore's COMPATIBLE-tier (native Windows,
meta_fd/transactions_fd/operation_fd: Path) branch, added in port phase 5.

Pure path operations with no Windows-specific call inside them, so they run
for real here on Linux by acquiring a real MutationLock in COMPATIBLE mode
(mocked win32 for the lock's own exclusivity primitive only) and exercising
_CheckpointStore against it directly.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from engine.checkpoint import CheckpointError, _CheckpointStore
from engine.transaction import MutationLock


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


@pytest.fixture
def compatible_store(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    (tmp_vault / ".vault-meta" / "transactions" / "op-1").mkdir(parents=True)
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock._acquire_compatible()
    store = _CheckpointStore(lock, tmp_vault, "op-1")
    try:
        yield store
    finally:
        store.close()
        lock._release_compatible()


def test_checkpoint_store_compatible_uses_path_handles(compatible_store):
    assert isinstance(compatible_store.meta_fd, Path)
    assert isinstance(compatible_store.transactions_fd, Path)
    assert isinstance(compatible_store.operation_fd, Path)


def test_write_then_read_roundtrip(compatible_store):
    compatible_store.write("checkpoint.pending.json", {"a": 1})
    assert compatible_store.exists("checkpoint.pending.json") is True
    value = compatible_store.read(
        "checkpoint.pending.json", label="pending state", code="CORRUPT_CHECKPOINT"
    )
    assert value == {"a": 1}


def test_exists_false_for_missing(compatible_store):
    assert compatible_store.exists("checkpoint.json") is False


def test_unlink_removes_file(compatible_store):
    compatible_store.write("checkpoint.pending.json", {"a": 1})
    compatible_store.unlink("checkpoint.pending.json")
    assert compatible_store.exists("checkpoint.pending.json") is False


def test_other_pending_finds_sibling_operation(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    (tmp_vault / ".vault-meta" / "transactions" / "op-1").mkdir(parents=True)
    (tmp_vault / ".vault-meta" / "transactions" / "op-2").mkdir(parents=True)
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock._acquire_compatible()
    try:
        store1 = _CheckpointStore(lock, tmp_vault, "op-1")
        try:
            assert store1.other_pending() is None
        finally:
            store1.close()
        store2 = _CheckpointStore(lock, tmp_vault, "op-2")
        try:
            store2.write("checkpoint.pending.json", {"a": 1})
        finally:
            store2.close()
        store1 = _CheckpointStore(lock, tmp_vault, "op-1")
        try:
            assert store1.other_pending() == "op-2"
        finally:
            store1.close()
    finally:
        lock._release_compatible()


def test_checkpoint_store_missing_operation_raises(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    (tmp_vault / ".vault-meta" / "transactions").mkdir(parents=True)
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock._acquire_compatible()
    try:
        with pytest.raises(CheckpointError):
            _CheckpointStore(lock, tmp_vault, "does-not-exist")
    finally:
        lock._release_compatible()
