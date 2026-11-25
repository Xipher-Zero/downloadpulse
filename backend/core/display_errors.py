"""The HTTP error the interface shows, and the boundary that raises it.

``DisplayError`` keeps the English ``detail`` API clients have always
received, beside a stable ``code`` (a key under ``error.`` in the catalog) and
``params`` the interface shows in the reader's language. ``refusal`` is what a
boundary raises: named by its cause's own code where it has one
(``core.localization`` owns the codes, ``code_of``, and what params may
disclose). Kept apart from ``core.localization`` so that module imports no
web framework.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.localization import UNCLASSIFIED, _checked_code, _safe_params, code_of, disclosed


class DisplayError(HTTPException):
    """An HTTP error the interface shows: ``detail`` stays the English text
    API clients already receive; ``code`` (a key under ``error.`` in the
    catalog) and ``params`` let the interface show it in its language."""

    def __init__(self, status_code: int, detail: str, *, code: str, params: dict | None = None,
                 headers: dict | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        # The params it sends never disclose more than ``detail`` (``disclosed``).
        self.code, self.params = disclosed(detail, _checked_code(code), params)
        # A boundary that wraps this refusal keeps its code (``code_of``).
        self.display_code, self.display_params = self.code, self.params


def display_error_body(error: DisplayError) -> dict:
    return {"detail": error.detail, "code": error.code, "params": error.params}


def refusal(status_code: int, detail: str, cause: BaseException | None = None, *, code: str | None = None,
            params: dict | None = None, headers: dict | None = None) -> DisplayError:
    """The response a boundary raises: ``detail`` exactly the English it always
    sent; named by the cause's own code where it has one, else by ``code``,
    else ``unclassified`` (the interface then shows the English ``detail``)."""
    named, named_params = code_of(cause)
    if named:
        return DisplayError(status_code, detail, code=named, params=named_params, headers=headers)
    if code:
        return DisplayError(status_code, detail, code=_checked_code(code), params=_safe_params(params), headers=headers)
    return DisplayError(status_code, detail, code=UNCLASSIFIED, headers=headers)
