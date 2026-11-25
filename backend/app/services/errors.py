class LedgerError(Exception):
    """Base class; `message` is safe to show to users."""
    status_code = 422

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(LedgerError):
    status_code = 422


class NotFoundError(LedgerError):
    status_code = 404


class ConflictError(LedgerError):
    """State changed under the caller (stale view, duplicate, concurrent writer)."""
    status_code = 409


class AuthError(LedgerError):
    status_code = 401


class PermissionDenied(LedgerError):
    status_code = 403
