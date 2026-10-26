"""The one exception the CRM raises that the framework has to render.

Copied from the monolith's `api/utils/custom_error.py`, keeping ONLY what the
CRM uses — `PermissionDeniedError`, raised by `require()` / `require_any()`.

⚠️ IT RENDERS AS HTTP 200. That looks wrong and is deliberate: this codebase
has two error shapes, and a *business* failure is
`{"status": "failure", "message": …, "data": null}` at 200, while a validation
failure is a 422. The UI's `unwrap()` reads `status === "success"` to decide
`ok`, so a permission denial rendered as 403 would reach the screen as a
network-shaped error with no message — the user would see "Something went
wrong. Please try again." instead of "You don't have permission…".

Phase 5 diffs this service's responses against the monolith's, failures
included. So this handler must stay byte-identical to the monolith's
`permission_denied_handler` until both are changed together.
"""
from __future__ import annotations


class PermissionDeniedError(Exception):
    """Raised when a caller lacks a permission (or workspace access)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class AuthError(Exception):
    """An authentication or availability failure, in the monolith's shape.

    ⚠️ THE ENVELOPE IS `{"message", "messages", "success": false}`, NOT the
    business-failure one. The platform's `ApiError` handler renders exactly
    that, and the UI's `unwrap()` reads `body.success === false` to pull the
    message out — so a differently-shaped 401 reaches the screen as
    "Something went wrong. Please try again." with the real reason nowhere.

    Phase 5 diffs the two services' responses, failures included, so this
    stays identical until both are changed together.
    """

    def __init__(self, message: str, status_code: int = 401) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class AuthNotConfiguredError(Exception):
    """Auth is wired up (Phase 4) but this deployment is missing its settings.

    ⚠️ THE SAFE DEFAULT IS REFUSAL, and it stays that way now that auth works:
    a missing public key or platform URL means this service cannot tell who is
    calling, and a service that cannot tell must not guess. In a system whose
    entire tenancy rule is `created_by == account_id`, a defaulted account id
    serves one customer's pipeline to another.

    Rendered as 503: it is a deployment fault, not the caller's.
    """

    def __init__(self, message: str | None = None) -> None:
        self.message = message or (
            "The CRM service is not configured to authenticate requests: it "
            "needs a JWT public key and the platform's base URL. See "
            "config/app.conf.example."
        )
        super().__init__(self.message)
