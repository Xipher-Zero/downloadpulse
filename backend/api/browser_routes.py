"""Browser pairing, connected browsers and the browser-client API over HTTP.

Three audiences, each fenced by ``auth.permissions``:

- a browser extension with no credential yet: start and poll a pairing
  (``PUBLIC``; the poll's secret device code is the proof); and a connected
  extension asking this server to prove it is the one that issued its token,
  before it sends that token (``PUBLIC``: the proof is the answer);
- a signed-in person: approve or deny a pairing for their OWN account, and
  list or disconnect their own browsers (``BROWSERS_OWN``);
- a browser token: the browser-client API (``BROWSER_CLIENT``, the only
  capability a browser token holds). Its first route proves a pairing worked
  and names the protocol the server speaks; the browser adapter extends it.

The extension's requests carry its own origin; ``auth.middleware``
admits extension origins under ``/api/browser/v1/`` only. The person-facing
routes live under ``/api/account/`` and keep every ordinary protection
(session, CSRF, same-origin).
"""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from auth import accounts
from auth.accounts import TOKEN_KIND_BROWSER, AccountRefused
from auth.browser_pairing import (
    PAIRING_SECONDS, POLL_INTERVAL_SECONDS, PairingRefused, pairing_store,
)
from auth.manager import peer_key
from auth.throttle import DualWindowRateLimiter
from application.dependencies import get_application
from application.service import ApplicationService
from api.external_routes import _call
from core.display_errors import DisplayError, refusal
from external.service import BROWSER_SOURCE_KINDS, ExternalClients
from services import setup_lifecycle

router = APIRouter()

BROWSER_PROTOCOL = "downloadpulse.browser/1"

# Starting a pairing allocates in-memory state for ten minutes: ten starts
# per peer and sixty-four installation-wide per ten minutes keep the store
# (``MAX_PENDING`` = 64) within capacity even before expiry runs.
pairing_start_limiter = DualWindowRateLimiter(per_peer_limit=10, global_limit=64, window_seconds=600)
# A signed-in person viewing, approving or denying pairings: generous for a
# person, a ceiling for anything scripted against a session.
pairing_answer_limiter = DualWindowRateLimiter(per_peer_limit=30, global_limit=600, window_seconds=600)


# A connected browser asks for this server's identity proof before it sends its token: about
# twice a minute while it is in use, and before each send that carries a sign-in. Its answers
# are counted per browser token, never per peer: behind a reverse proxy, or on the network
# through the desktop's Local Port, every browser and any stranger share one peer, and a
# stranger's flood must never refuse a real browser its proof. A stranger who knows no token
# ID (a random UUID) reaches only the second budget: malformed requests and unknown token IDs,
# per peer and installation-wide. A real extension sends neither.
identity_token_limiter = DualWindowRateLimiter(per_peer_limit=600, global_limit=600 * 4096, window_seconds=600,
                                               max_peers=4096)
identity_refusal_limiter = DualWindowRateLimiter(per_peer_limit=60, global_limit=6000, window_seconds=600)
IDENTITY_VERSION = 1
_IDENTITY_NONCE = re.compile(r"[A-Za-z0-9_-]{22,128}")
_IDENTITY_ORIGIN = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]{1,2040}")


def _pairing_refused(exc: PairingRefused, status: int = 404) -> DisplayError:
    return DisplayError(status, str(exc), code=exc.code)


def _answer_allowed(request: Request) -> None:
    if not pairing_answer_limiter.allow(request.state.principal.account_id or peer_key(request)):
        raise DisplayError(429, "Too many connection requests; wait a few minutes.",
                           code="browser.pairing.rate_limited", headers={"Retry-After": "60"})


@router.post("/browser/v1/pairings")
async def start_browser_pairing(request: Request,
                                client_name: Annotated[str, Body(embed=True, max_length=256)] = ""):
    """Start a pairing. The extension shows ``user_code`` and opens
    ``verification_path`` on this server; it polls with ``device_code``."""
    if not pairing_start_limiter.allow(peer_key(request)):
        raise DisplayError(429, "Too many connection requests; wait a few minutes.",
                           code="browser.pairing.rate_limited", headers={"Retry-After": "60"})
    # Until Finish, no account a person could approve the pairing with is
    # settled yet: refused before any pairing state is allocated. Not 401-405,
    # which the extension reads as a server too old to pair.
    if not (await setup_lifecycle.initial_setup()).complete:
        raise DisplayError(409, "Finish setting up DownloadPulse first, then connect again.",
                           code="browser.pairing.setup_incomplete")
    try:
        pairing, device_code = await pairing_store.start(client_name, peer_key(request))
    except PairingRefused as exc:
        raise _pairing_refused(exc, 503 if exc.code == "browser.pairing.busy" else 422) from None
    return {"protocol": BROWSER_PROTOCOL, "device_code": device_code, "user_code": pairing.user_code,
            "verification_path": f"/?connect-browser={pairing.user_code}",
            "expires_in": PAIRING_SECONDS, "interval": POLL_INTERVAL_SECONDS}


