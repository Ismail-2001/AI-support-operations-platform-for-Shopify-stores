"""Structured API error responses.

Every error returned by the API follows this shape so clients can programmatically
handle specific failure modes instead of parsing free-text detail strings.

Usage:
    raise APIError(code="TICKET_NOT_FOUND", message="Ticket xyz not found", status=404)
"""

from fastapi import HTTPException


class APIError(HTTPException):
    """HTTP exception with a machine-readable error code.

    The `code` field is a stable, snake_case identifier (e.g. TICKET_NOT_FOUND,
    RATE_LIMIT_EXCEEDED) that clients can switch on. The `message` field is
    human-readable and may change between versions.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: dict | None = None,
    ):
        self.error_code = code
        self.error_details = details
        super().__init__(status_code=status, detail=message)


def raise_not_found(resource: str, identifier: str) -> None:
    raise APIError(
        code=f"{resource.upper()}_NOT_FOUND",
        message=f"{resource} '{identifier}' not found",
        status=404,
    )


def raise_validation_error(field: str, message: str) -> None:
    raise APIError(
        code="VALIDATION_ERROR",
        message=f"Invalid value for '{field}': {message}",
        status=422,
        details={"field": field},
    )


def raise_rate_limited(limit: int) -> None:
    raise APIError(
        code="RATE_LIMIT_EXCEEDED",
        message=f"Rate limit exceeded ({limit}/minute). Try again shortly.",
        status=429,
    )


def raise_not_configured(integration: str) -> None:
    raise APIError(
        code=f"{integration.upper()}_NOT_CONFIGURED",
        message=f"{integration} is not configured",
        status=400,
    )


def raise_unprocessable(message: str) -> None:
    raise APIError(code="UNPROCESSABLE_ENTITY", message=message, status=422)
