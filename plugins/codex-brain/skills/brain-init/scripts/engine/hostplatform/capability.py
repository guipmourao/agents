"""The write-safety capability contract.

Every mutating operation resolves one :class:`PlatformCapability` for its
vault root and stamps its :class:`GuaranteeTier` into the transaction journal
alongside the existing approval hash, so a written transaction is
self-describing about what guarantee level produced it. See
``paths.capability_for`` for how a capability is resolved, and
``docs/windows-wsl.md`` for the tier boundary explained in user-facing terms.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class GuaranteeTier(enum.Enum):
    """How strongly writes to a vault are protected against a path being
    swapped out from under the engine mid-operation (a replaced symlink or
    junction, a directory replaced by a different one with the same name)."""

    #: POSIX dir_fd confinement: the vault root and every runtime directory
    #: stay pinned for the whole write by kernel-enforced directory
    #: descriptors. A concurrently swapped alias cannot redirect the write.
    STRICT = "strict"

    #: Native Windows: each path component is opened by full path and its
    #: identity verified immediately afterward (``FileIdInfo`` plus a
    #: reparse-tag check), narrowing the TOCTOU window from "anywhere during
    #: the walk" to "between one open and its immediately following check"
    #: rather than eliminating it via kernel-pinned handles.
    COMPATIBLE = "compatible"

    #: The filesystem cannot offer stable-enough file identity (FAT/exFAT,
    #: some SMB redirectors) or hasn't been vetted (unclassified network
    #: shares). Writes are refused outright.
    UNSAFE_REFUSED = "unsafe_refused"


@dataclass(frozen=True)
class PlatformCapability:
    #: True only for STRICT: the OS itself pins directory identity via a
    #: descriptor for the life of the operation.
    descriptor_confined: bool
    #: True for STRICT and COMPATIBLE: paths resolve to the intended target,
    #: verified either by descriptor pinning or by an immediate post-open check.
    path_confined: bool
    #: False on filesystems without stable file identity (zero inode on
    #: FAT/exFAT, degenerate FileId on some SMB redirectors).
    stable_identity: bool
    #: False on Windows: the CRT synthesizes permission bits, so callers must
    #: not assert mode equality as part of correctness there.
    mode_verified: bool
    #: True once symlink/junction (name-surrogate) checks have passed for
    #: every path component involved.
    reparse_safe: bool
    tier: GuaranteeTier


def strict_capability() -> PlatformCapability:
    return PlatformCapability(
        descriptor_confined=True,
        path_confined=True,
        stable_identity=True,
        mode_verified=True,
        reparse_safe=True,
        tier=GuaranteeTier.STRICT,
    )


def compatible_capability(
    *, stable_identity: bool = True, reparse_safe: bool = True
) -> PlatformCapability:
    return PlatformCapability(
        descriptor_confined=False,
        path_confined=True,
        stable_identity=stable_identity,
        mode_verified=False,
        reparse_safe=reparse_safe,
        tier=GuaranteeTier.COMPATIBLE,
    )


def unsafe_refused_capability() -> PlatformCapability:
    return PlatformCapability(
        descriptor_confined=False,
        path_confined=False,
        stable_identity=False,
        mode_verified=False,
        reparse_safe=False,
        tier=GuaranteeTier.UNSAFE_REFUSED,
    )
