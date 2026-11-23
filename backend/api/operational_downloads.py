"""Operational Downloads and Activity Log read-model routes.

The durable transfer row for a fully absorbed source remains queryable by its
explicit CONSOLIDATED lifecycle state, but the normal operational list excludes
it alongside soft-deleted history. Pagination and totals therefore reflect the
same canonical lifecycle rule as the visible rows.

The Activity Log reads the durable event journal (``db.event_journal``) here:
time window, severity, Event Type and search apply before the page limit, and
pages are navigated by cursor across the whole retained history. This module is
the sole declaring owner of GET /api/events; api.routes declares no variant.
"""
import asyncio
import json
import logging
import sqlite3
import time
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routes import _provider_display_name, _public_transfer_presentation
from application import dispatch_admission as live_admission
from application.dependencies import get_application
from application.manual_candidate_failover import preview_switch, switch_candidate
from application.manual_route_switch import (
    root_route_providers,
    switch_route_provider,
    switch_transfer_provider,
    transfer_route_providers,
)
from transfers.manual_route_switch import route_switch_offer, switch_available, torrent_root
from application.service import ApplicationService
from core import search_contract
from core.presentation_safety import safe_public_host
from db import event_journal
from db.database import get_db
from transfers import codec
from transfers import file_selection as fs
from transfers.read_models import (
    CAPACITY_WAIT_PRESENTATION as _CAPACITY_WAIT_PRESENTATION,
    RAW_PRESENTATION as _RAW_PRESENTATION, TERMINAL_PRESENTATION as _TERMINAL_PRESENTATION,
    WAIT_PRESENTATION as _WAIT_PRESENTATION, active_execution_projection,
)
from transfers.display_name import transfer_display_name
from transfers.runtime_telemetry import acquisition_eta_payload
from transfers.errors import Category, TransferError
from transfers.input_required import public_challenge
from transfers.manual_failover import DiscardConfirmationRequired
from transfers.presentation_repository import (
    ARTIFACT_PRESENTATION_SNAPSHOT_KEYS,
    effective_presentation,
    public_source_identity,
    request_kind_of,
    recovery_presentation,
)
from auth.permissions import require_destruction, transfer_access
from core.localization import DisplayError, UNCLASSIFIED, code_of, disclosed

from transfers.downloads_read_model import (  # noqa: E402 -- core's Downloads read model
    ACTIVITY_ORDER as _ACTIVITY_ORDER, bounded_request_kinds as _bounded_request_kinds, downloads_page,
)

logger = logging.getLogger("downloadpulse.api.operational_downloads")
router = APIRouter()




# Priority-ordering classification of the real EFFECTIVE presentation output
# (never raw status, never badge color). Reuses presentation_repository's own
# canonical vocabulary instead of a hand-copied duplicate table, so this
# never drifts into a second parallel taxonomy:
#
# - _SETTLED_PRESENTATION_STATUSES reuses presentation_repository's own
#   _TERMINAL_PRESENTATION set (imported, not re-derived) plus "failed".
#   presentation_repository deliberately excludes "failed" from
#   _TERMINAL_PRESENTATION because a failed transfer remains operator-
#   actionable (manual retry/switch); this ordering nonetheless places it in
#   settled/history per the explicit product requirement: "Failed must not
#   outrank active/current work merely because it has an error badge."
# - _KNOWN_LIVE_PRESENTATION_STATUSES is derived from every state
#   presentation_repository._RAW_PRESENTATION / _WAIT_PRESENTATION /
#   _CAPACITY_WAIT_PRESENTATION can itself emit, minus the settled ones,
#   plus "input_required"/"requires_attention" (operator-gated overrides
#   _RAW_PRESENTATION never enumerates) and "extracting" (the durable
#   TransferState.POST_PROCESSING raw value, echoed as-is by
#   recovery_presentation's fallback since _RAW_PRESENTATION has no explicit
#   entry for it).
# - A presentation_status in NEITHER set -- something this ordering has
#   genuinely never seen -- gets its own middle tier: it can never outrank a
#   KNOWN live/actionable item (the literal product requirement), but it is
#   not assumed to be settled either. See _activity_priority_tier.
_SETTLED_PRESENTATION_STATUSES = frozenset(_TERMINAL_PRESENTATION) | {"failed"}
_KNOWN_LIVE_PRESENTATION_STATUSES = (
    frozenset(state for state, _label, _badge in _RAW_PRESENTATION.values())
    | frozenset(state for state, _label, _badge in _WAIT_PRESENTATION.values())
    | {_CAPACITY_WAIT_PRESENTATION[0], "input_required", "requires_attention", "extracting"}
) - _SETTLED_PRESENTATION_STATUSES


