"""Coverage for transaction.py's lock-owner-record primitives, moved into
hostplatform.posix_backend in port phase 4c. POSIX-only."""

from __future__ import annotations

import os

import pytest

from engine.hostplatform import posix_backend

pytestmark = pytest.mark.usefixtures("posix_only")


@pytest.fixture
def parent_and_lock_fd(tmp_vault):
    parent_fd = posix_backend.open_lock_parent_fd(tmp_vault, (".vault-meta",), create=True)
    os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
    lock_fd = posix_backend.open_lock_directory_at(parent_fd, "mutation.lock")
    try:
        yield parent_fd, lock_fd
    finally:
        os.close(lock_fd)
        os.close(parent_fd)


def test_read_lock_owner_at_missing_returns_none(parent_and_lock_fd):
    _, lock_fd = parent_and_lock_fd
    assert posix_backend.read_lock_owner_at(lock_fd) is None


def test_write_then_read_lock_owner_roundtrip(parent_and_lock_fd):
    _, lock_fd = parent_and_lock_fd
    posix_backend.write_lock_owner_at(lock_fd, b'{"pid": 123}')
    assert posix_backend.read_lock_owner_at(lock_fd) == b'{"pid": 123}'


def test_read_lock_owner_at_oversized_returns_none(parent_and_lock_fd):
    _, lock_fd = parent_and_lock_fd
    posix_backend.write_lock_owner_at(lock_fd, b"x" * 100)
    assert posix_backend.read_lock_owner_at(lock_fd, limit=10) is None


def test_lock_entry_matches_true_for_pinned_directory(parent_and_lock_fd):
    parent_fd, lock_fd = parent_and_lock_fd
    assert posix_backend.lock_entry_matches(parent_fd, "mutation.lock", lock_fd) is True


def test_lock_entry_matches_false_after_swap(tmp_vault):
    parent_fd = posix_backend.open_lock_parent_fd(tmp_vault, (".vault-meta",), create=True)
    try:
        os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
        lock_fd = posix_backend.open_lock_directory_at(parent_fd, "mutation.lock")
        try:
            os.rmdir("mutation.lock", dir_fd=parent_fd)
            os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
            assert posix_backend.lock_entry_matches(parent_fd, "mutation.lock", lock_fd) is False
        finally:
            os.close(lock_fd)
    finally:
        os.close(parent_fd)


def test_remove_lock_directory_at_removes_pinned_directory(tmp_vault):
    parent_fd = posix_backend.open_lock_parent_fd(tmp_vault, (".vault-meta",), create=True)
    try:
        os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
        lock_fd = posix_backend.open_lock_directory_at(parent_fd, "mutation.lock")
        posix_backend.remove_lock_directory_at(parent_fd, "mutation.lock", lock_fd)
        os.close(lock_fd)
        assert not (tmp_vault / ".vault-meta" / "mutation.lock").exists()
    finally:
        os.close(parent_fd)


def test_remove_lock_directory_at_raises_on_identity_change(tmp_vault):
    parent_fd = posix_backend.open_lock_parent_fd(tmp_vault, (".vault-meta",), create=True)
    try:
        os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
        lock_fd = posix_backend.open_lock_directory_at(parent_fd, "mutation.lock")
        try:
            os.rmdir("mutation.lock", dir_fd=parent_fd)
            os.mkdir("mutation.lock", 0o700, dir_fd=parent_fd)
            with pytest.raises(posix_backend.LockIdentityChanged):
                posix_backend.remove_lock_directory_at(parent_fd, "mutation.lock", lock_fd)
        finally:
            os.close(lock_fd)
    finally:
        os.close(parent_fd)
