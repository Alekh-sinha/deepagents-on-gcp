"""Errors the engine raises on purpose; the API maps each to an HTTP status (NotFound 404, Forbidden 403, Busy 429, Conflict 409, BadRequest 400)."""
from __future__ import annotations


class ServiceError(Exception):
    """Base class for errors the API maps to HTTP status codes."""


class NotFound(ServiceError):
    pass


class Conflict(ServiceError):
    pass


class Forbidden(ServiceError):
    """The caller is authenticated but this user is not allowed to use the assistant."""


class Busy(Conflict):
    """Too many turns are running; the caller should retry shortly (a new turn is refused, never queued)."""


class BadRequest(ServiceError):
    pass
