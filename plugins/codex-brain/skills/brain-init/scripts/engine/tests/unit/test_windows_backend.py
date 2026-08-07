"""Unit tests for hostplatform.windows_backend against a mocked pywin32.

pywin32 is not installed on this development host (Linux), and never will
be for the general test suite -- these tests mock ``win32file``/``win32con``/
``pywintypes``/``winerror`` to validate our own control flow and error
mapping given an assumed API response shape. They do NOT prove the assumed
shape is correct; only the Windows CI job (docs/windows-wsl.md) running on
real ``windows-latest`` proves that, per the plan's rollout rigor.
"""

from __future__ import annotations

import sys
import types

import pytest

from engine.hostplatform import windows_backend as wb


class PywinError(Exception):
    """Stand-in for pywintypes.error -- the type real win32 API failures
    (lock contention, access denied, ...) raise. try_acquire_exclusive
    deliberately catches only this, not bare Exception, so tests must raise
    this type to simulate contention rather than a generic OSError."""


class _FakeHandle:
    def __init__(self):
        self.closed = False

    def Close(self):
        self.closed = True


def _install_fake_win32(monkeypatch, *, create_file_raises=None, get_info_raises=None):
    handle = _FakeHandle()

    def _create_file(*args, **kwargs):
        if create_file_raises is not None:
            raise create_file_raises
        return handle

    def _get_file_information_by_handle(h):
        if get_info_raises is not None:
            raise get_info_raises
        # (attrs, ctime, atime, mtime, volSerial, sizeHigh, sizeLow, nlinks, idxHigh, idxLow)
        return (0, None, None, None, 111, 0, 0, 1, 222, 333)

    def _lock_file_ex(handle, flags, reserved, nNumberOfBytesToLock, overlapped):
        # Real pywin32 signature is 5 positional args (a single combined
        # byte-count, not separate low/high DWORDs) -- a strict arity here is
        # what would have caught the third real Windows CI run's
        # "TypeError: LockFileEx() takes exactly 5 arguments (6 given)".
        return None

    def _unlock_file_ex(handle, reserved, nNumberOfBytesToLock, overlapped):
        return None

    fake_win32file = types.SimpleNamespace(
        CreateFile=_create_file,
        GetFileInformationByHandle=_get_file_information_by_handle,
        LockFileEx=_lock_file_ex,
        UnlockFileEx=_unlock_file_ex,
        GetFinalPathNameByHandle=lambda h, flags: "\\\\?\\C:\\vault\\wiki",
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
    fake_pywintypes = types.SimpleNamespace(OVERLAPPED=lambda: object(), error=PywinError)
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "pywintypes", fake_pywintypes)
    return fake_win32file, handle


def test_missing_dependency_raises_clear_error(monkeypatch):
    monkeypatch.delitem(sys.modules, "win32file", raising=False)
    monkeypatch.setattr(
        "builtins.__import__",
        _raise_import_error_for("win32file"),
    )
    with pytest.raises(wb.MissingDependencyError):
        wb._win32file()


def _raise_import_error_for(module_name):
    real_import = __import__

    def _fake_import(name, *args, **kwargs):
        if name == module_name:
            raise ImportError(f"No module named {module_name!r}")
        return real_import(name, *args, **kwargs)

    return _fake_import


def test_open_directory_captures_identity(monkeypatch, tmp_path):
    _install_fake_win32(monkeypatch)
    result = wb.open_directory(tmp_path)
    assert result.identity.volume_serial_number == 111
    assert result.identity.file_index_high == 222
    assert result.identity.file_index_low == 333
    assert result.identity.is_stable()


def test_open_directory_closes_handle_on_identity_failure(monkeypatch, tmp_path):
    _, handle = _install_fake_win32(monkeypatch, get_info_raises=OSError("boom"))
    with pytest.raises(OSError):
        wb.open_directory(tmp_path)
    assert handle.closed is True


def test_open_directory_after_open_hook_runs_before_identity_read(monkeypatch, tmp_path):
    _install_fake_win32(monkeypatch)
    order = []

    def hook(path):
        order.append("hook")

    fake_win32file = sys.modules["win32file"]
    original_get_info = fake_win32file.GetFileInformationByHandle

    def _tracking_get_info(h):
        order.append("identity_read")
        return original_get_info(h)

    fake_win32file.GetFileInformationByHandle = _tracking_get_info

    wb.open_directory(tmp_path, _after_open_hook=hook)
    assert order == ["hook", "identity_read"]


def test_windows_identity_zero_index_is_unstable():
    identity = wb.WindowsIdentity(volume_serial_number=1, file_index_high=0, file_index_low=0)
    assert not identity.is_stable()


