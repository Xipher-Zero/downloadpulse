"""Request context: the sign-in and referrer a submission brings with it.

A client that sends DownloadPulse a link may send, with it, what its own fetch
of that link would have carried: the cookies the client holds for the link's
site and the page the link was found on (its referrer). Some sources answer
only with them -- a file behind a sign-in, a host that checks where a download
was started from, a video only its signed-in viewer can watch.

Core owns that context, as it owns an answered input challenge
(``transfers.input_required.SubmittedInput``), and integrations never reach
for it: core hands each one exactly the context for the address it is about to
fetch, on the request it already passes --

* ``TransferRequest.context`` for a provider's ``resolve`` (and a candidate
  refresh's ``refresh_request``);
* ``ExecutionRequest.context`` for an executor's ``prepare`` and ``start``.

An integration reads ``context.for_address(address)`` (or ``headers``) for
the address it fetches and nothing else: the cookies whose domain, path and
``secure`` flag match that address, as the submitting browser itself would
have sent them, and the referrer under the browser's own downgrade rule. A
provider's issued link on another host matches no cookie.

Its lifetime is the submission's (``RequestContexts``):

* only in this process's memory, keyed by the submission's demand -- never in
  the database, a file, a log, a job or attempt record, the request's
  identity, a fingerprint, the idempotency digest or a replay; a restart loses
  it (the transfer then proceeds without);
* handed out only for a transfer that is not finished and whose every
  outstanding demand belongs to the context's owner, while the registrant says
  its grant is still valid (a browser's token not revoked) and within
  ``CEILING_SECONDS`` of the submission. Anything else -- an unknown transfer, a
  mixed or unattributed demand, a database error -- is "no context": it fails
  closed;
* released by ``release`` only, driven from durable state by ``settle`` and,
  eagerly, by a withdrawal or a refused admission.

Python cannot guarantee that released memory is overwritten: a released entry
is only unreferenced.
"""
from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from db.database import get_db


def _terminal() -> frozenset[str]:
    """Transfer states after which a submission's context is never handed out: the
    policy's own set (imported here, not at module level: models imports this module)."""
    from transfers.policy import TERMINAL_TRANSFER_STATES
    return frozenset(str(getattr(state, "value", state)) for state in TERMINAL_TRANSFER_STATES)

#: Per submission: at most this many cookies, and this many bytes of names,
#: values, domains and paths.
MAX_COOKIES = 300
MAX_BYTES = 32 * 1024
#: The longest referrer kept.
MAX_REFERRER = 2048
#: At most this many submissions' context at once, and this many for one owner;
#: past either a submission with context is refused (retryable), never accepted
#: without it silently -- and one account can never use up every other's room.
CAPACITY = 64
OWNER_CAPACITY = 16
#: Held at most this long after the submission, whatever the transfer's state.
CEILING_SECONDS = 24 * 3600
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}$")
# Header-safe: printable ASCII only (a browser percent-encodes anything else), never ";".
_UNSAFE_VALUE = re.compile(r"[^\x20-\x7e]|;")
_UNSAFE_REFERRER = re.compile(r"[^\x21-\x7e]")
_FIELDS = frozenset({"name", "value", "domain", "path", "secure", "http_only", "host_only", "expires"})


class ContextRefused(ValueError):
    """A submission's context is malformed or exceeds a bound. The message
    names the field, never a value."""


class ContextCapacity(RuntimeError):
    """Every slot is in use; the same submission may be retried."""


@dataclass(frozen=True)
class ContextCookie:
    name: str
    value: str = field(repr=False)
    domain: str
    path: str
    secure: bool
    http_only: bool
    host_only: bool
    expires: int | None

    def matches(self, scheme: str, host: str, path: str, now: float) -> bool:
        """Whether a browser would send this cookie to ``scheme://host/path`` (RFC 6265 5.4)."""
        from transfers.requests import SECURE_WEB_SCHEME, WEB_SCHEMES  # noqa: F401 -- lazy: models imports this module
        if self.expires is not None and self.expires <= now:
            return False
        if self.secure and scheme != SECURE_WEB_SCHEME:
            return False
        bare = self.domain.lower().lstrip(".")
        if self.host_only:
            if host != bare:
                return False
        elif not (host == bare or host.endswith("." + bare)):
            return False
        prefix = self.path or "/"
        return path == prefix or (path.startswith(prefix) and (prefix.endswith("/") or path[len(prefix)] == "/"))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _origin(address: str) -> tuple[str, str, int | None]:
    try:
        parts = urlsplit(address)
        return parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."), parts.port
    except ValueError:
        return "", "", None


def _parts(address: str) -> tuple[str, str, str]:
    try:
        parts = urlsplit(address)
        return parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."), parts.path or "/"
    except ValueError:
        return "", "", "/"