def _activity_priority_tier(presentation_status) -> int:
    """0 = known live/actionable, 1 = unrecognized (never outranks tier 0,
    never assumed settled), 2 = known settled/history."""
    status = str(presentation_status or "").strip().lower()
    if status in _SETTLED_PRESENTATION_STATUSES:
        return 2
    if status in _KNOWN_LIVE_PRESENTATION_STATUSES:
        return 0
    return 1


def _disabled_provider_ids(application) -> frozenset[str]:
    """Durable, PAGE-GLOBAL provider-enablement facts, read once per request.

    ``descriptor.enabled`` is computed once at composition time from
    AppSettings (e.g. ``bool(api_key)``) and held on the provider object the
    already-injected ``application.engine.registry`` carries for the whole
    request — no extra DB round-trip, no per-row/live check. This is NOT the
    full authoritative actionability gate: live route/candidate-expiry and
    provider *health* (``registry._unhealthy``) have no durable stored
    representation anywhere in this codebase today (confirmed: no production
    caller ever populates either), so they cannot be bounded-SQL-joined
    without either fabricating a stale guess or duplicating a live provider
    call per row — manual_candidate_failover.py re-validates those at actual
    switch time instead, and always has, even against the comprehensive
    Details read. Provider *enablement*, in contrast, is exactly the kind of
    durable fact that closes a real false-positive: a transfer whose only
    common hosts route through a disabled/unconfigured provider has no
    possible target, and the list must say so.

    Delegates to ``application.dispatch_admission.disabled_provider_ids`` --
    the same durable snapshot the Section 9 capacity-wait admission
    assessment below reuses -- so there is exactly one owner of "which
    provider ids are currently disabled."
    """
    return live_admission.disabled_provider_ids(getattr(application, "engine", None))


_EVENT_TIMEFRAME_SECONDS = {
    "1h": 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "72h": 72 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}
EventTimeframe = Literal["all", "1h", "12h", "24h", "72h", "7d", "30d"]
EventLevel = Literal["info", "warning", "warn", "error"]
EventCategory = Literal[event_journal.CATEGORIES]
_SOURCE_PROJECTION_FIELDS = (
    "_source_request_payload",
    "_root_request_kinds",
    "_delivered_candidate_source",
    "_active_candidate_source",
    "_delivered_candidate_input",
    "_active_candidate_input",
    "_route_candidate_summary",
)


def _decode_projection_value(value, default=None):
    try:
        return codec.load(value, default)
    except (TypeError, ValueError, KeyError):
        return default


def _search_plan(raw: Optional[str]):
    """Both lists' one reading of what the operator typed; a search the
    language cannot answer is a 400 ``invalid_query`` with a safe message."""
    try:
        return search_contract.plan(raw)
    except search_contract.InvalidSearch as exc:
        # ``code`` stays the machine identifier; ``message_code`` names these words (WS3e).
        raise HTTPException(status_code=400, detail={"code": "invalid_query", "message": str(exc),
                                                     **_message_code(exc)}) from None














def _bounded_source_identity(row) -> dict[str, str]:
    """Derive the safe list icon identity without comprehensive presentation."""
    request_kind = ""
    request_payload = _decode_projection_value(row.get("_source_request_payload"), None)
    if isinstance(request_payload, dict):
        try:
            request_kind = str(codec.request(request_payload).kind or "").strip().lower()
        except (TypeError, ValueError, KeyError):
            request_kind = ""

    # The candidate in use (the delivered one once completed, else the active
    # one), read as the input it came from (presentation_repository
    # candidate_input_sql); with none in use, the root request.
    completed = str(row.get("status") or "").strip().lower() == "completed"
    in_use = row.get("_delivered_candidate_input") if completed else None
    request_kind = request_kind_of(in_use or row.get("_active_candidate_input")) or request_kind

    base_identity = public_source_identity(request_kind)
    if base_identity.get("kind") in {"magnet", "torrent_file"}:
        return base_identity

    candidate_source = None
    if completed:
        delivered = _decode_projection_value(row.get("_delivered_candidate_source"), None)
        if public_source_identity(request_kind, delivered).get("kind") == "host":
            candidate_source = delivered

    if candidate_source is None:
        active = _decode_projection_value(row.get("_active_candidate_source"), None)
        if public_source_identity(request_kind, active).get("kind") == "host":
            candidate_source = active

    if candidate_source is None:
        candidates = _decode_projection_value(row.get("_route_candidate_summary"), [])
        if isinstance(candidates, list):
            for candidate in candidates:
                source = candidate.get("source") if isinstance(candidate, dict) else None
                if public_source_identity(request_kind, source).get("kind") == "host":
                    candidate_source = source
                    break

    return public_source_identity(request_kind, candidate_source)


