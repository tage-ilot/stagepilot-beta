"""Safe, typed failures raised by the Planning Center API boundary."""

from __future__ import annotations


class PlanningCenterError(Exception):
    """Base class for failures safe to surface without response bodies or secrets."""


class PlanningCenterConfigurationError(PlanningCenterError):
    """Required Planning Center configuration is absent or invalid."""


class PlanningCenterAuthenticationError(PlanningCenterError):
    """Planning Center rejected the configured Personal Access Token."""


class PlanningCenterPermissionError(PlanningCenterError):
    """The authenticated Planning Center user lacks access to the resource."""


class PlanningCenterTimeoutError(PlanningCenterError):
    """Planning Center did not respond within the configured timeout."""


class PlanningCenterTransportError(PlanningCenterError):
    """The request could not reach Planning Center."""


class PlanningCenterResponseError(PlanningCenterError):
    """Planning Center returned data that did not match the documented contract."""


class PlanningCenterPlanSelectionError(PlanningCenterError):
    """A requested plan is not one of the nearest-date candidates."""


class PlanningCenterLengthConflictError(PlanningCenterError):
    """The item changed while a rate-limited write was waiting."""


class PlanningCenterApiError(PlanningCenterError):
    """Planning Center returned an unsuccessful HTTP response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Planning Center returned HTTP {status_code}.")


class PlanningCenterRateLimitError(PlanningCenterApiError):
    """Planning Center asked the client to pause before making more requests."""

    def __init__(self, retry_after_seconds: int | None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(429)
