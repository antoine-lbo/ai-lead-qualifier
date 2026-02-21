"""
Centralized error handling middleware for the Lead Qualifier API.

Provides structured error responses, request ID tracking,
error logging with context, and Sentry integration.
"""

import logging
import traceback
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


# ---------- Error Codes ----------


class ErrorCode(str, Enum):
    """Application-specific error codes for client handling."""

    # Authentication & Authorization
    UNAUTHORIZED = "AUTH_001"
    FORBIDDEN = "AUTH_002"
    TOKEN_EXPIRED = "AUTH_003"
    INVALID_API_KEY = "AUTH_004"

    # Validation
    VALIDATION_ERROR = "VAL_001"
    INVALID_EMAIL = "VAL_002"
    INVALID_LEAD_DATA = "VAL_003"
    MISSING_REQUIRED_FIELD = "VAL_004"

    # Enrichment
    ENRICHMENT_FAILED = "ENR_001"
    ENRICHMENT_TIMEOUT = "ENR_002"
    ENRICHMENT_RATE_LIMITED = "ENR_003"
    ENRICHMENT_PROVIDER_DOWN = "ENR_004"

    # Qualification
    QUALIFICATION_FAILED = "QUAL_001"
    OPENAI_ERROR = "QUAL_002"
    SCORING_ERROR = "QUAL_003"

    # External Services
    CRM_SYNC_FAILED = "EXT_001"
    SLACK_NOTIFICATION_FAILED = "EXT_002"
    DATABASE_ERROR = "EXT_003"
    REDIS_ERROR = "EXT_004"

    # Rate Limiting
    RATE_LIMITED = "RATE_001"
    QUOTA_EXCEEDED = "RATE_002"

    # General
    INTERNAL_ERROR = "GEN_001"
    NOT_FOUND = "GEN_002"
    SERVICE_UNAVAILABLE = "GEN_003"
    BAD_REQUEST = "GEN_004"

# ---------- Error Response Model ----------


class ErrorDetail(BaseModel):
    """Individual error detail for validation errors."""

    field: Optional[str] = None
    message: str
    code: Optional[str] = None


class ErrorResponse(BaseModel):
    """Standardized error response format."""

    error: bool = True
    code: str
    message: str
    details: Optional[list[ErrorDetail]] = None
    request_id: str
    timestamp: str
    path: Optional[str] = None
    documentation_url: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "error": True,
                "code": "VAL_001",
                "message": "Validation error in request body",
                "details": [
                    {"field": "email", "message": "Invalid email format", "code": "VAL_002"}
                ],
                "request_id": "req_abc123",
                "timestamp": "2024-01-15T10:30:00Z",
                "path": "/api/qualify",
            }
        }


# ---------- Custom Exceptions ----------


class AppException(Exception):
    """Base application exception with structured error info."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: Optional[list[ErrorDetail]] = None,
        context: Optional[dict[str, Any]] = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details
        self.context = context or {}
        super().__init__(message)


class EnrichmentException(AppException):
    """Raised when lead enrichment fails."""

    def __init__(self, message: str, provider: str = "unknown", **kwargs):
        super().__init__(
            message=message,
            code=ErrorCode.ENRICHMENT_FAILED,
            status_code=status.HTTP_502_BAD_GATEWAY,
            context={"provider": provider, **kwargs},
        )


class QualificationException(AppException):
    """Raised when AI qualification fails."""

    def __init__(self, message: str, lead_id: Optional[str] = None, **kwargs):
        super().__init__(
            message=message,
            code=ErrorCode.QUALIFICATION_FAILED,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            context={"lead_id": lead_id, **kwargs},
        )


class RateLimitException(AppException):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str = "Rate limit exceeded", retry_after: int = 60):
        super().__init__(
            message=message,
            code=ErrorCode.RATE_LIMITED,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            context={"retry_after": retry_after},
        )


class ExternalServiceException(AppException):
    """Raised when an external service call fails."""

    def __init__(self, service: str, message: str, **kwargs):
        super().__init__(
            message=f"{service} error: {message}",
            code=ErrorCode.SERVICE_UNAVAILABLE,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            context={"service": service, **kwargs},
        )

# ---------- Error Handlers ----------


def _build_error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Optional[list[ErrorDetail]] = None,
    headers: Optional[dict] = None,
) -> JSONResponse:
    """Build a standardized error JSON response."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    body = ErrorResponse(
        code=code,
        message=message,
        details=details,
        request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        path=str(request.url.path),
    )

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=headers,
    )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handle custom application exceptions."""
    logger.error(
        "Application error: %s [%s]",
        exc.code.value,
        exc.status_code,
        extra={
            "error_code": exc.code.value,
            "status_code": exc.status_code,
            "context": exc.context,
            "request_id": getattr(request.state, "request_id", None),
        },
    )

    headers = {}
    if exc.code == ErrorCode.RATE_LIMITED:
        headers["Retry-After"] = str(exc.context.get("retry_after", 60))

    return _build_error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code.value,
        message=exc.message,
        details=exc.details,
        headers=headers or None,
    )

async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handle standard HTTP exceptions with our format."""
    code_map = {
        400: ErrorCode.BAD_REQUEST,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        429: ErrorCode.RATE_LIMITED,
    }
    error_code = code_map.get(exc.status_code, ErrorCode.INTERNAL_ERROR)

    return _build_error_response(
        request=request,
        status_code=exc.status_code,
        code=error_code.value,
        message=str(exc.detail),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors with field-level detail."""
    details = []
    for error in exc.errors():
        field = " -> ".join(str(loc) for loc in error["loc"])
        details.append(
            ErrorDetail(
                field=field,
                message=error["msg"],
                code=error.get("type", "validation_error"),
            )
        )

    logger.warning(
        "Validation error on %s: %d field(s)",
        request.url.path,
        len(details),
    )

    return _build_error_response(
        request=request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code=ErrorCode.VALIDATION_ERROR.value,
        message=f"Validation failed: {len(details)} error(s) in request",
        details=details,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions. Logs full traceback."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    logger.critical(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=True,
        extra={
            "request_id": request_id,
            "traceback": traceback.format_exc(),
        },
    )

    return _build_error_response(
        request=request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR.value,
        message="An unexpected error occurred. Please try again later.",
    )

# ---------- Request ID Middleware ----------


from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns a unique request ID to every incoming request."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", f"req_{uuid.uuid4().hex[:12]}")
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs incoming requests and response status codes."""

    async def dispatch(self, request: Request, call_next):
        import time

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        log_level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            log_level,
            "%s %s -> %s (%sms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

        return response


# ---------- Setup ----------


def setup_error_handling(app: FastAPI) -> None:
    """Register all error handlers and middleware on the FastAPI app.

    Usage:
        app = FastAPI()
        setup_error_handling(app)
    """
    # Middleware (order matters — first added = outermost)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # Exception handlers
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    logger.info("Error handling middleware registered")
