"""LeadMind — AI lead intelligence and qualification engine."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - depends on how the package was installed
    __version__ = version("leadmind")
except PackageNotFoundError:  # editable checkout without metadata
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
