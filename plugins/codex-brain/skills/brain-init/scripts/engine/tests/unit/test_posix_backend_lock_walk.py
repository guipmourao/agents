"""Coverage for the transaction.py runtime-directory-chain walk moved into
hostplatform.posix_backend in port phase 4a. POSIX-only: this is exactly the
code path that stays unchanged (still gated by supports_transaction_lock_dirfd)
until phase 4g."""

from __future__ import annotations

import os

import pytest

from engine.hostplatform import posix_backend

pytestmark = pytest.mark.usefixtures("posix_only")


def test_supports_transaction_lock_dirfd_true_on_linux():
    assert posix_backend.supports_transaction_lock_dirfd() is True


def test_open_lock_root_fd_returns_directory_descriptor(tmp_vault):
    fd = posix_backend.open_lock_root_fd(tmp_vault)
    try:
        import stat as stat_module

        assert stat_module.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def test_open_lock_parent_fd_creates_chain(tmp_vault):
    fd = posix_backend.open_lock_parent_fd(
        tmp_vault, (".vault-meta", "locks"), create=True
    )
    try:
        import stat as stat_module

        assert stat_module.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    assert (tmp_vault / ".vault-meta" / "locks").is_dir()


def test_open_lock_parent_fd_without_create_fails_when_missing(tmp_vault):
    with pytest.raises(FileNotFoundError):
        posix_backend.open_lock_parent_fd(
            tmp_vault, (".vault-meta", "does-not-exist"), create=False
        )


def test_open_lock_parent_from_root_fd_reuses_existing_chain(tmp_vault):
    first = posix_backend.open_lock_parent_fd(
        tmp_vault, (".vault-meta", "locks"), create=True
    )
    os.close(first)
    root_fd = posix_backend.open_lock_root_fd(tmp_vault)
    try:
        chained = posix_backend.open_lock_parent_from_root_fd(
            root_fd, (".vault-meta", "locks"), create=False
        )
        os.close(chained)
    finally:
        os.close(root_fd)