def test_try_acquire_exclusive_true_on_success(monkeypatch, tmp_path):
    _install_fake_win32(monkeypatch)
    handle = wb.open_directory(tmp_path)
    assert wb.try_acquire_exclusive(handle) is True


def test_try_acquire_exclusive_false_on_failure(monkeypatch, tmp_path):
    _install_fake_win32(monkeypatch)
    handle = wb.open_directory(tmp_path)
    sys.modules["win32file"].LockFileEx = lambda *a, **k: (_ for _ in ()).throw(
        PywinError("locked")
    )
    assert wb.try_acquire_exclusive(handle) is False


def test_try_acquire_exclusive_reraises_unexpected_errors(monkeypatch, tmp_path):
    """A non-pywintypes.error exception (e.g. a signature mismatch bug) must
    propagate, not be silently treated as ordinary lock contention."""

    _install_fake_win32(monkeypatch)
    handle = wb.open_directory(tmp_path)
    sys.modules["win32file"].LockFileEx = lambda *a, **k: (_ for _ in ()).throw(
        TypeError("wrong number of arguments")
    )
    with pytest.raises(TypeError):
        wb.try_acquire_exclusive(handle)


def test_final_path_for_handle_strips_extended_prefix(monkeypatch, tmp_path):
    _install_fake_win32(monkeypatch)
    handle = wb.open_directory(tmp_path)
    assert wb.final_path_for_handle(handle) == wb.Path("C:\\vault\\wiki")


def test_is_likely_cfa_block_false_when_win32com_unavailable(monkeypatch, tmp_path):
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    monkeypatch.delitem(sys.modules, "win32com.shell.shell", raising=False)
    monkeypatch.delitem(sys.modules, "win32com.shell.shellcon", raising=False)
    exc = OSError("denied")
    exc.winerror = 5
    assert wb.is_likely_cfa_block(tmp_path, exc) is False


def test_is_likely_cfa_block_false_for_unrelated_error(monkeypatch, tmp_path):
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    exc = OSError("not found")
    exc.winerror = 2
    assert wb.is_likely_cfa_block(tmp_path, exc) is False


def _install_fake_win32com(monkeypatch, *, known_folder: "Path"):
    # `import win32com.shell.shell as shell` needs the parent package's
    # sys.modules entry to expose the submodule as an attribute too (that's
    # how Python binds the "as shell" name after the submodule import) --
    # a bare sys.modules entry for the leaf isn't enough.
    fake_shellcon = types.SimpleNamespace(FOLDERID_Documents=object())
    fake_shell = types.SimpleNamespace(
        SHGetKnownFolderPath=lambda constant, flags: str(known_folder)
    )
    fake_win32com_shell_pkg = types.SimpleNamespace(shell=fake_shell, shellcon=fake_shellcon)
    fake_win32com_pkg = types.SimpleNamespace(shell=fake_win32com_shell_pkg)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com_pkg)
    monkeypatch.setitem(sys.modules, "win32com.shell", fake_win32com_shell_pkg)
    monkeypatch.setitem(sys.modules, "win32com.shell.shell", fake_shell)
    monkeypatch.setitem(sys.modules, "win32com.shell.shellcon", fake_shellcon)


def test_is_likely_cfa_block_true_for_protected_folder(monkeypatch, tmp_path):
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    documents = tmp_path / "Documents"
    vault = documents / "vault" / "wiki" / "note.md"
    vault.parent.mkdir(parents=True)
    _install_fake_win32com(monkeypatch, known_folder=documents)
    exc = OSError("denied")
    exc.winerror = 5
    assert wb.is_likely_cfa_block(vault, exc) is True


def test_remap_write_error_returns_cfa_error_when_matched(monkeypatch, tmp_path):
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    documents = tmp_path / "Documents"
    vault = documents / "vault" / "wiki" / "note.md"
    vault.parent.mkdir(parents=True)
    _install_fake_win32com(monkeypatch, known_folder=documents)
    exc = OSError("denied")
    exc.winerror = 5
    remapped = wb.remap_write_error(vault, exc)
    assert isinstance(remapped, wb.ControlledFolderAccessBlocked)
    assert "Controlled Folder Access" in str(remapped) or "Controlled folder access" in str(
        remapped
    )


def test_remap_write_error_passes_through_unrelated_error(monkeypatch, tmp_path):
    fake_winerror = types.SimpleNamespace(ERROR_ACCESS_DENIED=5)
    monkeypatch.setitem(sys.modules, "winerror", fake_winerror)
    exc = OSError("disk full")
    exc.winerror = 112
    assert wb.remap_write_error(tmp_path, exc) is exc
