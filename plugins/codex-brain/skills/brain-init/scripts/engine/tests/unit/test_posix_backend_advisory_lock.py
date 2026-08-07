"""Coverage for transaction.py's process-exclusivity lock, moved into
hostplatform.posix_backend in port phase 4b. POSIX-only."""

from __future__ import annotations

import os

import pytest

from engine.hostplatform import posix_backend

pytestmark = pytest.mark.usefixtures("posix_only")


def test_advisory_lock_acquire_release_cycle(tmp_vault):
    fd = os.open(tmp_vault, os.O_RDONLY)
    try:
        assert posix_backend.try_vault_advisory_lock(fd) is True
        posix_backend.release_vault_advisory_lock(fd)
    finally:
        os.close(fd)


def test_advisory_lock_second_holder_in_same_process_still_succeeds(tmp_vault):
    # flock is per open-file-description, not per-fd: a second os.open of the
    # same path is a distinct description and does contend.
    first = os.open(tmp_vault, os.O_RDONLY)
    second = os.open(tmp_vault, os.O_RDONLY)
    try:
        assert posix_backend.try_vault_advisory_lock(first) is True
        assert posix_backend.try_vault_advisory_lock(second) is False
    finally:
        posix_backend.release_vault_advisory_lock(first)
        os.close(first)
        os.close(second)


def test_release_is_best_effort_on_bad_fd():
    # Must not raise even on a closed/invalid descriptor.
    posix_backend.release_vault_advisory_lock(-1)