@dataclass(frozen=True)
class RequestContext:
    """One submission's context, or the part of it for one address."""
    cookies: tuple[ContextCookie, ...] = field(default=(), repr=False)
    referrer: str = field(default="", repr=False)

    def __repr__(self) -> str:   # never a value, nor how many
        return "<request context: redacted>"

    def __bool__(self) -> bool:
        return bool(self.cookies or self.referrer)

    def for_address(self, address: str) -> "RequestContext":
        """Exactly what a browser would send with a request to ``address``: its
        matching cookies, and the referrer under the browser's default policy
        (strict-origin-when-cross-origin): in full to its own origin, only its
        origin to any other, and nothing from HTTPS to plain HTTP. Nothing for
        anything but HTTP(S)."""
        from transfers.requests import SECURE_WEB_SCHEME, WEB_SCHEMES  # noqa: F401 -- lazy: models imports this module
        scheme, host, path = _parts(address)
        if scheme not in WEB_SCHEMES or not host:
            return RequestContext()
        now = time.time()
        cookies = tuple(cookie for cookie in self.cookies if cookie.matches(scheme, host, path, now))
        referrer = self.referrer
        if referrer:
            source = _origin(referrer)
            if source[0] == SECURE_WEB_SCHEME and scheme != SECURE_WEB_SCHEME:
                referrer = ""
            elif source != _origin(address):
                referrer = f"{source[0]}://{urlsplit(referrer).netloc.rsplit('@', 1)[-1]}/"
        return RequestContext(cookies, referrer)

    def cookie_jar(self, address: str) -> tuple[ContextCookie, ...]:
        """The cookies for ``address``'s site, for a fetcher that keeps its own
        cookie jar and applies each cookie's path and ``secure`` flag per
        request itself (a page reader that makes many requests to that site):
        every unexpired cookie whose domain matches the host. Nothing for
        anything but HTTP(S)."""
        from transfers.requests import SECURE_WEB_SCHEME, WEB_SCHEMES  # noqa: F401 -- lazy: models imports this module
        scheme, host, _path = _parts(address)
        if scheme not in WEB_SCHEMES or not host:
            return ()
        now = time.time()
        return tuple(cookie for cookie in self.cookies
                     if cookie.matches(SECURE_WEB_SCHEME, host, cookie.path or "/", now))

    def headers(self, address: str) -> dict[str, str]:
        """``for_address(address)`` as HTTP request headers (``Cookie``, ``Referer``)."""
        scoped = self.for_address(address)
        headers = {}
        if scoped.cookies:
            # Longer paths first, as a browser orders them (RFC 6265 5.4).
            ordered = sorted(scoped.cookies, key=lambda cookie: -len(cookie.path or "/"))
            headers["Cookie"] = "; ".join(f"{cookie.name}={cookie.value}" for cookie in ordered)
        if scoped.referrer:
            headers["Referer"] = scoped.referrer
        return headers

    @classmethod
    def from_submission(cls, cookies, referrer, addresses: list[str]) -> "RequestContext":
        """A submission's ``cookies`` and ``referrer``, validated: each cookie's
        domain matches one of the submitted ``addresses`` (a cookie for any other
        site is refused), within the bounds. ``None`` for either is none."""
        from transfers.requests import SECURE_WEB_SCHEME, WEB_SCHEMES  # noqa: F401 -- lazy: models imports this module
        hosts = {host for host in (_parts(address)[1] for address in addresses) if host}
        parsed, size = [], 0
        if cookies is not None:
            if not isinstance(cookies, list) or not cookies:
                raise ContextRefused("cookies must be a non-empty list")
            if len(cookies) > MAX_COOKIES:
                raise ContextRefused(f"cookies holds at most {MAX_COOKIES} entries")
            for item in cookies:
                if not isinstance(item, dict) or set(item) - _FIELDS:
                    raise ContextRefused("each cookie is an object of name, value, domain, path, secure, http_only, "
                                         "host_only, expires")
                name, value, domain, path = item.get("name"), item.get("value"), item.get("domain"), item.get("path", "/")
                if not isinstance(name, str) or not _TOKEN.match(name):
                    raise ContextRefused("a cookie's name is not a valid cookie name")
                if not isinstance(value, str) or len(value) > 8192 or _UNSAFE_VALUE.search(value):
                    raise ContextRefused("a cookie's value is not a valid cookie value")
                if not isinstance(domain, str) or not domain or len(domain) > 253 or _UNSAFE_VALUE.search(domain):
                    raise ContextRefused("a cookie's domain is not valid")
                if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024 or _UNSAFE_VALUE.search(path):
                    raise ContextRefused("a cookie's path is not valid")
                bare = domain.lower().lstrip(".")
                # Only a submitted address's own site: its host is that domain or under it.
                # An address host takes only a cookie for exactly itself (never a "domain" of its digits).
                if "." not in bare or not any(host == bare or (host.endswith("." + bare) and not _is_ip(host))
                                              for host in hosts):
                    raise ContextRefused("a cookie's domain does not match the submitted link")
                # An omitted host_only is the narrower reading: the cookie stays with its own host.
                flags = [item.get("secure", False), item.get("http_only", False), item.get("host_only", True)]
                if not all(isinstance(flag, bool) for flag in flags):
                    raise ContextRefused("secure, http_only and host_only are true or false")
                if _is_ip(bare):
                    # An address is never a domain to match under (RFC 6265 5.1.3): its own host only.
                    flags[2] = True
                expires = item.get("expires")
                if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int) or expires < 0):
                    raise ContextRefused("a cookie's expires is whole seconds, or null")
                size += len(name) + len(value) + len(domain) + len(path)
                parsed.append(ContextCookie(name, value, domain, path, *flags, expires))
            if size > MAX_BYTES:
                raise ContextRefused(f"cookies hold at most {MAX_BYTES} bytes")
        kept = ""
        if referrer is not None:
            if (not isinstance(referrer, str) or not referrer or len(referrer) > MAX_REFERRER
                    or _UNSAFE_REFERRER.search(referrer) or _parts(referrer)[0] not in WEB_SCHEMES
                    or not _parts(referrer)[1]):
                raise ContextRefused(f"referrer is an http(s) address of at most {MAX_REFERRER} characters")
            try:
                parts = urlsplit(referrer)
            except ValueError:
                raise ContextRefused("referrer is not a valid address") from None
            if parts.username is not None or parts.password is not None:
                raise ContextRefused("referrer may not carry a user name or password")
            # What a browser sends: never the fragment.
            kept = parts._replace(fragment="").geturl()
        return cls(tuple(parsed), kept)