def _artifact_facts(raw_facts) -> list[dict]:
    """The page's per-artifact facts (``artifact_presentation_facts``),
    decoded; in artifact id order, the order the selection reads."""
    try:
        facts = json.loads(raw_facts) if raw_facts else []
    except (TypeError, ValueError):
        facts = []
    if not isinstance(facts, list):
        return []
    return sorted((fact for fact in facts if isinstance(fact, dict)), key=lambda fact: int(fact.get("artifact_id") or 0))


def _bounded_child_presentations(facts, *, paused, input_required, capacity_only_blocked_ids):
    """Project the page's per-artifact child presentations for the shared owner.

    ``facts`` is the decoded JSON array the bounded projection built from raw
    durable facts (each artifact's id, status, plus its latest recovery-
    snapshot fields) already restricted to canonical artifact membership
    (Section 7). Each entry is passed through the same
    ``recovery_presentation`` projector the comprehensive Details path uses,
    with the Section 9 capacity-wait fact reduced to a single set-membership
    check against ``capacity_only_blocked_ids`` -- the execution-admission
    owner's own positive record (``transfers.convergence_engine
    .TransferEngine.capacity_only_blocked_ids``) -- so this can never derive a
    capacity-wait classification the real dispatch path didn't itself assert.
    Each fact's ``awaiting_switched_provider`` is the projection's own
    ``replacement_held`` fact (``TransferRepository._replacement_held_artifacts``,
    derived in the same one query): a file held for an operator switch's
    landing waits for that switch's provider.
    """
    presentations = []
    for fact in facts:
        context = {key: fact.get(key) for key in ARTIFACT_PRESENTATION_SNAPSHOT_KEYS}
        artifact_id = fact.get("artifact_id")
        presentations.append(recovery_presentation(
            fact.get("status"), context, paused=paused, input_required=input_required,
            capacity_only_blocked=artifact_id in capacity_only_blocked_ids,
            awaiting_switched_provider=bool(fact.get("awaiting_switched_provider")),
        ))
    return presentations


# DP 1.0.12 Workstream B (Section 10): the bounded list projects the same
# durable facts the fresh-click read endpoint uses and classifies them through
# the one canonical domain function, transfers.file_selection
# .file_selection_affordance -- shared by Details, Dashboard Recent, and
# Downloads, never a per-surface eligibility heuristic. Never inferred from
# provider name, filename shape, UI glyph, or an open connection.


class DiscardConfirmationBody(BaseModel):
    """The exact consequence the operator confirmed (from the refusal or the
    preview): honored only while it is still current."""
    material_generation: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)


def _message_code(exc: Exception) -> dict:
    """The code naming a structured refusal's words, where its cause names one
    (``message_code`` and ``params``; the machine ``code`` beside it is unchanged)."""
    named, params = disclosed(str(exc), *code_of(exc))
    return {"message_code": named, "params": params} if named != UNCLASSIFIED else {}


def _switch_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DiscardConfirmationRequired):
        return HTTPException(status_code=409, detail={
            "confirmation": "discard_material", "discarded_bytes": exc.discarded_bytes,
            "retained_bytes": exc.retained_bytes, "material_generation": exc.material_generation,
            "unusable_bytes": exc.unusable_bytes, "abandoned_bytes": exc.abandoned_bytes,
            "changed": exc.changed,
            "message": ("Downloaded progress changed after the switch was confirmed; nothing was switched."
                        if exc.changed else "Switching to this source discards downloaded progress."),
            "code": "switch.discard_changed" if exc.changed else "switch.discard_confirmation",
        })
    if isinstance(exc, KeyError):
        return DisplayError(404, "Transfer not found", code="transfer.not_found")
    missing = exc.error.category in {Category.RESOURCE_NOT_FOUND, Category.SOURCE_NOT_FOUND}
    return HTTPException(status_code=404 if missing else 409, detail=exc.error.as_dict())


@router.post("/torrents/{transfer_id}/artifacts/{artifact_id}/candidate")
async def activate_artifact_candidate(
    transfer_id: int,
    artifact_id: int,
    candidate_id: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
    discard_confirmed: Annotated[bool, Body(embed=True)] = False,
    discard_confirmation: Annotated[Optional[DiscardConfirmationBody], Body(embed=True)] = None,
    *,
    request: Request,
    application: ApplicationService = Depends(get_application),
):
    """Request activation of one exact existing canonical acquisition candidate.

    When the requested source cannot continue from the artifact's existing
    valid material, nothing changes until the operator confirms the discard
    (``discard_confirmed``); the refusal reports exactly how much is lost. A
    confirmation carrying the consequence it confirmed is refused
    (``changed``) once that consequence is no longer current."""
    expected = await require_destruction(request, application.repository, transfer_id)
    try:
        return await switch_candidate(
            application, transfer_id, artifact_id, candidate_id, discard_confirmed=discard_confirmed,
            expected_demand=expected,
            discard_confirmation=discard_confirmation.model_dump() if discard_confirmation else None)
    except (DiscardConfirmationRequired, KeyError, TransferError) as exc:
        raise _switch_http_error(exc) from None


