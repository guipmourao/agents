"""Platform-specific write primitives, behind one capability contract.

``paths.capability_for`` is the single entry point callers should use to
find out what guarantee tier a vault root gets on the current host — it
dispatches internally to :mod:`hostplatform.posix_backend` or
:mod:`hostplatform.windows_backend` plus :mod:`hostplatform.fsclassify`. Nothing in
``transaction.py``/``legacy_lock.py``/``checkpoint.py``/``capture.py`` should
import :mod:`win32file` (or check ``os.name`` for primitive selection)
directly — that dispatch belongs here so it stays in one place.
"""

from __future__ import annotations

from .capability import GuaranteeTier, PlatformCapability

__all__ = ["GuaranteeTier", "PlatformCapability"]
