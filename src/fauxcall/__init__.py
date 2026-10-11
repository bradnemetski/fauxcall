"""FauxCall: record real APIs and MCP servers, then serve high-fidelity fakes for agent testing."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fauxcall")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
