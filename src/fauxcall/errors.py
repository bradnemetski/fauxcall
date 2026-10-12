"""Exception hierarchy for FauxCall."""

from __future__ import annotations


class FauxcallError(Exception):
    """Base class for every error FauxCall raises on purpose."""


class ConfigurationError(FauxcallError):
    """A setting, model file or workspace is missing or invalid."""


class SpecError(FauxcallError):
    """An API specification could not be fetched, parsed or understood."""


class RecordingError(FauxcallError):
    """A recording plan could not be executed."""


class HttpStatusError(RecordingError):
    """The server answered a protocol step with an HTTP error status."""

    def __init__(self, message: str, status: int, www_authenticate: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.www_authenticate = www_authenticate


class ApprovalRequiredError(RecordingError):
    """A mutating call was attempted without an up-front approval."""


class ModelError(FauxcallError):
    """The API model is inconsistent (e.g. a relation points at a missing collection)."""


class WorldError(FauxcallError):
    """The world data violates an invariant or cannot be loaded."""
