class SolarEdgeError(Exception):
    """Base class for SolarEdge client errors."""


class BudgetExceeded(SolarEdgeError):
    """The monthly credit budget does not allow this call."""


class RateLimited(SolarEdgeError):
    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None,
                 month_exhausted: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.month_exhausted = month_exhausted


class AuthExpired(SolarEdgeError):
    """Terminal: the owner must re-authorize."""


class TransientAuthError(SolarEdgeError):
    """Temporary failure while authenticating; retry later."""


class PermissionDenied(SolarEdgeError):
    """HTTP 403."""


class NotFound(SolarEdgeError):
    """HTTP 404."""


class ApiError(SolarEdgeError):
    def __init__(self, status: int, message: str | None = None) -> None:
        super().__init__(message or f"SolarEdge API error {status}")
        self.status = status