@router.get("/torrents/{transfer_id}/artifacts/{artifact_id}/candidate/preview")
async def preview_artifact_candidate(
    transfer_id: int,
    artifact_id: int,
    candidate_id: Annotated[str, Query(min_length=1, max_length=128)],
    application: ApplicationService = Depends(get_application),
):
    """Read-only: what switching to this exact candidate would keep and
    discard now, from the same continuation planner the switch uses. Nothing
    is changed, reserved or promised; the switch itself revalidates."""
    try:
        return await preview_switch(application, transfer_id, artifact_id, candidate_id)
    except (KeyError, TransferError) as exc:
        raise _switch_http_error(exc) from None


@router.get("/torrents/{transfer_id}/route")
async def read_root_route(transfer_id: int, artifact_id: Annotated[Optional[int], Query(ge=1)] = None,
                          application: ApplicationService = Depends(get_application)):
    """Read-only provider status of one root's route: its committed provider
    and every provider of its competition with one status
    (``transfers.manual_route_switch.route_status``) -- a torrent's one root,
    the transfer's only root, or (``artifact_id``) the root that produced that
    file's selected source, named by ``root_id``."""
    try:
        status = await root_route_providers(application, transfer_id, artifact_id=artifact_id)
    except KeyError:
        raise DisplayError(404, "Transfer not found", code="transfer.not_found") from None
    if status is None:
        raise DisplayError(404, "Transfer has no switchable provider route", code="transfer.no_switchable_route")
    definitions = application.definitions
    for item in [status, *status["providers"]]:
        key = "current_provider_id" if item is status else "provider_id"
        name_key = "current_provider_name" if item is status else "provider_name"
        item[name_key] = _provider_display_name(item.get(key), definitions) if item.get(key) else None
    return status


@router.post("/torrents/{transfer_id}/route")
async def replace_root_route(
    transfer_id: int,
    provider_id: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
    expected_provider_id: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
    artifact_id: Annotated[Optional[int], Body(embed=True, ge=1)] = None,
    expected_root_id: Annotated[Optional[str], Body(embed=True, min_length=1, max_length=128)] = None,
    *,
    request: Request,
    application: ApplicationService = Depends(get_application),
):
    """Operator replacement of one root's provider route with ``provider_id``;
    ``expected_provider_id`` is the provider the operator saw as current. A
    transfer of several roots names the file (``artifact_id``) whose producing
    root is switched, and ``expected_root_id`` the root the operator saw; the
    server re-derives it. Refused (nothing changed) with the normalized reason
    when the route or root moved since, the provider cannot take the root now,
    or capacity is not available; the requested provider is never
    substituted."""
    expected = await require_destruction(request, application.repository, transfer_id)
    try:
        return await switch_route_provider(application, transfer_id, provider_id,
                                           expected_provider_id=expected_provider_id, expected_demand=expected,
                                           artifact_id=artifact_id, expected_root_id=expected_root_id)
    except KeyError:
        raise DisplayError(404, "Transfer not found", code="transfer.not_found") from None
    except TransferError as exc:
        raise HTTPException(status_code=409, detail=exc.error.as_dict()) from None


@router.get("/torrents/{transfer_id}/route/transfer")
async def read_transfer_route(transfer_id: int, application: ApplicationService = Depends(get_application)):
    """Read-only provider status of a transfer of several roots, for one
    best-effort transfer-level choice (``transfers.manual_route_switch.
    transfer_route_status``): for each provider, every participating root's
    status and how many roots it may take now. Nothing is reserved; the
    switch re-checks each root."""
    try:
        status = await transfer_route_providers(application, transfer_id)
    except KeyError:
        raise DisplayError(404, "Transfer not found", code="transfer.not_found") from None
    if status is None:
        raise DisplayError(404, "Transfer has no transfer-level provider choice", code="transfer.no_provider_choice")
    definitions = application.definitions
    for item in status["providers"]:
        item["provider_name"] = _provider_display_name(item["provider_id"], definitions)
        for root in item["roots"]:
            current = root.get("current_provider_id")
            root["current_provider_name"] = _provider_display_name(current, definitions) if current else None
    return status


