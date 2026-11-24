"""An exception's text stays in the server log, never in an API response
(the update check behind ``/api/version/check``, ``/api/aria2/runtime``); a notification failure's
reason is one log line however many lines the remote endpoint answered."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import aiohttp

from api import routes
from services.notifications import NotificationService

SECRET = "internal /app/data/secret.db path\ntraceback detail"


class _Failing:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(SECRET)


def test_a_failed_update_check_is_logged_and_never_kept_or_served(monkeypatch, caplog):
    """The update check's one fetcher (``application.updates.refresh``): its
    failure reason is logged server-side; the record holds no exception text,
    and ``/api/version/check`` only reads that record -- no network I/O."""
    from application import updates

    monkeypatch.setattr(updates, "_record", None)
    monkeypatch.setattr(aiohttp, "ClientSession", _Failing)
    with caplog.at_level(logging.WARNING, logger="downloadpulse.updates"):
        assert asyncio.run(updates.refresh()) is None
    assert "secret.db" in caplog.text  # the reason is logged server-side
    assert updates.record() is None    # and kept nowhere else
    result = asyncio.run(routes.version_check(SimpleNamespace(state=SimpleNamespace(principal=None))))
    assert result == {"checked": False}
    assert "secret.db" not in repr(result)


def test_runtime_diagnostics_failure_names_no_exception_text(monkeypatch, caplog):
    async def status():
        return {"running": True}

    async def diagnostics():
        raise RuntimeError(SECRET)

    application = SimpleNamespace(integration_admin=lambda _id: SimpleNamespace(memory_diagnostics=diagnostics,
                                                                                runtime_status=status))
    with caplog.at_level(logging.WARNING, logger="downloadpulse.routes"):
        result = asyncio.run(routes.aria2_runtime_status(application))
    assert result["diagnostics"] == {"error": "Diagnostics are unavailable"}
    assert "secret.db" not in repr(result)
    assert "secret.db" in caplog.text


def test_notification_failure_reason_is_one_log_line(monkeypatch, caplog):
    forged = "Discord webhook 400: bad\n2026-01-01 | ERROR | forged | admin signed in"

    class _Raising:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(forged)

    import services.notifications as notifications
    monkeypatch.setattr(notifications.aiohttp, "ClientSession", _Raising)
    service = NotificationService("https://example.invalid/hook", dedup=False)
    with caplog.at_level(logging.ERROR):
        sent = asyncio.run(service.send("Title", "Body"))
    assert sent is False
    failures = [r.getMessage() for r in caplog.records if "notification failed" in r.getMessage()]
    assert failures and all("\n" not in line and "\r" not in line for line in failures)
    assert "\\n2026-01-01" in failures[0]


def test_log_values_never_carry_a_line_break():
    from core.logging_utils import sanitize_exception, sanitize_log_value

    forged = "name\r\n2026-11-21 ERROR forged record"
    assert "\n" not in sanitize_log_value(forged) and "\r" not in sanitize_log_value(forged)
    assert sanitize_log_value(forged) == "name\\r\\n2026-11-21 ERROR forged record"
    assert "\n" not in sanitize_exception(RuntimeError(forged))


def test_log_redaction_is_linear_on_hostile_values():
    import time

    from core.logging_utils import sanitize_log_value

    started = time.monotonic()
    for hostile in ("a" * 200_000, "a://!:!" * 30_000, "x://" + "y:" * 50_000):
        sanitize_log_value(hostile)
    assert time.monotonic() - started < 2
    # Redaction is unchanged, and still runs before the value is shortened.
    assert sanitize_log_value("see https://user:secret@host/p") == "see https://user:<redacted>@host/p"
    assert "secret" not in sanitize_log_value("x" * 5000 + " https://u:secret@h/", max_length=6000)
