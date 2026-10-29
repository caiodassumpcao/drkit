"""drkit: backup and disaster recovery for Docker Compose servers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("drkit")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
