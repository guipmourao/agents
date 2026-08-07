"""Coverage for MutationLock's COMPATIBLE (native Windows) tier, added in
port phase 4d.

Most of this tier's methods (_owner_compatible/_write_owner_compatible/
_remove_lock_dir_compatible/_may_reap_owner) are plain path operations with
no Windows-specific call inside them, so they run for real here on Linux --
only _acquire_compatible/_release_compatible/_close_descriptors_compatible
touch hostplatform.windows_backend and need the same mocked win32file/
win32con/pywintypes pattern as test_windows_backend.py.

This is control-flow and record-format coverage, not proof the COMPATIBLE
tier works on real Windows -- that needs the Windows CI job (task #8) and is
also moot in production today: _require_write_platform still refuses all of
native Windows unconditionally until phase 4g generalizes it.
"""

from __future__ import annotations

import socket
import sys
import time
import types
from pathlib import Path

import pytest

from engine.transaction import MutationLock, TransactionConflict, TransactionError


def test_owner_compatible_roundtrip(tmp_vault):
    lock_dir = tmp_vault / ".vault-meta" / "mutation.lock"
    lock_dir.mkdir(parents=True)
    lock = MutationLock(tmp_vault)
    assert lock._owner_compatible(lock_dir) is None
    lock._write_owner_compatible(lock_dir, {"pid": 123, "token": "abc"})
    owner = lock._owner_compatible(lock_dir)
    assert owner == {"pid": 123, "token": "abc"}


def test_remove_lock_dir_compatible(tmp_vault):
    lock_dir = tmp_vault / ".vault-meta" / "mutation.lock"
    lock_dir.mkdir(parents=True)
    lock = MutationLock(tmp_vault)
    lock._write_owner_compatible(lock_dir, {"pid": 1})
    expected = lock_dir.lstat()
    lock._remove_lock_dir_compatible(lock_dir, expected)
    assert not lock_dir.exists()


def test_remove_lock_dir_compatible_raises_on_identity_change(tmp_vault):
    lock_dir = tmp_vault / ".vault-meta" / "mutation.lock"
    lock_dir.mkdir(parents=True)
    lock = MutationLock(tmp_vault)
    expected = lock_dir.lstat()
    # Simulate a concurrent replace: swap in a directory with the same name
    # but a different underlying object, mirroring the quarantine-rename
    # pattern _acquire_compatible itself uses -- rmdir+mkdir at the same
    # path can be reallocated the same inode on some filesystems, which
    # would make this assertion flaky, so go through a decoy path instead.
    decoy = tmp_vault / ".vault-meta" / "decoy"
    decoy.mkdir()
    lock_dir.rmdir()
    decoy.rename(lock_dir)
    from engine.transaction import _LockIdentityChanged

    with pytest.raises(_LockIdentityChanged):
        lock._remove_lock_dir_compatible(lock_dir, expected)


def test_may_reap_owner_fresh_lock_not_stale(tmp_vault):
    lock = MutationLock(tmp_vault, stale_after=3600.0)
    owner = {"started_epoch": time.time(), "pid": 1, "host": "somehost"}
    assert lock._may_reap_owner(time.time(), owner, fallback_mtime=None, process_alive=lambda pid: True) is False


def test_may_reap_owner_stale_and_dead_process_is_reapable(tmp_vault):
    lock = MutationLock(tmp_vault, stale_after=1.0)
    owner = {
        "started_epoch": time.time() - 100,
        "pid": 1,
        "host": socket.gethostname(),
    }
    assert (
        lock._may_reap_owner(
            time.time(), owner, fallback_mtime=None, process_alive=lambda pid: False
        )
        is True
    )


def test_may_reap_owner_stale_but_different_host_not_reapable(tmp_vault):
    lock = MutationLock(tmp_vault, stale_after=1.0)
    owner = {"started_epoch": time.time() - 100, "pid": 1, "host": "otherhost"}
    assert (
        lock._may_reap_owner(
            time.time(), owner, fallback_mtime=None, process_alive=lambda pid: False
        )
        is False
    )