@router.post("/torrents/{transfer_id}/route/transfer")
async def switch_transfer_route(
    transfer_id: int,
    provider_id: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
    expected_routes: Annotated[dict[Annotated[str, Field(min_length=1, max_length=128)],
                                    Annotated[str, Field(min_length=1, max_length=128)]],
                               Body(embed=True, max_length=2048)],
    *,
    request: Request,
    application: ApplicationService = Depends(get_application),
):
    """Best-effort operator choice of ``provider_id`` for a transfer of
    several roots: every root the provider may take is switched by the
    one-root switch, naming the provider the operator saw on it
    (``expected_routes``: root id -> provider). Not atomic: each root's
    outcome is reported, and a refused root never undoes a switched one."""
    expected = await require_destruction(request, application.repository, transfer_id)
    try:
        return await switch_transfer_provider(application, transfer_id, provider_id,
                                              expected_routes=dict(expected_routes), expected_demand=expected)
    except KeyError:
        raise DisplayError(404, "Transfer not found", code="transfer.not_found") from None
    except TransferError as exc:
        raise HTTPException(status_code=409, detail=exc.error.as_dict()) from None


@router.get("/events/count")
async def count_activity_events():
    """How many events the journal retains in all (Settings -> Event Logging)."""
    async with get_db() as db:
        return {"recorded": await event_journal.recorded_count(db)}


@router.get("/events")
async def list_activity_events(
    search: Optional[str] = None,
    level: Optional[EventLevel] = None,
    category: Optional[EventCategory] = None,
    timeframe: EventTimeframe = "all",
    limit: Annotated[int, Query(ge=1, le=event_journal.MAX_PAGE_SIZE)] = event_journal.DEFAULT_PAGE_SIZE,
    before: Annotated[Optional[int], Query(ge=1)] = None,
    snapshot: Annotated[Optional[int], Query(ge=0)] = None,
    after: Annotated[Optional[int], Query(ge=0)] = None,
    last: bool = False,
    window_start: Annotated[Optional[float], Query(ge=0)] = None,
):
    """One newest-first page of the durable event journal, across its whole
    retained history.

    Every filter -- time window, severity, Event Type (category) and search --
    applies before the page limit. ``before`` (the previous page's
    ``next_before``) walks older; ``snapshot`` (returned by the first page)
    freezes the newest edge, so events recorded meanwhile never shift an
    investigation (``newer_available`` reports them). ``search`` is the one
    search language (``core.search_contract``), answered over message, detail
    and transfer name through the derived FTS5 index and over the event's
    transfer and related transfer ids; ``search.status`` is
    ``index_incomplete`` while the index has not yet caught up with every
    event, never silently.

    Within one snapshot, ``after`` (the first item's id) returns the newer
    neighbouring page and ``last`` the final page of the same partition with
    its ``page`` number, from one bounded count of the frozen, filtered
    matches; a final page that cannot be proven (an index still catching up,
    or more matches than the count bound) is refused with 409, never guessed.
    One cursor direction at a time.

    A relative ``timeframe`` is measured from now once, by the investigation's
    first page, which reports that cutoff as ``window_start``; its later pages
    send it back so every one of them -- Last's count included -- filters by
    the same window as the snapshot it shares."""
    if sum((before is not None, after is not None, bool(last))) > 1:
        raise DisplayError(400, "Use only one of before, after or last.", code="activity.one_anchor_only")
    if window_start is not None and timeframe == "all":
        raise DisplayError(400, "window_start applies only to a relative timeframe.", code="activity.window_start_relative_only")
    since = None if timeframe == "all" else (
        float(window_start) if window_start is not None else time.time() - _EVENT_TIMEFRAME_SECONDS[timeframe])
    plan = _search_plan(search)
    if plan is not None and plan.text is not None and event_journal.fts_supported():
        try:
            # Bounded, idempotent: a search first covers what it can of the
            # unindexed tail, and reports whatever remains.
            await event_journal.catch_up()
        except Exception as exc:
            logger.warning("Event index catch-up before search failed: %s", type(exc).__name__)
    severity = "warning" if level == "warn" else level
    async with get_db() as db:
        try:
            return await event_journal.page(db, limit=limit, before=before, snapshot=snapshot, category=category,
                                            severity=severity, since=since, search=plan, after=after, last=last)
        except event_journal.LastPageUnavailable as exc:
            raise HTTPException(status_code=409, detail={"code": "last_page_unavailable",
                                                         "message": str(exc), **_message_code(exc)}) from None
        except event_journal.SearchUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": "search_unavailable", "message": str(exc),
                                                         **_message_code(exc)}) from None
        except sqlite3.DatabaseError as exc:
            if plan is None or plan.text is None:
                raise
            # The derived index is unreadable: drop it for a bounded rebuild
            # by the maintenance cadence. Nothing authoritative is touched.
            logger.warning("Event search index failed and will be rebuilt: %s", type(exc).__name__)
            await event_journal.reset_index()
            raise HTTPException(status_code=503, detail={
                "code": "search_unavailable",
                "message": "The Activity Log search index is being rebuilt. Try the search again shortly.",
                "message_code": "search.index_rebuilding", "params": {}}) from None










