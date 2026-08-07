"""Portable core for codex-brain.

The package deliberately has no third-party runtime dependencies on POSIX
platforms (WSL/Linux/macOS). Native Windows vault writes additionally
require `pywin32`, imported lazily by hostplatform.windows_backend and only
for mutating operations -- see docs/windows-wsl.md. Host-specific plugins
and skills call this core instead of deriving mutable vault state from the
plugin installation directory.
"""

from __future__ import annotations

__version__ = "2.3.0"