def test_may_reap_owner_force_stale_bypasses_host_and_liveness(tmp_vault):
    lock = MutationLock(tmp_vault, stale_after=1.0, force_stale_lock=True)
    owner = {"started_epoch": time.time() - 100, "pid": 1, "host": "otherhost"}
    assert (
        lock._may_reap_owner(
            time.time(), owner, fallback_mtime=None, process_alive=lambda pid: True
        )
        is True
    )


class _PywinError(Exception):
    """Stand-in for pywintypes.error, the type try_acquire_exclusive
    deliberately catches for expected lock contention (see
    windows_backend.try_acquire_exclusive's docstring) -- anything else must
    propagate instead of being mistaken for ordinary contention."""


class _FakeWinHandle:
    def __init__(self):
        self.closed = False

    def Close(self):
        self.closed = True


def _install_fake_win32(monkeypatch):
    handle = _FakeWinHandle()
    fake_win32file = types.SimpleNamespace(
        CreateFile=lambda *a, **k: handle,
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
    fake_pywintypes = types.SimpleNamespace(OVERLAPPED=lambda: object(), error=_PywinError)
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "pywintypes", fake_pywintypes)


def test_acquire_release_compatible_cycle(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock._acquire_compatible()
    try:
        assert lock.acquired is True
        assert (tmp_vault / ".vault-meta" / "mutation.lock").is_dir()
    finally:
        lock._release_compatible()
    assert lock.acquired is False
    assert not (tmp_vault / ".vault-meta" / "mutation.lock").exists()


def test_acquire_compatible_contention_times_out(monkeypatch, tmp_vault):
    _install_fake_win32(monkeypatch)
    holder = MutationLock(tmp_vault, timeout=1.0)
    holder._acquire_compatible()
    try:
        sys.modules["win32file"].LockFileEx = lambda *a, **k: (_ for _ in ()).throw(
            _PywinError("locked")
        )
        contender = MutationLock(tmp_vault, timeout=0.1, poll_interval=0.02)
        with pytest.raises(TransactionConflict):
            contender._acquire_compatible()
    finally:
        sys.modules["win32file"].LockFileEx = lambda *a, **k: None
        holder._release_compatible()


def test_acquire_compatible_surfaces_cfa_error_on_meta_mkdir(monkeypatch, tmp_vault):
    """A Controlled-Folder-Access-blocked mkdir on .vault-meta must surface
    as an actionable ControlledFolderAccessBlocked-carrying error, not a bare
    PermissionError -- exercises the phase-7 wiring end to end."""

    _install_fake_win32(monkeypatch)
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    documents = tmp_vault.parent / "Documents"
    documents.mkdir()
    protected_vault = documents / "vault"
    protected_vault.mkdir()

    fake_shellcon = types.SimpleNamespace(FOLDERID_Documents=object())
    fake_shell = types.SimpleNamespace(
        SHGetKnownFolderPath=lambda constant, flags: str(documents)
    )
    fake_shell_pkg = types.SimpleNamespace(shell=fake_shell, shellcon=fake_shellcon)
    fake_win32com_pkg = types.SimpleNamespace(shell=fake_shell_pkg)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com_pkg)
    monkeypatch.setitem(sys.modules, "win32com.shell", fake_shell_pkg)
    monkeypatch.setitem(sys.modules, "win32com.shell.shell", fake_shell)
    monkeypatch.setitem(sys.modules, "win32com.shell.shellcon", fake_shellcon)

    real_mkdir = Path.mkdir

    def _blocked_mkdir(self, *args, **kwargs):
        if self.name == ".vault-meta":
            exc = PermissionError("Access is denied")
            exc.winerror = 5
            raise exc
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _blocked_mkdir)

    from engine.hostplatform.windows_backend import ControlledFolderAccessBlocked

    lock = MutationLock(protected_vault, timeout=1.0)
    with pytest.raises(TransactionError) as excinfo:
        lock._acquire_compatible()
    assert "Controlled" in str(excinfo.value)