@dataclass(frozen=True)
class _Entry:
    owner: str
    context: RequestContext = field(repr=False)
    valid: Callable[[], Awaitable[bool]] = field(repr=False)
    registered_at: float

    def __repr__(self) -> str:
        return "<request context entry: redacted>"


class RequestContexts:
    """The one holder (module singleton ``request_contexts``)."""

    def __init__(self):
        self._entries: dict[str, _Entry] = {}

    def __repr__(self) -> str:
        return "<RequestContexts: redacted>"

    def __len__(self) -> int:
        return len(self._entries)

    def register(self, demand_id: str, context: RequestContext, *, owner: str,
                 valid: Callable[[], Awaitable[bool]]) -> None:
        """Hold ``context`` for the submission whose demand is ``demand_id``
        (before its admission), for ``owner`` (the submitting account), while
        ``valid()`` says the registrant's grant still stands -- and, until the
        demand exists, that the submission may still become it."""
        if not context:
            return
        if str(demand_id) not in self._entries:
            if len(self._entries) >= CAPACITY:
                raise ContextCapacity("request context capacity reached")
            if sum(entry.owner == str(owner) for entry in self._entries.values()) >= OWNER_CAPACITY:
                raise ContextCapacity("request context capacity reached for this owner")
        self._entries[str(demand_id)] = _Entry(str(owner), context, valid, time.time())

    def release(self, demand_id: str) -> None:
        """The only removal."""
        self._entries.pop(str(demand_id), None)

    def held(self, demand_id: str) -> bool:
        return str(demand_id) in self._entries

    async def settle(self) -> int:
        """Release every entry whose submission has ended, by durable state; how many."""
        released = 0
        for demand_id in list(self._entries):
            # Released only when durable state says it has ended: a read that failed
            # (a busy database) decides nothing, and the next settle asks again.
            if await self._live(demand_id) is False:
                self.release(demand_id)
                released += 1
        return released

    async def _live(self, demand_id: str) -> bool | None:
        """True while it may be handed out, False once it has ended, None when that
        could not be read now (handed out as not live; never released for it)."""
        entry = self._entries.get(demand_id)
        if entry is None or time.time() - entry.registered_at > CEILING_SECONDS:
            return False
        try:
            async with get_db() as db:
                demand = await db.fetchone(
                    """SELECT d.withdrawn_at, t.status FROM transfer_demands d JOIN torrents t ON t.id=d.transfer_id
                       WHERE d.id=?""", (demand_id,))
            # Not admitted (yet): kept while the registrant says it may still become a demand.
            if demand is not None and (demand["withdrawn_at"] is not None or demand["status"] in _terminal()):
                return False
            return bool(await entry.valid())
        except Exception:
            return None

    async def for_transfer(self, transfer_id: int | None) -> RequestContext | None:
        """The context for ``transfer_id``, only when every rule holds; else None."""
        if transfer_id is None or not self._entries:
            return None
        try:
            async with get_db() as db:
                status = await db.fetchone("SELECT status FROM torrents WHERE id=?", (int(transfer_id),))
            if status is None or status["status"] in _terminal():
                return None
            # The transfer core's own reading of "every outstanding demand this transfer's work
            # serves" (consolidated demand included), not a second definition of it.
            from transfers.repository import TransferRepository
            async with get_db() as db:
                outstanding = await TransferRepository.affected_demand_in(db, int(transfer_id))
        except Exception:
            return None
        owners = {row.get("account_id") for row in outstanding}
        # Every outstanding demand is one account's (none unattributed), and one of them holds context.
        if len(owners) != 1 or None in owners or not outstanding:
            return None
        held = sorted((self._entries[row["id"]].registered_at, row["id"]) for row in outstanding
                      if row["id"] in self._entries and self._entries[row["id"]].owner in owners)
        for _, demand_id in held:
            if await self._live(demand_id):
                return self._entries[demand_id].context
        return None


request_contexts = RequestContexts()
