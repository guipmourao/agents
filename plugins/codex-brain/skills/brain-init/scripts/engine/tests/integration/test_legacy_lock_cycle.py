"""End-to-end coverage of legacy_lock.py's ported directory-walk primitives.

Runs on real filesystem state (no mocking) to prove the hostplatform.dirops
rewire preserved behavior on POSIX. acquire/release/clear-stale still go
through MutationLock (transaction.py, POSIX-only today), so this suite only
runs where dir_fd confinement is available -- Windows coverage for the
write-side commands arrives with transaction.py's port (plan phase 4); the
read-side primitives (_open_root/_open_child_directory/_validate_target_path)
this task actually ported are exercised indirectly through `list`/`peek`,
which this suite also covers and which *do* now work end-to-end on Windows
once pywin32 is available -- that half just can't be proven on this Linux CI
host.
"""

from __future__ import annotations

import pytest

from engine import legacy_lock


pytestmark = pytest.mark.usefixtures("posix_only")


def test_acquire_then_release_cycle(tmp_vault, capsys):
    assert legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60) == 0
    capsys.readouterr()

    assert legacy_lock._peek(tmp_vault, "notes/today.md") == 0
    assert "unheld" not in capsys.readouterr().out

    assert legacy_lock._release(tmp_vault, "notes/today.md") == 0
    capsys.readouterr()

    assert legacy_lock._peek(tmp_vault, "notes/today.md") == 0
    assert "unheld" in capsys.readouterr().out


def test_acquire_twice_without_release_is_held(tmp_vault):
    assert legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60) == 0
    assert legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60) == 75


def test_acquire_after_stale_timeout_succeeds(tmp_vault, monkeypatch):
    real_time = legacy_lock.time.time
    monkeypatch.setattr(legacy_lock.time, "time", lambda: real_time() - 120)
    assert legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60) == 0
    monkeypatch.setattr(legacy_lock.time, "time", real_time)
    assert legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60) == 0


def test_list_reports_held_lock(tmp_vault, capsys):
    legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60)
    capsys.readouterr()
    assert legacy_lock._list(tmp_vault) == 0
    assert "notes/today.md" in capsys.readouterr().out


def test_clear_stale_removes_old_locks(tmp_vault, monkeypatch, capsys):
    real_time = legacy_lock.time.time
    monkeypatch.setattr(legacy_lock.time, "time", lambda: real_time() - 3700)
    legacy_lock._acquire(tmp_vault, "notes/today.md", stale_after=60)
    monkeypatch.setattr(legacy_lock.time, "time", real_time)
    capsys.readouterr()
    assert legacy_lock._clear_stale(tmp_vault, max_age=3600) == 0
    assert capsys.readouterr().out.strip() == "1"
    assert legacy_lock._peek(tmp_vault, "notes/today.md") == 0
    assert "unheld" in capsys.readouterr().out


def test_validate_target_path_rejects_symlink_traversal(tmp_vault):
    outside = tmp_vault.parent / "outside"
    outside.mkdir()
    (tmp_vault / "escape").symlink_to(outside)
    with pytest.raises(legacy_lock.LegacyLockError):
        legacy_lock._validate_target_path(tmp_vault, "escape/file.md")


def test_peek_on_never_acquired_path_is_unheld(tmp_vault, capsys):
    capsys.readouterr()
    assert legacy_lock._peek(tmp_vault, "notes/never.md") == 0
    assert "unheld" in capsys.readouterr().out