@router.post("/browser/v1/pairings/poll")
async def poll_browser_pairing(device_code: Annotated[str, Body(embed=True, max_length=256)] = ""):
    """``pending`` | ``slow_down`` | ``denied`` | ``expired`` | ``approved``
    (with the raw token, exactly once)."""
    return await pairing_store.poll(device_code)


def _identity_rate_limited() -> DisplayError:
    return DisplayError(429, "Too many requests; wait a few minutes.",
                        code="browser.identity.rate_limited", headers={"Retry-After": "60"})


@router.post("/browser/v1/identity")
async def prove_browser_identity(request: Request,
                                 token_id: Annotated[str, Body(embed=True, max_length=64)] = "",
                                 nonce: Annotated[str, Body(embed=True, max_length=128)] = "",
                                 origin: Annotated[str, Body(embed=True, max_length=2048)] = ""):
    """Prove this server is the one that issued browser token ``token_id``
    (1.0.8, ``capabilities.identity``), so the extension never hands its token,
    or a person's sign-in, to something else listening at the address it knows
    (another program on 127.0.0.1:8095). No credential is sent -- that is the
    point -- and none is needed: the answer is
    ``HMAC-SHA256(SHA-256(token), context \\n nonce \\n origin)``
    (``accounts.browser_identity_proof``), which only a holder of the token or
    of this server's record of it can compute."""
    malformed = not _IDENTITY_NONCE.fullmatch(nonce) or not _IDENTITY_ORIGIN.fullmatch(origin)
    proof = None if malformed else await accounts.browser_identity_proof(token_id, nonce, origin)
    if proof is None:
        # Malformed, or no such browser token: counted per peer and installation-wide.
        if not identity_refusal_limiter.allow(peer_key(request)):
            raise _identity_rate_limited()
        if malformed:
            raise DisplayError(422, "An identity check needs a nonce and the server's address.",
                               code="browser.identity.invalid")
        raise DisplayError(404, "Connected browser not found.", code="browser.identity.unknown")
    # A known browser token's answers: counted for that token alone, whoever else asks.
    if not identity_token_limiter.allow(token_id):
        raise _identity_rate_limited()
    return {"protocol": BROWSER_PROTOCOL, "identity": IDENTITY_VERSION, "proof": proof}


@router.get("/account/browser-pairings/{user_code}")
async def pending_browser_pairing(request: Request, user_code: str):
    """What the signed-in person is asked to approve: never the device code."""
    _answer_allowed(request)
    try:
        pairing = await pairing_store.pending(user_code)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    return {"user_code": pairing.user_code, "client_name": pairing.client_name,
            "requested_at": pairing.created_at, "expires_in": PAIRING_SECONDS}


@router.post("/account/browser-pairings/{user_code}/approve")
async def approve_browser_pairing(request: Request, user_code: str,
                                  label: Annotated[str, Body(embed=True, max_length=128)] = ""):
    """Connect the browser to the signed-in person's own account."""
    _answer_allowed(request)
    principal = request.state.principal
    try:
        pairing = await pairing_store.approve(user_code, principal.account_id, label)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    except AccountRefused as exc:
        raise refusal(403 if exc.code == "account.browser_not_permitted" else 422, str(exc), exc) from None
    return {"ok": True, "browser": {"id": pairing.token_id, "label": pairing.token_label}}


@router.post("/account/browser-pairings/{user_code}/deny")
async def deny_browser_pairing(request: Request, user_code: str):
    _answer_allowed(request)
    try:
        await pairing_store.deny(user_code)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    return {"ok": True}


def _public_browser(row: dict) -> dict:
    return {"id": str(row["id"]), "label": str(row["label"]), "created_at": row["created_at"],
            "last_seen_at": row.get("last_seen_at")}


@router.get("/account/browsers")
async def list_own_browsers(request: Request):
    """The signed-in person's own connected browsers."""
    rows = await accounts.list_tokens(request.state.principal.account_id)
    return {"browsers": [_public_browser(row) for row in rows if row.get("kind") == TOKEN_KIND_BROWSER]}


@router.delete("/account/browsers/{token_id}")
async def disconnect_own_browser(request: Request, token_id: str):
    """Disconnect one of the signed-in person's own browsers; another
    account's token, or an Integration token, is simply not found."""
    account_id = request.state.principal.account_id
    if not await accounts.revoke_token(account_id, token_id, kind=TOKEN_KIND_BROWSER, actor_id=account_id):
        raise DisplayError(404, "Connected browser not found.", code="account.browser_token_not_found")
    return {"ok": True}


@router.get("/browser-tokens")
async def list_all_browser_tokens():
    """An Admin's view of every connected browser (Users & Access ›
    Integration Accounts): revoked through the account token route like any
    token. A listing, not an account: each browser stays its person's."""
    return {"browsers": [{"id": str(row["id"]), "label": str(row["label"]), "account_id": str(row["account_id"]),
                          "account_name": str(row["account_name"]), "created_at": row["created_at"],
                          "last_seen_at": row.get("last_seen_at")} for row in await accounts.list_browser_tokens()]}


