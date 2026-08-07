"""Coverage for capture.py's COMPATIBLE-tier (native Windows) branch, added
in port phase 6.

CaptureQueueLock mirrors transaction.MutationLock's design exactly (same
primitives, same tier dispatch), so this reuses the same mocked win32file/
win32con/pywintypes pattern as test_mutation_lock_compatible.py. The
_runtime_entry_exists/_read_runtime_regular/_atomic_runtime_write Path-mode
branches are pure path operations and run for real without mocking.

Control-flow and record-format coverage, not proof this works on real
Windows -- see test_mutation_lock_compatible.py's module docstring for the
same caveat. Also moot in production today outside CODEX_BRAIN_WINDOWS_WRITE.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from engine.capture import (
    CaptureQueue,
    CaptureQueueLock,
    _atomic_runtime_write,
    _read_runtime_regular,
    _runtime_entry_exists,
)
from engine.transaction import _WINDOWS_WRITE_OPT_IN_ENV_VAR


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
def force_compatible_tier(monkeypatch):
    import engine.transaction as transaction_module
    from engine.hostplatform.capability import compatible_capability

    monkeypatch.setattr(
        transaction_module, "capability_for", lambda vault_root: compatible_capability()
    )
    monkeypatch.setenv(_WINDOWS_WRITE_OPT_IN_ENV_VAR, "1")


def test_runtime_entry_exists_and_read_write_path_mode(tmp_path):
    assert _runtime_entry_exists(tmp_path, "queue.json") is False
    _atomic_runtime_write(tmp_path, "queue.json", b'{"a": 1}')
    assert _runtime_entry_exists(tmp_path, "queue.json") is True
    raw = _read_runtime_regular(tmp_path, "queue.json", limit=1024, missing_ok=False)
    assert raw == b'{"a": 1}'


def test_capture_queue_lock_compatible_cycle(
    monkeypatch, tmp_vault, force_compatible_tier
):
    _install_fake_win32(monkeypatch)
    lock = CaptureQueueLock(tmp_vault, timeout=1.0)
    lock.acquire()
    try:
        assert lock.acquired is True
        assert (tmp_vault / ".vault-meta" / "capture" / "queue.lock").is_dir()
        runtime = lock.duplicate_runtime_fd()
        assert runtime == lock.runtime
    finally:
        lock.release()
    assert lock.acquired is False
    assert not (tmp_vault / ".vault-meta" / "capture" / "queue.lock").exists()


def test_capture_queue_enqueue_and_list_compatible(
    monkeypatch, tmp_vault, force_compatible_tier
):
    _install_fake_win32(monkeypatch)
    # load_adapter_manifest reads a config/adapters.json that isn't present
    # in this checkout (unrelated to the Windows port) -- stub it directly
    # rather than depending on that file existing.
    import engine.capture as capture_module

    manifest = {
        "schema": capture_module.ADAPTER_SCHEMA,
        "adapters": [
            {
                "id": adapter_id,
                "maturity": "implemented" if adapter_id == "filesystem" else "metadata-only",
                "execution": "internal" if adapter_id == "filesystem" else "external",
                "destructive": False,
            }
            for adapter_id in ("filesystem", "url", "image", "pdf", "youtube", "epub", "ocr")
        ],
    }
    monkeypatch.setattr(capture_module, "load_adapter_manifest", lambda path=None: manifest)

    queue = CaptureQueue(tmp_vault)
    entry = queue.enqueue("filesystem", "/tmp/some/source.txt")
    assert entry["adapter"] == "filesystem"

    listed = queue.list()
    assert len(listed) == 1
    assert listed[0]["id"] == entry["id"]


def test_capture_queue_lock_refuses_without_opt_in(monkeypatch, tmp_vault):
    import engine.transaction as transaction_module
    from engine.hostplatform.capability import compatible_capability

    monkeypatch.setattr(
        transaction_module, "capability_for", lambda vault_root: compatible_capability()
    )
    monkeypatch.delenv(_WINDOWS_WRITE_OPT_IN_ENV_VAR, raising=False)
    from engine.capture import CaptureValidationError

    with pytest.raises(CaptureValidationError) as excinfo:
        CaptureQueueLock(tmp_vault)
    assert excinfo.value.code == "UNSUPPORTED_PLATFORM"
