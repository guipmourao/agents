"""Regression test for a real bug the first windows-latest CI run caught
(not something found by review or by mocking win32 -- a genuine drift
between two functions that agreed on POSIX by construction but not on
Windows).

_prepare_writes recorded new_mode=0o600 for a freshly created file (no
prior original_mode to preserve), but _portable_file_mode -- the same
function used to verify a completed write -- unconditionally reports
0o644 for any file once os.name == "nt" (the Windows CRT synthesizes
permission bits regardless of what chmod/fchmod actually requested). Every
create-mode write's own post-apply RESULT_DRIFT check failed against
itself as a result: transaction.py:4970's apply_bundle call in a real
windows-latest job hit "completed operation path drifted ... expected
mode=0600 ... found mode=420 [0o644]" on the very first real run.

This can't be exercised end-to-end via apply_bundle with a monkeypatched
os.name on a POSIX dev host: pathlib.Path's __new__ dispatches to
WindowsPath/PosixPath based on os.name at instantiation time, so faking it
breaks Path resolution itself (NotImplementedError: cannot instantiate
WindowsPath on your system) before apply_bundle's own logic ever runs.
Testing the two pure functions directly, which is all this bug actually
involved, sidesteps that entirely.
"""

from __future__ import annotations

import stat as stat_module

import pytest

from engine.transaction import _default_new_file_mode, _portable_file_mode


def test_default_new_file_mode_matches_portable_file_mode_on_windows(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    default_mode = _default_new_file_mode()
    # Whatever raw bits a real Windows CRT might report (0o666 or 0o444 per
    # _portable_file_mode's own docstring) must normalize to the exact value
    # recorded as this file's expected new_mode.
    for raw_bits in (0o666, 0o444):
        st_mode = stat_module.S_IFREG | raw_bits
        assert _portable_file_mode(st_mode) == default_mode


def test_default_new_file_mode_matches_portable_file_mode_on_posix(monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    default_mode = _default_new_file_mode()
    st_mode = stat_module.S_IFREG | default_mode
    assert _portable_file_mode(st_mode) == default_mode


def test_default_new_file_mode_is_0o644_on_windows(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    assert _default_new_file_mode() == 0o644


def test_default_new_file_mode_is_0o600_on_posix(monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    assert _default_new_file_mode() == 0o600
