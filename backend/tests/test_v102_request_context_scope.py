"""1.0.2: what core hands an integration for one address
(``transfers.request_context.RequestContext``) is exactly what the
submitting browser would have sent there -- the cookies whose domain, path and
secure flag match, the referrer under the downgrade rule -- and nothing for
any other host, scheme or transport. A submission is validated before core
holds any of it."""
from __future__ import annotations

import time

import pytest

from transfers.request_context import MAX_BYTES, MAX_COOKIES, ContextRefused, RequestContext

LINK = "https://files.example.test/pub/archive.zip"


def cookie(name="SID", value="v", domain=".example.test", path="/", secure=False, host_only=False, expires=None):
    return {"name": name, "value": value, "domain": domain, "path": path, "secure": secure,
            "http_only": True, "host_only": host_only, "expires": expires}


def context(*cookies, referrer=None, links=(LINK,)):
    return RequestContext.from_submission(list(cookies) or None, referrer, list(links))


def names(scoped) -> set[str]:
    return {item.name for item in scoped.cookies}


def test_an_address_gets_only_the_cookies_a_browser_would_send_it():
    held = context(cookie("SITE"), cookie("HOST", domain="files.example.test", host_only=True),
                   cookie("SECURE", secure=True), cookie("PUB", path="/pub"), cookie("PRIV", path="/private"),
                   cookie("GONE", expires=int(time.time()) - 10))
    assert names(held.for_address(LINK)) == {"SITE", "HOST", "SECURE", "PUB"}
    # Another host under the same site: the host-only cookie is not its.
    assert names(held.for_address("https://cdn.example.test/pub/x")) == {"SITE", "SECURE", "PUB"}
    # Plain HTTP: no secure cookie.
    assert names(held.for_address("http://files.example.test/pub/x")) == {"SITE", "HOST", "PUB"}
    # A path prefix only at a segment boundary.
    assert "PUB" not in names(held.for_address("https://files.example.test/public/x"))
    # Another site, another transport: nothing at all.
    assert not held.for_address("https://example.org/pub/x")
    assert not held.for_address("ftp://files.example.test/pub/x")
    assert not held.for_address("not an address")


def test_headers_are_the_cookie_and_referer_lines_for_that_address():
    held = context(cookie("A", "1", path="/"), cookie("B", "2", path="/pub"), referrer="https://example.test/list#top")
    headers = held.headers(LINK)
    # Longer paths first, as a browser orders them; the fragment never travels.
    assert headers == {"Cookie": "B=2; A=1", "Referer": "https://example.test/list"}
    assert held.headers("https://example.org/x") == {"Referer": "https://example.test/list"}


def test_an_https_referrer_never_goes_to_plain_http():
    held = context(referrer="https://example.test/page")
    assert held.headers("http://files.example.test/x") == {}
    assert context(referrer="http://example.test/page").headers("http://files.example.test/x") == {
        "Referer": "http://example.test/page"}


def test_a_cookie_jar_is_the_site_s_cookies_whatever_their_path():
    held = context(cookie("SITE"), cookie("PRIV", path="/private"), cookie("SECURE", secure=True),
                   cookie("GONE", expires=int(time.time()) - 10))
    assert {item.name for item in held.cookie_jar(LINK)} == {"SITE", "PRIV", "SECURE"}
    assert held.cookie_jar("https://example.org/") == ()
    assert held.cookie_jar("file:///etc/passwd") == ()


def test_its_values_never_appear_in_its_repr():
    held = context(cookie(value="secret-cookie-value"), referrer="https://example.test/secret-path")
    for text in (repr(held), repr(held.cookies[0]), str(held)):
        assert "secret" not in text


@pytest.mark.parametrize("cookies, referrer, message", [
    ([], None, "non-empty list"),
    ("SID=v", None, "non-empty list"),
    ([cookie(domain=".example.org")], None, "does not match"),
    ([cookie(domain="test")], None, "does not match"),
    ([cookie(name="bad name")], None, "valid cookie name"),
    ([cookie(value="a;b")], None, "valid cookie value"),
    ([cookie(value="a\r\nX-Injected: 1")], None, "valid cookie value"),
    ([cookie(path="relative")], None, "path is not valid"),
    ([{**cookie(), "extra": 1}], None, "each cookie is an object"),
    ([cookie(expires=True)], None, "expires"),
    ([cookie()] * (MAX_COOKIES + 1), None, f"at most {MAX_COOKIES}"),
    ([cookie(name=f"n{i}", value="x" * 8000) for i in range(5)], None, f"at most {MAX_BYTES} bytes"),
    (None, "javascript:alert(1)", "referrer"),
    (None, "https://example.test/a b", "referrer"),
    (None, "https://user:hunter2xyz@example.test/", "user name or password"),
    (None, "https://example.test/" + "x" * 2100, "referrer"),
    (None, "", "referrer"),
])
def test_a_malformed_or_foreign_submission_is_refused_naming_the_field(cookies, referrer, message):
    with pytest.raises(ContextRefused, match=message) as refused:
        RequestContext.from_submission(cookies, referrer, [LINK])
    assert "hunter2xyz" not in str(refused.value) and "alert" not in str(refused.value)


def test_a_cookie_may_be_for_any_of_the_submitted_links():
    held = RequestContext.from_submission([cookie(domain="other.test")], None,
                                          [LINK, "https://other.test/file.bin"])
    assert names(held.for_address("https://other.test/file.bin")) == {"SID"}
    assert not RequestContext.from_submission(None, None, [LINK])


def test_only_core_and_the_registering_intake_touch_the_holder():
    """Integrations are handed the context on the requests core passes them; none may
    reach for the holder (the 1.0.14 shape, where Media Downloads read a store outside
    core through a context variable core set for it, is gone)."""
    from pathlib import Path
    backend = Path(__file__).resolve().parents[1]
    touching = sorted(path.relative_to(backend).as_posix() for path in backend.rglob("*.py")
                      if "tests" not in path.parts and "request_contexts" in path.read_text())
    assert touching == ["external/service.py", "transfers/_engine_base.py", "transfers/request_context.py"]
    leftovers = sorted(path.relative_to(backend).as_posix() for path in backend.rglob("*.py")
                       if "tests" not in path.parts
                       and any(name in path.read_text() for name in ("served_transfer(", "_SERVED_TRANSFER",
                                                                     "session_cookies", "browser_cookies")))
    assert leftovers == []