def _apply_activity_priority_order(items: list) -> list:
    """Stable-sort an already recency-ordered candidate page into three tiers
    -- known live/actionable, unrecognized, known settled/history (see
    ``_activity_priority_tier``) -- preserving recency inside each tier
    (Python's ``sorted`` is stable). Classification uses the real
    ``effective_presentation()`` output already computed per item by the
    caller -- never badge color, never ``presentation_badge_status``, never a
    re-derived raw-status guess. A presentation status this ordering has
    never seen before can never outrank a known live/actionable item, and is
    never assumed to be settled either.
    """
    return sorted(
        items,
        key=lambda item: _activity_priority_tier(item.get("presentation_status")),
    )


async def _with_access(request: Request | None, application, items: list) -> list:
    """Each row's caller-specific ``access`` (``auth.permissions.transfer_access``),
    for the whole page in one read."""
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None:
        return items
    access = await transfer_access(principal, application.repository, [item["id"] for item in items])
    for item in items:
        item["access"] = access.get(int(item["id"]), {"destroy": False, "own_requests": []})
    return items


@router.get("/torrents")
async def list_operational_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0,
    order: Optional[str] = None,
    application: ApplicationService = Depends(get_application),
    request: Request = None,
):
    plan = _search_plan(search)
    # The page itself is core's Downloads read model (transfers.downloads_read_model);
    # this route adds what only the application holds and shapes the response.
    activity_mode = bool(
        order == _ACTIVITY_ORDER and status is None and plan is None and offset == 0 and limit > 0
    )
    registry = getattr(getattr(application, "engine", None), "registry", None)
    page = await downloads_page(
        getattr(application, "repository", None), status=status, plan=plan, limit=limit, offset=offset, order=order,
        disabled_provider_ids=_disabled_provider_ids(application), routes_judged=registry is not None)
    if "history" in page:
        presentations = await asyncio.gather(
            *(application.repository.presentation(transfer_id) for transfer_id in page["history"]))
        items = [
            _public_transfer_presentation(item, application.definitions)
            for item in presentations
            if item is not None
        ]
        return {"items": await _with_access(request, application, items), "total": page["total"],
                "search": search_contract.describe(plan)}
    rows, total, route_roots = page["rows"], page["total"], page["route_roots"]

    # Section 9: read once for the whole page, never per row. This is a
    # plain passthrough to the execution-admission owner's own positive
    # record (transfers.convergence_engine.TransferEngine
    # .capacity_only_blocked_ids) -- the same live fact the comprehensive
    # Details path reads via api.routes.get_torrent -- never a
    # presentation-side reconstruction from durable columns.
    capacity_only_blocked_ids = live_admission.capacity_only_blocked_ids(getattr(application, "engine", None))
    # Likewise the admission owner's record of promoted transfers waiting
    # only on a restart nobody confirmed: the row offers the reaffirmation.
    restart_consent_ids = live_admission.restart_consent_ids(getattr(application, "engine", None))
    # Roots routed from the same facts share one routing decision on this page.
    route_memo: dict = {}

    items = []
    for row in rows:
        projected = dict(row)
        projected["queue_member"] = bool(projected.get("queue_member"))
        projected["queue_restart_consent"] = projected["queue_member"] and int(projected["id"]) in restart_consent_ids
        current_root_resource_state = projected.pop("_current_root_resource_state", None)
        paused = bool(int(projected.pop("_paused_intent", 0) or 0))
        challenge_row = _decode_projection_value(projected.pop("_input_challenge", None), None)
        input_required = isinstance(challenge_row, dict)
        # The modal acts on this: the same canonical public projection the
        # single-transfer API returns -- descriptors and facts, never input.
        projected["input_required"] = public_challenge(challenge_row) if input_required else None
        projected["active_execution_progress"], projected["active_execution_basis"] = active_execution_projection(
            projected, prefix="_")
        for name in ("completed", "total", "writers", "acquiring", "bytes_known", "units_known",
                     "units_completed", "units_total", "estimates_known", "estimated"):
            projected.pop(f"_execution_{name}", None)
        artifact_facts = _artifact_facts(projected.pop("_artifact_presentation_facts", None))
        file_presentations = _bounded_child_presentations(
            artifact_facts,
            paused=paused, input_required=input_required,
            capacity_only_blocked_ids=capacity_only_blocked_ids,
        )
        # The failure a reason names (``failure_selection``), each error its category.
        request_failure = (projected.pop("_request_failure_state", None),
                           projected.pop("_request_failure_category", None),
                           projected.pop("_request_failure_retry_at", None))
        failure = (projected.pop("_transfer_error_category", None),
                   [request_failure] if request_failure[0] else [],
                   [(fact.get("status"), fact.get("error_category")) for fact in artifact_facts])
        source_identity = _bounded_source_identity(projected)
        deferred_provider_id = projected.pop("_deferred_provider_id", None)
        deferred_source = _decode_projection_value(projected.pop("_deferred_source", None), None)
        common_candidate_count = max(0, int(projected.get("common_candidate_count") or 0))
        group_remaining_count = max(0, int(projected.get("group_remaining_count") or 0))
        raw_filenames = _decode_projection_value(projected.pop("_group_member_filenames", None), [])
        artifact_filenames = raw_filenames if isinstance(raw_filenames, list) else []
        # A torrent/magnet submission's root name is its real canonical
        # identity (durable submission-kind fact, from the same
        # source_identity the icon already uses — never inferred from
        # filename shape/count/provider). It must win over any single member
        # artifact filename (Workstream C name-regression correction).
        display_name = transfer_display_name(
            artifact_filenames,
            root_name=projected.get("name"),
            root_is_canonical_identity=source_identity.get("kind") in {"magnet", "torrent_file"},
        )
        # Candidate-action scope classifier (DP 1.0.12 Contextual Candidate
        # Action Scope task, §4/§10): orthogonal to common_candidate_count/
        # group_remaining_count above -- a THIRD read fact, never a
        # replacement. Precedence is intentional and must not be reordered:
        # exactly one movable artifact always wins over a group target, even
        # when a (necessarily disjoint, since STRICT_COMMON_TARGETS is never
        # derived from the movable subset) group target also exists.
        movable_artifact_count = int(projected.pop("_movable_artifact_count", 0) or 0)
        single_movable_artifact_id = projected.pop("_single_movable_artifact_id", None)
        single_movable_artifact_candidate_count = projected.pop(
            "_single_movable_artifact_candidate_count", None
        )
        actionable_common_target_count = int(
            projected.pop("_actionable_common_target_count", 0) or 0
        )
        file_selection_manifest_id = projected.pop("_file_selection_manifest_id", None)
        file_selection_decision = projected.pop("_file_selection_decision", None)
        file_selection_request_kind = projected.pop("_file_selection_request_kind", None)
        file_selection_committed_at = projected.pop("_file_selection_committed_at", None)
        file_selection_entry_count = int(projected.pop("_file_selection_entry_count", 0) or 0)
        file_selection_explicit_only = bool(projected.pop("_file_selection_explicit_only", 0))
        if movable_artifact_count == 1:
            candidate_action_scope = "artifact"
            candidate_action_count = int(single_movable_artifact_candidate_count or 0)
            candidate_action_artifact_id = (
                int(single_movable_artifact_id) if single_movable_artifact_id is not None else None
            )
        elif movable_artifact_count > 1 and actionable_common_target_count > 0:
            candidate_action_scope = "group"
            candidate_action_count = actionable_common_target_count
            candidate_action_artifact_id = None
        else:
            candidate_action_scope = "none"
            candidate_action_count = 0
            candidate_action_artifact_id = None
        request_kinds = _bounded_request_kinds(projected)
        roots = route_roots.get(int(projected["id"]), {})
        facts = next(iter(roots.values())) if len(roots) == 1 else None
        if torrent_root(facts):
            projected["route_provider_id"] = facts["current"]
        for field in _SOURCE_PROJECTION_FIELDS:
            projected.pop(field, None)
        item = _public_transfer_presentation(projected, application.definitions)
        item["current_source_identity"] = source_identity
        if torrent_root(facts):
            # The same selectability the picker shows: a launcher only when a
            # provider other than the current one may legitimately take it.
            item["route_switch_available"] = bool(registry is not None and switch_available(registry, facts))
        elif registry is not None and (scope := route_switch_offer(registry, roots, route_memo)) is not None:
            # Any other transfer: a launcher only while one of its roots that
            # produced a selected source may legitimately move. ``root`` opens
            # its one root's picker; ``transfer`` opens the transfer's
            # best-effort provider picker (``/route/transfer``).
            item["route_switch_available"] = True
            item["route_switch_scope"] = scope
        # An operator's pending provider choice is a preference awaiting that
        # provider's copy (transfers.provider_choice), never the committed
        # provider: presented apart from ``active_provider_id``.
        pending_providers = sorted({choice["provider_id"] for facts in roots.values()
                                    for choice in facts.get("pending_choices", ())})
        if pending_providers:
            item["pending_provider_ids"] = pending_providers
            item["pending_provider_names"] = [_provider_display_name(provider_id, application.definitions)
                                              for provider_id in pending_providers]
        # A source switch accepted while the download is paused, which Resume
        # completes (``deferred_source``): its destination, never the provider
        # or source the paused download is on -- those stay the committed ones.
        # Only the destination's own provider and safe public host; nothing
        # when the provider is unknown, a host only when the source is one.
        if deferred_provider_id:
            host = None
            if isinstance(deferred_source, dict) and str(deferred_source.get("scope") or "") == "host":
                host = safe_public_host(str(deferred_source.get("key") or "")) or None
            item["deferred_switch"] = {
                "provider_id": str(deferred_provider_id),
                "provider_name": _provider_display_name(str(deferred_provider_id), application.definitions),
                "host": host,
            }
        item["request_kinds"] = request_kinds
        # Transfer-level common-source MEMBERSHIP summary. ``common_candidate_count``
        # is the number of canonical hosts common to every current authoritative
        # artifact of the transfer — the raw intersection, independent of
        # switch-eligibility or artifact state. The group Candidates launcher
        # appears only when this is 2 or more; whether any given common host is
        # currently actionable is a separate fact derived lazily from Details
        # when the chooser opens, never encoded in this bounded field. Feeds
        # Downloads and Dashboard Recent Items identically.
        item["common_candidate_count"] = common_candidate_count
        # Remaining-work membership count (Section 10): 0 means the list
        # surfaces render the common-source indicator as static history
        # instead of an interactive launcher. Independent of actionability.
        item["group_remaining_count"] = group_remaining_count
        # Canonical human-facing transfer title (Section 15-20; narrowed by
        # the DP 1.0.12 Workstream C name-regression correction). Dashboard
        # Recent and Downloads both render this SAME field — no per-surface
        # normalization duplication. Its parts let the browser say a
        # multi-file name in the reader's language.
        item["display_name"], item["display_name_parts"] = display_name
        # Candidate-action scope/count/target (Section 3): the smallest
        # unambiguous candidate-switch operation scope the operator can act
        # on right now. Orthogonal to common_candidate_count/
        # group_remaining_count above -- never overwrites them, never derived
        # from them, never derived from a narrowed "movable" membership set.
        item["candidate_action_scope"] = candidate_action_scope
        item["candidate_action_count"] = candidate_action_count
        item["candidate_action_artifact_id"] = candidate_action_artifact_id
        # File-selection affordance hint (Section 10): an AFFORDANCE HINT
        # only, never mutation authority -- the browser always performs one
        # fresh authoritative GET .../file-selection read on click before
        # opening any picker (Section 6.8).
        item["file_selection_affordance"] = fs.file_selection_affordance(
            file_selection_manifest_id,
            file_selection_decision,
            file_selection_committed_at,
            file_selection_entry_count,
            awaiting_manifest=fs.awaits_manifest(file_selection_request_kind),
            explicit_only=file_selection_explicit_only,
        )
        # Effective processing presentation via the ONE shared owner
        # (transfers.presentation_repository.effective_presentation), fed the same
        # logical inputs as the comprehensive Details projection: the durable
        # transfer status, the per-artifact child presentations, the pause /
        # input-required signals, and the state of the CURRENT authoritative root
        # provider-resource binding. The durable ``status`` is never mutated;
        # Dashboard, Downloads and Details cannot disagree for the same facts.
        item.update(effective_presentation(
            str(projected.get("status") or ""),
            file_presentations,
            paused=paused,
            input_required=input_required,
            current_resource_state=current_root_resource_state,
            failure=failure,
        ))
        items.append(item)
    # The estimated time remaining of each row acquiring now, from the one
    # owner (``TransferEngine.acquisition_etas``): one bounded read for the
    # page and the meter's memory, never an estimate of this projection's own.
    engine = getattr(application, "engine", None)
    acquiring = [int(item["id"]) for item in items
                 if item.get("status") == "downloading" and item.get("presentation_status") == "downloading"]
    etas = await engine.acquisition_etas(acquiring) if engine is not None and acquiring else {}
    for item in items:
        item["acquisition_eta"] = acquisition_eta_payload(etas.get(int(item["id"])))
    if activity_mode:
        items = _apply_activity_priority_order(items)
    return {"items": await _with_access(request, application, items), "total": total,
                "search": search_contract.describe(plan)}