def _request_context_capabilities() -> dict:
    """What a link submission may bring with it (``transfers.request_context``):
    its sign-in on the links' own site and the page it came from. Core hands
    them to whichever integration fetches the links, so they are offered for
    every link, not only for a video."""
    from transfers import request_context as held
    return {"cookies": {"max_count": held.MAX_COOKIES, "max_bytes": held.MAX_BYTES, "sources": ["links"]},
            "referrer": {"max_length": held.MAX_REFERRER, "sources": ["links"]}}


@router.get("/browser/v1/session")
async def browser_session(request: Request, application: ApplicationService = Depends(get_application)):
    """Which connection this browser token is, and the protocol this server
    speaks: how the extension verifies a pairing and a server's version; and
    its person's appearance, which the extension's own pages follow."""
    from services import account_appearance
    principal = request.state.principal
    rows = await accounts.list_tokens(principal.account_id)
    label = next((str(row["label"]) for row in rows if str(row["id"]) == principal.credential_id), "")
    capabilities = {"sources": sorted(BROWSER_SOURCE_KINDS), "controls": ["pause", "resume"],
                    "withdraw": True, "estimate": True,
                    # This server answers ``POST /browser/v1/identity`` (1.0.8): once seen, the
                    # extension asks for that proof before it sends this token again.
                    "identity": IDENTITY_VERSION}
    capabilities.update(_request_context_capabilities())
    return {"protocol": BROWSER_PROTOCOL, "token": {"id": principal.credential_id, "label": label},
            "account": {"display_name": principal.display_name},
            "capabilities": capabilities,
            # The person's own appearance (services.account_appearance, its one owner): the
            # mode and the palette chosen for each mode, as palette IDs. The extension draws
            # its own pages in it (operator, 2026-11-18).
            "appearance": await account_appearance.load(principal.account_id)}


@router.delete("/browser/v1/session")
async def disconnect_this_browser(request: Request):
    """Disconnect the browser this token belongs to, from the browser itself
    (the extension's Disconnect): the token that carries this request is
    revoked at once, journaled as disconnected by its owner. The work it
    sent is untouched."""
    principal = request.state.principal
    if not await accounts.revoke_token(principal.account_id, principal.credential_id, kind=TOKEN_KIND_BROWSER,
                                       actor_id=principal.account_id):
        raise DisplayError(404, "Connected browser not found.", code="account.browser_token_not_found")
    return {"ok": True}


@router.post("/browser/v1/application/show")
async def show_application():
    """The extension's Open DownloadPulse, for DownloadPulse on this computer:
    the installed desktop application brings its own window forward (its
    supervisor does, as the tray's Open does, on its next look) and answers
    ``{"desktop": true}``. A server has no window: ``{"desktop": false}``,
    and the extension opens the page instead. Nothing else is touched."""
    from core.config import desktop_installation
    if not desktop_installation():
        return {"desktop": False}
    from application.desktop_control import request_show
    request_show()
    return {"desktop": True}


# ── the browser's own downloads (``external.service``, browser jobs) ────────
#
# Each route is the external-client owner's operation, called with the
# browser token's principal: the same idempotency, listing, isolation,
# withdrawal and advisory controls an Integration has, with no delivery.


@router.post("/browser/v1/jobs")
async def submit_browser_job(request: Request, body: Annotated[dict, Body()],
                             application: ApplicationService = Depends(get_application)):
    """Submit one download (``idempotency_key`` required): a retry with the
    same key answers the same job (200), never a second download."""
    job, created = await _call(ExternalClients(application).submit(request.state.principal, body))
    return JSONResponse(status_code=201 if created else 200, content=job)


@router.get("/browser/v1/jobs")
async def list_browser_jobs(request: Request, application: ApplicationService = Depends(get_application),
                            after: Annotated[int, Query(ge=0)] = 0,
                            limit: Annotated[int, Query(ge=1, le=200)] = 100,
                            phase: Annotated[str | None, Query(max_length=32)] = None):
    """This browser's own downloads, by change cursor."""
    return await _call(ExternalClients(application).jobs(request.state.principal, after=after, limit=limit,
                                                         phase=phase))


@router.get("/browser/v1/jobs/{job_id}")
async def get_browser_job(job_id: str, request: Request,
                          application: ApplicationService = Depends(get_application)):
    return await _call(ExternalClients(application).job(request.state.principal, job_id))


@router.post("/browser/v1/jobs/{job_id}/withdraw")
async def withdraw_browser_job(job_id: str, request: Request,
                               application: ApplicationService = Depends(get_application)):
    """Remove: withdraw this browser's request. The download stops only if
    nobody else still needs it; a finished download is kept."""
    return await _call(ExternalClients(application).withdraw(request.state.principal, job_id))


@router.post("/browser/v1/jobs/{job_id}/controls")
async def request_browser_control(job_id: str, request: Request, body: Annotated[dict, Body()],
                                  application: ApplicationService = Depends(get_application)):
    """Advisory pause/resume (``{kind, request_key}``): honoured only while
    this browser's request is the only one its download serves."""
    return await _call(ExternalClients(application).control(request.state.principal, job_id, body))
