"""End-to-end coverage of transaction.MutationLock's STRICT (POSIX) tier,
after port phase 4d split acquire()/release() into a tier dispatcher plus
_acquire_strict/_release_strict. Proves the rename didn't change POSIX
behavior; the COMPATIBLE (Windows) tier this phase also added is exercised
only by mocked unit coverage (see test_windows_backend.py) -- it cannot run
for real here and stays unreachable in production until phase 4g."""

from __future__ import annotations

import json
import time

import pytest

from engine.transaction import MutationLock, TransactionConflict

pytestmark = pytest.mark.usefixtures("posix_only")


def test_acquire_release_cycle(tmp_vault):
    lock = MutationLock(tmp_vault, timeout=1.0)
    lock.acquire()
    try:
        assert lock.acquired is True
        lock_dir = tmp_vault / ".vault-meta" / "mutation.lock"
        assert lock_dir.is_dir()
        owner = json.loads((lock_dir / "owner.json").read_text())
        assert owner["token"] == lock.token
    finally:
        lock.release()
    assert lock.acquired is False
    assert not (tmp_vault / ".vault-meta" / "mutation.lock").exists()


def test_context_manager_cycle(tmp_vault):
    with MutationLock(tmp_vault, timeout=1.0) as lock:
        assert lock.acquired is True
    assert lock.acquired is False


def test_contended_lock_times_out(tmp_vault):
    holder = MutationLock(tmp_vault, timeout=1.0)
    holder.acquire()
    try:
        contender = MutationLock(tmp_vault, timeout=0.2, poll_interval=0.05)
        with pytest.raises(TransactionConflict):
            contender.acquire()
    finally:
        holder.release()


def test_stale_lock_is_reaped_with_force_flag(tmp_vault):
    holder = MutationLock(tmp_vault, timeout=1.0, stale_after=0.05)
    holder.acquire()
    lock_dir = tmp_vault / ".vault-meta" / "mutation.lock"
    owner_path = lock_dir / "owner.json"
    owner = json.loads(owner_path.read_text())
    owner["started_epoch"] = time.time() - 10
    owner_path.write_text(json.dumps(owner))
    # Drop the process-lifetime advisory lock without going through release()
    # so the record looks like a crashed holder's leftover, not a live one.
    holder._close_descriptors_strict()

    reaper = MutationLock(
        tmp_vault, timeout=1.0, stale_after=0.05, force_stale_lock=True
    )
    reaper.acquire()
    try:
        assert reaper.acquired is True
    finally:
        reaper.release()


def test_duplicate_parent_fd_unavailable_outside_held_lock(tmp_vault):
    lock = MutationLock(tmp_vault, timeout=1.0)
    with pytest.raises(Exception):
        lock.duplicate_parent_fd()
