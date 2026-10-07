from __future__ import annotations


class ServiceError(Exception):
    """A business-rule failure with a stable code and a message safe to show
    to the caller (the voice agent reads `say` aloud-ready guidance)."""

    status = 400

    def __init__(self, code: str, message: str, *, say: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.say = say


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Unauthorized(ServiceError):
    status = 401


class Unavailable(ServiceError):
    status = 503
