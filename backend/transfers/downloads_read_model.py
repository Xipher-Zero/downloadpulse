"""The Downloads collection's read model -- the transfer core's own.

One bounded page of transfers for the Downloads list and the Dashboard's Recent
Activity: the filters, the text search (evaluated by SQLite once per request), the
orderings (chronological, the operator queue, Recent Activity's bounded cohort) and
every list-only enrichment in one statement over core's tables. The Downloads route
(``api.operational_downloads``) supplies what only the application holds -- the search
plan, the disabled providers, whether a registry judges route alternatives -- and
shapes the response; it never queries core's tables itself
(``tests/test_v102_core_boundaries.py``; operator, 2026-11-22). Moved here, unchanged,
from that route.
"""
import json

from core import search_contract
from core.presentation_safety import safe_public_host
from db.database import get_db
from transfers import file_selection as fs  # noqa: F401 -- the page statement's file-selection facts
from transfers.display_name import normalized_transfer_display_name
from transfers.manual_failover import switch_eligible_sql
from transfers.models import BITTORRENT_REQUEST_KINDS
from transfers.presentation_repository import candidate_inputs_sql, public_source_identity
from transfers.read_models import (
    ENDED_ROUTE_STATES as _ENDED_ROUTE_STATES, MUTATING_EXECUTION_STATES as _MUTATING_EXECUTION_STATES,
    QUEUE_MEMBER_SQL as _QUEUE_MEMBER_SQL, active_execution_progress_sql, canonical_artifact_membership_sql,
    root_route_facts_by_root_in,
)

# The bounded list's remaining-work signal (Section 10 of the DP 1.0.12
# presentation task) reuses the SAME cheap artifact-state classification the
# comprehensive Details presentation already uses for its own read-time
# ``switch_eligible`` approximation -- the ONE canonical owner
# ``transfers.manual_failover.switch_eligible``, in its SQL form
# (DP 1.0.12 recovery leveling, Section 31) -- rather than a fourth
# hand-copied literal list. A
# non-completed artifact that is NOT eligible (e.g. ``cancelled``,
# ``input_required``, a ``recovery_wait`` for storage) has no acquisition path
# a candidate switch could ever help with, so it must not count as remaining
# work; a status of ``error`` IS included, matching this codebase's own
# established distinction between a recoverable failure (still switchable)
# and a terminal one, and so is a timed retry backoff of the route that
# failed. This is
# deliberately NOT the full authoritative actionability check — live
# provider health / route binding / candidate-expiry are out of bounded-SQL
# scope by design (see the group_common_sources comment below) and are only
# ever re-validated at actual switch time by manual_candidate_failover.py.
_SWITCHABLE_SQL = switch_eligible_sql("status", "quiescence_reason")

_GMA_SWITCHABLE_SQL = switch_eligible_sql("gma.status", "gma.quiescence_reason")

# Route-attempt states that end a route, from their one owner
# (transfers._repository_base._ENDED_ROUTE_STATES): never a current provider.
_ENDED_ROUTE_STATES_SQL = ", ".join(f"'{state}'" for state in sorted(_ENDED_ROUTE_STATES))

# Execution states of a live writer, from their one owner
# (transfers._repository_base._MUTATING_EXECUTION_STATES).
_MUTATING_EXECUTION_STATES_SQL = ", ".join(f"'{state}'" for state in sorted(_MUTATING_EXECUTION_STATES))

# The one canonical actionable-artifact filter (DP 1.0.12 recovery leveling,
# Section 7), shared with transfers._repository_base.TransferRepository.artifacts()
# so lifecycle aggregation and this bounded list's presentation-vote facts can
# never again silently diverge onto different child sets.
_CANONICAL_ARTIFACT_SQL = canonical_artifact_membership_sql("f")

# DP 1.0.12 UI Finishing (Correction 2 -- Dashboard Recent Activity priority).
#
# Candidate ACQUISITION (which raw rows are even fetched) is split by raw
# ``torrents.status`` into two small, closed, fixed sets -- every raw value
# transfers.models.TransferState can persist, partitioned by the one
# structural fact that actually determines the split: transfer_pause_intents
# and transfer_input_challenges rows (the two facts that can make
# effective_presentation diverge from raw status into "paused"/
# "input_required") are only ever written against, and only ever read for,
# a transfer while its lifecycle is still open -- see
# transfers.engine/convergence_engine's terminal-transition paths, which
# retire both before/at the same transition that raises torrents.status into
# one of _SETTLED_RAW_STATUSES. A transfer already at a settled raw status
# cannot carry a live pause intent or input challenge (regression-proven in
# test_dashboard_recent_activity_priority.py::
# test_settled_raw_status_transfer_cannot_carry_a_live_pause_or_input_challenge).
# This is what makes the raw-status split below safe to use for candidate
# ACQUISITION while effective_presentation() remains the sole authority for
# priority CLASSIFICATION (see _activity_priority_tier below) -- acquisition
# only ever needs to decide "is this row even worth fetching", never "which
# cohort does it ultimately belong to".
_ACTIVITY_LIVE_RAW_STATUSES = (
    "pending", "processing", "input_required", "ready", "queued",
    "downloading", "paused", "verifying", "extracting",
)

_SETTLED_RAW_STATUSES = ("completed", "error", "failed", "lost", "cancelled", "deleted", "consolidated")

# Downloads' text search, evaluated by SQLite on every row the list's other
# filters admit, before the page is cut: the fields a Downloads row and its
# Details present, read from the transfer's own durable rows -- the root
# request's kind and name, the canonical artifacts' file names (from which the
# displayed name is derived, by the same owner the list uses), the label, the
# hash as displayed, the current error message, the canonical artifacts' source
# hosts and the names of transfers consolidated into this one. Never a request
# URL or payload, a credential or a provider's native detail. It runs ONCE per
# request, into a table private to the request's own connection, which the
# page and its total then both read.
_DOWNLOADS_MATCH = "dp_downloads_search_match"

_DOWNLOADS_HITS = "dp_downloads_search_hits"

_DOWNLOADS_MATCH_SQL = f"""{_DOWNLOADS_MATCH}(?,
            t.name,
            (SELECT json_extract(r.payload, '$.kind') FROM transfer_requests r WHERE r.transfer_id = t.id
             ORDER BY CASE WHEN r.parent_id IS NULL THEN 0 ELSE 1 END, r.ordinal, r.id LIMIT 1),
            (SELECT json_group_array(member.filename) FROM (
                SELECT f.filename FROM download_files f WHERE f.torrent_id = t.id AND {_CANONICAL_ARTIFACT_SQL}
                ORDER BY f.id) member),
            t.label,
            CASE WHEN t.hash LIKE 'deleted:%' OR t.hash LIKE 'retired:%' THEN COALESCE(t.source_fingerprint, '')
                 ELSE t.hash END,
            t.error_message,
            (SELECT json_group_array(b.source_key) FROM download_files f
             JOIN canonical_candidate_bindings b ON b.canonical_artifact_id = f.id
             WHERE f.torrent_id = t.id AND {_CANONICAL_ARTIFACT_SQL} AND lower(COALESCE(b.source_scope, '')) = 'host'),
            (SELECT json_group_array(contributor.name) FROM artifact_consolidations c
             JOIN download_files f ON f.id = c.canonical_artifact_id
             JOIN torrents contributor ON contributor.id = c.source_transfer_id
             WHERE f.torrent_id = t.id))"""

# Joins the fields into one text folded once. The search language admits no
# control character, so no match can span two fields.
_FIELD_SEPARATOR = "\x01"

def _json_strings(value) -> list[str]:
    try:
        decoded = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded if item] if isinstance(decoded, list) else []

def _downloads_search_match(needle, root_name, request_kind, filenames, label, display_hash, error_message,
                            hosts, contributors) -> int:
    names = _json_strings(filenames)
    fields = (root_name, label, display_hash, error_message, *names, *_json_strings(contributors))
    if needle in search_contract.fold(_FIELD_SEPARATOR.join(field for field in fields if field)):
        return 1
    # A safe host is its raw value lower-cased and trimmed, so it can only
    # match where the raw value does.
    for host in _json_strings(hosts):
        if needle in search_contract.fold(host) and needle in (safe_public_host(host) or ""):
            return 1
    # The displayed name is the root name or a file name (both matched above)
    # unless it names a collection -- a file name or a prefix of one, then
    # " + N files" -- or nothing at all, so only those can add a match: one
    # reaching into " + N files" holds its "+" or lies within it.
    if len(names) < 2:
        if names or root_name:
            return 0
    elif "+" not in needle and all(needle not in f" + {count} files" for count in (len(names), len(names) - 1)):
        return 0
    displayed = normalized_transfer_display_name(
        names, root_name=root_name,
        root_is_canonical_identity=public_source_identity(request_kind).get("kind") in {"magnet", "torrent_file"})
    return int(needle in search_contract.fold(displayed))

def _downloads_search_clause(plan) -> tuple[str, list]:
    """The plan as one predicate over ``torrents t``: the exact id alone, or
    membership in the request's evaluated text search (which includes the
    exact id of a bare number) -- one row per transfer however many of its
    fields match."""
    if plan.text is None:
        return "t.id = ?", [plan.transfer_id]
    return f"t.id IN (SELECT id FROM temp.{_DOWNLOADS_HITS})", []

async def _prepare_search(db, plan, scope: tuple[list, list]) -> None:
    """Evaluate a text search once on ``db``, over the collection ``scope``
    (the list's other filters) admits."""
    if plan is None or plan.text is None:
        return
    await db.create_function(_DOWNLOADS_MATCH, 9, _downloads_search_match)
    await db.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_DOWNLOADS_HITS}(id INTEGER PRIMARY KEY)")
    await db.execute(f"DELETE FROM temp.{_DOWNLOADS_HITS}")
    clauses, params = scope
    match = [_DOWNLOADS_MATCH_SQL]
    values = [search_contract.fold(plan.text)]
    if plan.transfer_id is not None:
        match.insert(0, "t.id = ?")
        values.insert(0, plan.transfer_id)
    where = " AND ".join([*clauses, "(" + " OR ".join(match) + ")"])
    await db.execute(f"INSERT INTO temp.{_DOWNLOADS_HITS}(id) SELECT t.id FROM torrents t WHERE {where}",
                     [*params, *values])

def _bounded_request_kinds(row) -> list[str]:
    """The distinct canonical root request kinds carried by one bounded row.

    Separates the submission CHANNEL (``source``) from what was actually
    submitted, which is what "Submitted As" reports. Derived from the durable
    request payload the projection already reads; never a second stored field.
    """
    raw = str(row.get("_root_request_kinds") or "")
    return sorted({part.strip().lower() for part in raw.split(",")} - {""})

_ACTIVITY_ORDER = "activity"

# Downloads' Priority view: the operator queue (``TransferRepository.queue``)
# -- unfinished transfers by durable priority, then the older transfer -- and
# after it the settled history, most recently settled first (completion time,
# else the admission time: a cancellation records none), ordered over the
# complete filtered collection BEFORE the page is cut from it. Without it the
# collection is chronological, newest first, exactly as before.
_QUEUE_ORDER = "queue"

_QUEUE_MEMBER = _QUEUE_MEMBER_SQL.replace("status", "t.status")

_QUEUE_ORDER_SQL = f"""CASE WHEN {_QUEUE_MEMBER} THEN 0 ELSE 1 END,
            CASE WHEN {_QUEUE_MEMBER} THEN -t.priority END,
            CASE WHEN {_QUEUE_MEMBER} THEN t.id END,
            COALESCE(t.completed_at, t.created_at) DESC, t.id DESC"""

async def _bounded_status_candidates(db, base_where: str, params: list, statuses: tuple, limit: int) -> list:
    """One indexed SEEK per raw status value, via ``idx_torrents_status_created
    (status, created_at)`` -- never a scan across the whole excluded/included
    status set. Each of the ``len(statuses)`` queries SEEKs directly to that
    status's slice of the index and walks it in ``created_at`` DESC order,
    stopping at ``LIMIT`` -- work bounded by ``len(statuses) x limit``, a
    small fixed constant, never by total table size. ``len(statuses)`` is
    itself fixed (9 live raw statuses or 7 settled raw statuses -- see
    ``_ACTIVITY_LIVE_RAW_STATUSES`` / ``_SETTLED_RAW_STATUSES`` above), so
    this is still a bounded, fixed number of SQL statements regardless of
    history size. Returns ``(id, created_at)`` pairs (not yet re-sorted
    across statuses or truncated -- the caller merges/truncates once all
    cohort sources are collected).
    """
    rows = []
    for status in statuses:
        sql = f"""SELECT t.id, t.created_at
            FROM torrents t INDEXED BY idx_torrents_status_created
            WHERE {base_where} AND t.status = ?
            ORDER BY t.created_at DESC LIMIT ?"""
        found = await db.fetchall(sql, [*params, status, limit])
        rows.extend((row["id"], row["created_at"]) for row in found)
    return rows

def _newest_first_unique_ids(pairs: list, limit: int) -> list:
    """Merge ``(id, created_at)`` pairs from multiple bounded sources,
    de-duplicate (a transfer can legitimately appear in more than one
    source query), sort newest-first, and cap at ``limit``. Pure in-memory
    work over an already-bounded input set -- no additional DB round trip.
    """
    by_id = {}
    for transfer_id, created_at in pairs:
        if transfer_id not in by_id or created_at > by_id[transfer_id]:
            by_id[transfer_id] = created_at
    ordered = sorted(by_id.items(), key=lambda pair: pair[1], reverse=True)
    return [transfer_id for transfer_id, _created_at in ordered[:limit]]

async def _activity_cohort_candidate_ids(where_clauses: list, params: list, limit: int) -> list:
    """Bounded candidate-id acquisition for Dashboard Recent Activity
    priority ordering (Correction 2). Every query below is a fixed, bounded
    number of indexed SEEK-and-walk statements (never a scan/sort
    proportional to total history size -- see the boundedness proof in the
    finishing-pass report), and the total candidate id count returned is
    always ``<= limit``.

    1. One SEEK per known live raw status (``_bounded_status_candidates``
       against ``_ACTIVITY_LIVE_RAW_STATUSES``).
    2. Only if step 1 did not already reach ``limit``: one SEEK per settled
       raw status (``_bounded_status_candidates`` against
       ``_SETTLED_RAW_STATUSES``), to fill the remaining room with recent
       settled/history rows.

    DP 1.0.12 leveling remediation (FUNC-001): candidate ACQUISITION used to
    also run a bounded "recovery net" query against ``transfer_pause_intents``
    / ``transfer_input_challenges`` because a settled raw ``torrents.status``
    was not guaranteed to have its pause intent / INPUT_REQUIRED challenge
    retired. That engine-ownership defect is now fixed at the source --
    ``transfers._repository_base.TransferRepository._write_lifecycle_transition``
    (and every direct status-update path that settles a parent) transactionally
    retires both tables -- so a settled raw status can no longer carry either
    row and this bounded list can trust raw ``torrents.status`` again without
    read-model compensation.
    """
    base_where = " AND ".join(where_clauses)
    async with get_db() as db:
        live_pairs = await _bounded_status_candidates(
            db, base_where, params, _ACTIVITY_LIVE_RAW_STATUSES, limit
        )
        live_ids = _newest_first_unique_ids(live_pairs, limit)

        remaining = limit - len(live_ids)
        settled_ids = []
        if remaining > 0:
            settled_pairs = await _bounded_status_candidates(
                db, base_where, params, _SETTLED_RAW_STATUSES, remaining
            )
            settled_ids = _newest_first_unique_ids(settled_pairs, remaining)

    return live_ids + settled_ids

async def downloads_page(repository, *, status, plan, limit: int, offset: int, order, disabled_provider_ids,
                         routes_judged: bool) -> dict:
    """The Downloads collection's page, read once: the filtered, ordered, bounded rows
    with every list-only enrichment in one statement, their total, and each routed
    root's committed route facts. ``plan``: the search plan (``core.search_contract``);
    ``disabled_provider_ids``: the providers currently disabled (an admission fact the
    caller holds); ``routes_judged``: whether a registry can judge every root's route
    alternatives (else only torrent roots are routed). For the explicit
    ``consolidated`` history view: ``{"history": ids, "total": n}``, presented by the
    caller per transfer."""
    clauses = []
    params = []

    if status:
        clauses.append("t.status = ?")
        params.append(status)
    else:
        clauses.append("t.status NOT IN ('deleted', 'consolidated')")

    search_scope = (list(clauses), list(params))
    if plan is not None:
        predicate, predicate_params = _downloads_search_clause(plan)
        clauses.append(predicate)
        params.extend(predicate_params)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # DP 1.0.12 UI Finishing (Correction 2): a narrow, additive ordering mode
    # for Dashboard Recent Activity. It only engages for the plain default
    # bounded-page shape (no explicit status filter, first page, a positive
    # limit) -- exactly what the Dashboard's own dynamic-capacity request
    # always sends. Any other combination (explicit status filter, a nonzero
    # offset, no limit) falls straight through to the unchanged ordinary
    # page_sql below, so default Downloads/list behavior and pagination
    # semantics are untouched.
    activity_mode = bool(
        order == _ACTIVITY_ORDER and status is None and plan is None and offset == 0 and limit > 0
    )

    if activity_mode:
        candidate_ids = await _activity_cohort_candidate_ids(clauses, params, limit)
        if candidate_ids:
            values_sql = ", ".join(f"({int(cid)}, {ordinal})" for ordinal, cid in enumerate(candidate_ids))
            page_sql = f"SELECT column1 AS id, column2 AS page_ordinal FROM (VALUES {values_sql})"
        else:
            page_sql = "SELECT NULL AS id, NULL AS page_ordinal WHERE 0"
        query_params = []
    else:
        ordering = _QUEUE_ORDER_SQL if order == _QUEUE_ORDER else "t.created_at DESC, t.id DESC"
        page_sql = f"""SELECT t.id, ROW_NUMBER() OVER (ORDER BY {ordering}) AS page_ordinal
            FROM torrents t {where}
            ORDER BY {ordering}"""
        query_params = list(params)
        if limit > 0:
            page_sql += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])

    # Explicit consolidated status is a durable-history diagnostic view, not
    # the normal Downloads collection. Preserve its established comprehensive
    # presentation semantics while keeping the default/current list bounded.
    if status == "consolidated":
        async with get_db() as db:
            await _prepare_search(db, plan, search_scope)
            history_rows = await db.fetchall(page_sql, query_params)
            total_row = await db.fetchone(
                f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
            )
            total = total_row["cnt"] if total_row else 0

        return {"history": [int(row["id"]) for row in history_rows], "total": total}

    # Durable, page-global provider-enablement facts (see _disabled_provider_ids
    # docstring) — computed once per request from the already-injected
    # application, never per-row and never an extra DB round-trip.
    disabled_provider_clause = (
        "AND movement.provider_id NOT IN ({})".format(
            ", ".join(f"'{pid}'" for pid in sorted(disabled_provider_ids))
        )
        if disabled_provider_ids
        else ""
    )

    # The normal Downloads collection is a bounded read model. It intentionally
    # does not reconstruct the comprehensive per-transfer presentation used by
    # the detail route. All list-only enrichment is computed in this one SQL read.
    query = f"""
        WITH page AS (
            {page_sql}
        ),
        latest_route AS (
            SELECT transfer_id, provider_id, candidate_summary
            FROM (
                SELECT
                    p.transfer_id,
                    a.provider_id,
                    p.candidate_summary,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id
                        ORDER BY p.ordinal DESC, p.updated_at DESC
                    ) AS row_number
                FROM route_attempt_provenance p
                JOIN resolution_attempts a
                  ON a.id = p.resolution_attempt_id
                JOIN page
                  ON page.id = p.transfer_id
            )
            WHERE row_number = 1
        ),
        -- The current provider: the provider of the most recent LIVE route
        -- (each request's latest attempt not declined, by a provider that
        -- did not decline that request; an ended route is nobody's) -- the
        -- same derivation as transfers._repository_base.current_route_provider.
        declined_attempt AS (
            SELECT DISTINCT d.request_id, d.provider_id
            FROM resolution_attempts d
            JOIN transfer_requests r
              ON r.id = d.request_id
            JOIN page
              ON page.id = r.transfer_id
            WHERE d.state = 'declined'
        ),
        live_route AS (
            SELECT transfer_id, ordinal, provider_id
            FROM (
                SELECT
                    p.transfer_id,
                    p.ordinal,
                    a.provider_id,
                    a.state,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.request_id
                        ORDER BY p.ordinal DESC
                    ) AS request_rank
                FROM route_attempt_provenance p
                JOIN resolution_attempts a
                  ON a.id = p.resolution_attempt_id
                JOIN page
                  ON page.id = p.transfer_id
                LEFT JOIN declined_attempt
                  ON declined_attempt.request_id = p.request_id
                 AND declined_attempt.provider_id = a.provider_id
                WHERE a.provider_id IS NOT NULL AND a.provider_id != ''
                  AND declined_attempt.request_id IS NULL
            ) request_route
            WHERE request_route.request_rank = 1
              AND request_route.state NOT IN ({_ENDED_ROUTE_STATES_SQL})
        ),
        current_route AS (
            SELECT transfer_id, provider_id
            FROM (
                SELECT
                    transfer_id,
                    provider_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY transfer_id
                        ORDER BY ordinal DESC
                    ) AS row_number
                FROM live_route
            )
            WHERE row_number = 1
        ),
        live_route_providers AS (
            SELECT
                transfer_id,
                COUNT(DISTINCT provider_id) AS provider_count,
                MIN(provider_id) AS provider_id
            FROM live_route
            GROUP BY transfer_id
        ),
        -- Each root an operator switched: its latest switch, and the provider
        -- of its committed route now (its latest route attempt not declined)
        -- -- transfers._repository_base._switched_route_providers.
        latest_root_switch AS (
            SELECT p.request_id, MAX(p.ordinal) AS switch_ordinal
            FROM route_attempt_provenance p
            JOIN page ON page.id = p.transfer_id
            WHERE p.operation = 'operator_switch'
            GROUP BY p.request_id
        ),
        switched_root_route AS (
            SELECT sw.request_id, sw.switch_ordinal, cr.provider_id
            FROM latest_root_switch sw
            JOIN (
                SELECT request_id, provider_id
                FROM (
                    SELECT
                        p.request_id,
                        a.provider_id,
                        ROW_NUMBER() OVER (PARTITION BY p.request_id ORDER BY p.ordinal DESC) AS route_rank
                    FROM route_attempt_provenance p
                    JOIN resolution_attempts a ON a.id = p.resolution_attempt_id
                    JOIN page ON page.id = p.transfer_id
                    LEFT JOIN declined_attempt
                      ON declined_attempt.request_id = p.request_id
                     AND declined_attempt.provider_id = a.provider_id
                    WHERE a.provider_id IS NOT NULL AND a.provider_id != ''
                      AND a.state != 'declined' AND declined_attempt.request_id IS NULL
                )
                WHERE route_rank = 1
            ) cr ON cr.request_id = sw.request_id
        ),
        -- The active provider: execution authority, the same derivation as
        -- transfers._repository_base.active_provider. A finished execution
        -- that delivered nothing and whose route an operator switch of its
        -- root replaced names that root's committed route provider now -- unless
        -- a candidate switch since replaced its candidate: then the provider of
        -- the candidate its artifact selects now (_reselected, decided first). Each artifact's own
        -- execution is its latest attempt by the durable per-artifact
        -- provenance ordinal (a replaced attempt never counts); live writers
        -- outrank finished ones; among live writers the latest started, among
        -- finished ones the latest recorded activity. Every attempt at that
        -- instant is kept: no durable sequence orders them further, so more
        -- than one provider (or an unrecorded one) is ambiguous, never broken.
        final_execution AS (
            SELECT
                transfer_id,
                provider_id,
                live,
                CASE WHEN live = 1 THEN started_at ELSE active_at END AS moment
            FROM (
                SELECT
                    p.transfer_id,
                    CASE
                        WHEN NOT (e.authorized = 1 AND e.state IN ({_MUTATING_EXECUTION_STATES_SQL}))
                         AND COALESCE(p.delivered, 0) = 0
                         AND json_extract(sf.candidates, '$[' || COALESCE(sf.selected_candidate, 0) || '].id')
                             IS NOT NULL
                         AND CAST(json_extract(sf.candidates, '$[' || COALESCE(sf.selected_candidate, 0) || '].id')
                             AS TEXT) != COALESCE(p.candidate_id, '')
                        THEN json_extract(sf.candidates, '$[' || COALESCE(sf.selected_candidate, 0) || '].provider_id')
                        WHEN NOT (e.authorized = 1 AND e.state IN ({_MUTATING_EXECUTION_STATES_SQL}))
                         AND COALESCE(p.delivered, 0) = 0
                         AND switched_root_route.switch_ordinal > rp.ordinal
                        THEN switched_root_route.provider_id
                        ELSE p.provider_id
                    END AS provider_id,
                    p.created_at AS started_at,
                    p.updated_at AS active_at,
                    CASE
                        WHEN e.authorized = 1 AND e.state IN ({_MUTATING_EXECUTION_STATES_SQL}) THEN 1
                        ELSE 0
                    END AS live,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id, p.artifact_id
                        ORDER BY p.ordinal DESC
                    ) AS artifact_rank
                FROM execution_attempt_provenance p
                JOIN page
                  ON page.id = p.transfer_id
                LEFT JOIN execution_attempts e
                  ON e.id = p.execution_attempt_id
                LEFT JOIN route_attempt_provenance rp
                  ON rp.resolution_attempt_id = p.route_attempt_id
                LEFT JOIN transfer_requests rq
                  ON rq.id = rp.request_id
                LEFT JOIN switched_root_route
                  ON switched_root_route.request_id = COALESCE(rq.parent_id, rq.id)
                LEFT JOIN download_files sf
                  ON sf.id = p.artifact_id
            )
            WHERE artifact_rank = 1
        ),
        execution_lead AS (
            SELECT
                transfer_id,
                COUNT(DISTINCT COALESCE(provider_id, '')) AS provider_count,
                MIN(provider_id) AS provider_id
            FROM (
                SELECT
                    transfer_id,
                    provider_id,
                    RANK() OVER (
                        PARTITION BY transfer_id
                        ORDER BY live DESC, moment DESC
                    ) AS lead_rank
                FROM final_execution
            )
            WHERE lead_rank = 1
            GROUP BY transfer_id
        ),
        delivery AS (
            SELECT
                p.transfer_id,
                COUNT(DISTINCT p.provider_id) AS provider_count,
                MIN(p.provider_id) AS provider_id
            FROM execution_attempt_provenance p
            JOIN page
              ON page.id = p.transfer_id
            WHERE p.delivered = 1
              AND p.provider_id IS NOT NULL
            GROUP BY p.transfer_id
        ),
        -- The origin provider: the one provider owning the route of every
        -- root request (latest route attempt per root, never a provider that
        -- declined that request) -- the same derivation as
        -- transfers._repository_base.origin_provider.
        declined_route AS (
            SELECT DISTINCT d.request_id, d.provider_id
            FROM resolution_attempts d
            JOIN transfer_requests r
              ON r.id = d.request_id AND r.parent_id IS NULL
            JOIN page
              ON page.id = r.transfer_id
            WHERE d.state = 'declined'
        ),
        root_route AS (
            SELECT request_id, provider_id
            FROM (
                SELECT
                    p.request_id,
                    a.provider_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.request_id
                        ORDER BY p.ordinal DESC
                    ) AS row_number
                FROM route_attempt_provenance p
                JOIN resolution_attempts a
                  ON a.id = p.resolution_attempt_id
                JOIN transfer_requests r
                  ON r.id = p.request_id AND r.parent_id IS NULL
                JOIN page
                  ON page.id = p.transfer_id
                LEFT JOIN declined_route
                  ON declined_route.request_id = p.request_id
                 AND declined_route.provider_id = a.provider_id
                WHERE a.provider_id IS NOT NULL
                  AND declined_route.request_id IS NULL
            )
            WHERE row_number = 1
        ),
        origin AS (
            SELECT
                r.transfer_id,
                COUNT(DISTINCT root_route.provider_id) AS provider_count,
                MIN(root_route.provider_id) AS provider_id,
                SUM(CASE WHEN root_route.request_id IS NULL THEN 1 ELSE 0 END) AS unrouted_count
            FROM transfer_requests r
            JOIN page
              ON page.id = r.transfer_id
            LEFT JOIN root_route
              ON root_route.request_id = r.id
            WHERE r.parent_id IS NULL
            GROUP BY r.transfer_id
        ),
        delivered_picked AS (
            SELECT transfer_id, candidate_source, artifact_id, candidate_id
            FROM (
                SELECT
                    p.transfer_id,
                    p.candidate_source,
                    p.artifact_id,
                    p.candidate_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id
                        ORDER BY e.updated_at DESC, p.ordinal DESC, e.id DESC
                    ) AS row_number
                FROM execution_attempt_provenance p
                JOIN execution_attempts e
                  ON e.id = p.execution_attempt_id
                JOIN page
                  ON page.id = p.transfer_id
                WHERE p.delivered = 1
            ) picked
            WHERE row_number = 1
        ),
        -- The input that contributed each picked candidate (candidate_inputs_sql):
        -- joined and ranked once for the page, never a subquery per row.
        delivered_input AS (
            {candidate_inputs_sql('delivered_picked')}
        ),
        delivered_source AS (
            SELECT d.transfer_id, d.candidate_source, i.payload AS candidate_input
            FROM delivered_picked d
            LEFT JOIN delivered_input i ON i.transfer_id = d.transfer_id AND i.rank = 1
        ),
        active_picked AS (
            SELECT transfer_id, candidate_source, artifact_id, candidate_id
            FROM (
                SELECT
                    f.torrent_id AS transfer_id,
                    p.candidate_source,
                    p.artifact_id,
                    p.candidate_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.torrent_id
                        ORDER BY f.updated_at DESC, p.ordinal DESC, f.id DESC
                    ) AS row_number
                FROM download_files f
                JOIN execution_attempt_provenance p
                  ON p.execution_attempt_id = f.execution_attempt_id
                JOIN page
                  ON page.id = f.torrent_id
                WHERE f.execution_attempt_id IS NOT NULL
                  AND COALESCE(f.mirror_state, '') != 'standby'
            ) picked
            WHERE row_number = 1
        ),
        active_input AS (
            {candidate_inputs_sql('active_picked')}
        ),
        active_source AS (
            SELECT a.transfer_id, a.candidate_source, i.payload AS candidate_input
            FROM active_picked a
            LEFT JOIN active_input i ON i.transfer_id = a.transfer_id AND i.rank = 1
        ),
        -- A source switch accepted while a file's download is parked: the file
        -- SELECTS a candidate its paused, still-authorized writer is not
        -- serving, and Resume completes it (``TransferEngine.pending_source``,
        -- read here from the same durable truth: the writer's own candidate is
        -- re-found by exact id, else by provider and source identity -- a
        -- refresh descendant -- exactly as ``resolve_candidate_index``). Its
        -- destination is the selected candidate's provider and source; one
        -- per transfer, or none when files disagree or it names no provider.
        parked_writers AS (
            SELECT
                f.id AS artifact_id,
                f.torrent_id AS transfer_id,
                f.candidates,
                COALESCE(f.selected_candidate, 0) AS selected,
                e.candidate AS writer
            FROM download_files f
            JOIN page
              ON page.id = f.torrent_id
            JOIN execution_attempts e
              ON e.id = f.execution_attempt_id
             AND e.authorized = 1
             AND e.state = 'paused'
            WHERE {_CANONICAL_ARTIFACT_SQL}
              AND LOWER(TRIM(COALESCE(f.status, ''))) NOT IN ('completed', 'cancelled', 'duplicate')
        ),
        parked_writer_positions AS (
            SELECT
                w.artifact_id,
                w.transfer_id,
                w.candidates,
                w.selected,
                COALESCE(
                    MIN(CASE WHEN json_extract(j.value, '$.id') = json_extract(w.writer, '$.id')
                             THEN CAST(j.key AS INTEGER) END),
                    MIN(CASE WHEN json_extract(w.writer, '$.source_identity') IS NOT NULL
                              AND json_extract(j.value, '$.source_identity') IS NOT NULL
                              AND COALESCE(json_extract(j.value, '$.provider_id'), '')
                                  = COALESCE(json_extract(w.writer, '$.provider_id'), '')
                              AND json_extract(j.value, '$.source_identity.scope')
                                  = json_extract(w.writer, '$.source_identity.scope')
                              AND json_extract(j.value, '$.source_identity.key')
                                  = json_extract(w.writer, '$.source_identity.key')
                             THEN CAST(j.key AS INTEGER) END)
                ) AS writer_position
            FROM parked_writers w, json_each(w.candidates) j
            GROUP BY w.artifact_id, w.transfer_id, w.candidates, w.selected, w.writer
        ),
        deferred_source AS (
            SELECT
                transfer_id,
                MIN(provider_id) AS provider_id,
                MIN(source) AS source
            FROM (
                SELECT
                    transfer_id,
                    json_extract(candidates, '$[' || selected || '].provider_id') AS provider_id,
                    json_extract(candidates, '$[' || selected || '].source_identity') AS source
                FROM parked_writer_positions
                WHERE writer_position IS NULL OR writer_position != selected
            )
            GROUP BY transfer_id
            HAVING COUNT(DISTINCT COALESCE(provider_id, '') || '|' || COALESCE(source, '')) = 1
               AND COUNT(*) = COUNT(provider_id)
               AND MIN(COALESCE(provider_id, '')) != ''
        ),
        root_request AS (
            SELECT transfer_id, payload
            FROM (
                SELECT
                    r.transfer_id,
                    r.payload,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.transfer_id
                        ORDER BY CASE WHEN r.parent_id IS NULL THEN 0 ELSE 1 END,
                                 r.ordinal,
                                 r.id
                    ) AS row_number
                FROM transfer_requests r
                JOIN page
                  ON page.id = r.transfer_id
            )
            WHERE row_number = 1
        ),
        root_request_kinds AS (
            -- The canonical request kinds of each transfer's ROOT requests, as
            -- one bounded aggregate over the page (never a per-row query). The
            -- kind lives in the durable request payload, so nothing is stored
            -- twice and nothing needs a migration; this only surfaces it. It is
            -- the DISTINCT set because one transfer may legitimately own more
            -- than one root request.
            SELECT
                r.transfer_id,
                GROUP_CONCAT(DISTINCT LOWER(COALESCE(json_extract(r.payload, '$.kind'), ''))) AS kinds
            FROM transfer_requests r
            JOIN page
              ON page.id = r.transfer_id
            WHERE r.parent_id IS NULL
            GROUP BY r.transfer_id
        ),
        request_failures AS (
            SELECT
                r.transfer_id,
                COUNT(*) AS failure_count
            FROM transfer_requests r
            JOIN page
              ON page.id = r.transfer_id
            WHERE r.state = 'failed'
            GROUP BY r.transfer_id
        ),
        -- Durable state of the CURRENT authoritative root provider-resource
        -- binding only. The root request's own ``resource`` payload id is
        -- matched to this transfer's binding row: transfer-scoped, so a
        -- predecessor/tombstoned resource on another transfer is excluded, and
        -- a historical binding of THIS transfer is excluded because the current
        -- root request points only at the current resource. ``resource_key``
        -- NULL is the pre-split historical form (primary key IS the canonical
        -- id). Consumed only as a presentation override, never a status mutation.
        current_root_resource AS (
            SELECT rr.transfer_id, pr.state AS resource_state
            FROM (
                SELECT
                    r.transfer_id,
                    r.resource,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.transfer_id
                        ORDER BY r.ordinal, r.id
                    ) AS row_number
                FROM transfer_requests r
                JOIN page
                  ON page.id = r.transfer_id
                WHERE r.parent_id IS NULL AND r.resource IS NOT NULL
            ) rr
            JOIN provider_resources pr
              ON pr.transfer_id = rr.transfer_id
             AND (pr.resource_key = json_extract(rr.resource, '$.id')
                  OR (pr.resource_key IS NULL
                      AND pr.id = json_extract(rr.resource, '$.id')))
            WHERE rr.row_number = 1
        ),
        -- Every current CANONICAL artifact on the page: the exact same
        -- membership predicate transfers._repository_base.TransferRepository
        -- .artifacts() uses for lifecycle aggregation (DP 1.0.12 recovery
        -- leveling, Section 7 -- this CTE previously included blocked/
        -- standby/non-request-bound rows too, which let a non-actionable
        -- child vote in this bounded list's aggregate presentation truth
        -- even though lifecycle aggregation already excluded it; that drift
        -- is exactly what Section 7 requires eliminating). Feeds ONLY the
        -- recovery-snapshot join below and the presentation-vote facts; never
        -- a second definition of current-artifact membership, and never used
        -- for Details' historical/provenance file listing, which still shows
        -- every row.
        page_artifacts AS (
            SELECT f.id AS artifact_id, f.torrent_id AS transfer_id, f.status AS status
            FROM download_files f
            JOIN page ON page.id = f.torrent_id
            WHERE {_CANONICAL_ARTIFACT_SQL}
        ),
        -- Per-artifact presentation facts for the page, folded into the one
        -- bounded read as a JSON array per transfer (one row per transfer, no
        -- per-row query, no comprehensive presentation call). ONLY raw durable
        -- facts are projected here — the artifact's own id/status plus the
        -- fields of its current durable recovery state; no presentation or
        -- precedence logic lives in SQL. Section 9's capacity-wait fact is
        -- NOT projected here at all -- it is a live, execution-admission-owned
        -- fact (transfers.convergence_engine.TransferEngine
        -- .capacity_only_blocked_ids), looked up by artifact_id in Python,
        -- never reconstructed from durable columns. In Python these feed the
        -- SHARED pure owner transfers.presentation_repository
        -- .effective_presentation exactly as the comprehensive Details
        -- projection feeds it, so the bounded list can never derive a
        -- processing truth that disagrees with Details.
        --
        -- DP 1.0.12 recovery leveling, Section 14/20: this is a plain JOIN
        -- against artifact_recovery_state's primary key -- one current row
        -- per artifact -- rather than the pre-leveling window-function
        -- reduction over an unbounded, ever-growing application_events
        -- history (that shape existed only because "current" state used to
        -- be reconstructed by picking the latest of many snapshot events per
        -- artifact; current state is now a single row, so there is nothing
        -- left to rank/partition). Still page-bounded via page_artifacts —
        -- no work proportional to rows outside the requested page.
        -- Files an operator switch HOLDS for its landing: the selected
        -- candidate's every origin is a root (or a member of a root that is
        -- not a BitTorrent root) switched after that origin's attempt -- the
        -- same predicate as transfers._repository_base
        -- ._route_replacement_admission, as joins.
        replacement_held AS (
            SELECT f.id AS artifact_id
            FROM download_files f
            JOIN page ON page.id = f.torrent_id
            JOIN transfer_requests fr ON fr.id = f.request_id AND fr.parent_id IS NULL
            JOIN canonical_candidate_bindings b
              ON b.canonical_artifact_id = f.id
             AND b.candidate_order = COALESCE(f.selected_candidate, 0) + 1
            JOIN canonical_candidate_origins o ON o.binding_id = b.id
            JOIN transfer_requests r ON r.id = o.request_id
            LEFT JOIN transfer_requests rp ON rp.id = r.parent_id
            LEFT JOIN route_attempt_provenance op ON op.resolution_attempt_id = o.resolution_attempt_id
            LEFT JOIN latest_root_switch sw ON sw.request_id = COALESCE(r.parent_id, r.id)
            WHERE f.status NOT IN ('completed', 'deleted')
            GROUP BY f.id
            HAVING COUNT(*) = SUM(CASE
                WHEN (rp.id IS NULL
                      OR LOWER(TRIM(COALESCE(json_extract(rp.payload, '$.kind'), '')))
                         NOT IN ('magnet', 'torrent', 'torrent_file', 'file'))
                 AND r.transfer_id = f.torrent_id
                 AND sw.switch_ordinal > op.ordinal
                THEN 1 ELSE 0 END)
        ),
        artifact_presentation_facts AS (
            SELECT
                pa.transfer_id AS transfer_id,
                json_group_array(json_object(
                    'artifact_id', pa.artifact_id,
                    'status', pa.status,
                    'quiescence_reason', ars.quiescence_reason,
                    'decision_action', ars.decision_action,
                    'last_applied_action', ars.last_applied_action,
                    'decision_reason', ars.decision_reason,
                    'last_applied_reason', ars.last_applied_reason,
                    'wake_condition', ars.wake_condition,
                    'recovery_claim_token', ars.recovery_claim_token,
                    'awaiting_switched_provider', CASE WHEN replacement_held.artifact_id IS NULL THEN 0 ELSE 1 END
                )) AS artifacts
            FROM page_artifacts pa
            LEFT JOIN artifact_recovery_state ars
              ON ars.artifact_id = pa.artifact_id
            LEFT JOIN replacement_held
              ON replacement_held.artifact_id = pa.artifact_id
            GROUP BY pa.transfer_id
        ),
        -- The transfer's one current challenge row (unique per transfer), for
        -- page transfers only; ``transfers.input_required.public_challenge``
        -- projects it exactly as the single-transfer API does.
        -- In-flight execution activity that is not DP material (the one
        -- definition, page transfers only); never completion.
        page_active_execution AS (
            {active_execution_progress_sql("f.torrent_id IN (SELECT id FROM page)")}
        ),
        input_challenge AS (
            SELECT c.transfer_id,
                   json_object(
                       'challenge_id', c.challenge_id, 'transfer_id', c.transfer_id,
                       'generation', c.generation, 'reason', c.reason, 'origin', c.origin,
                       'integration_id', c.integration_id, 'operation_id', c.operation_id,
                       'request_id', c.request_id, 'artifact_id', c.artifact_id,
                       'methods', c.methods, 'facts', c.facts
                   ) AS challenge
            FROM transfer_input_challenges c JOIN page ON page.id = c.transfer_id
        ),
        -- DP 1.0.12 Workstream B: bounded file-selection affordance hint.
        -- The transfer's CURRENT selection generation only (newest by
        -- created_at/id — the exact same "newest wins" rule
        -- transfers.repository.TransferRepository._current_generation uses,
        -- never re-derived), joined once per page, never per-row. Only the
        -- three durable facts the Section 10 classifier needs are projected;
        -- no manifest/entry list crosses into this bounded read.
        page_current_file_selection AS (
            SELECT transfer_id, manifest_id, decision, manifest_committed_at, request_kind
            FROM (
                SELECT
                    s.transfer_id,
                    s.manifest_id,
                    s.decision,
                    s.manifest_committed_at,
                    -- The generation's own root request kind: whether a file
                    -- list always comes (transfers.file_selection.awaits_manifest).
                    -- A primary-key join, never a per-row correlated subquery.
                    LOWER(COALESCE(json_extract(r.payload, '$.kind'), '')) AS request_kind,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.transfer_id ORDER BY s.created_at DESC, s.id DESC
                    ) AS row_number
                FROM transfer_file_selections s
                JOIN page ON page.id = s.transfer_id
                LEFT JOIN transfer_requests r ON r.id = s.request_id
            )
            WHERE row_number = 1
        ),
        -- Entry count of that current generation's bound manifest (0/absent
        -- when no manifest is bound yet) — the same durable manifest-size
        -- fact the fresh-click file-selection read model exposes, never a
        -- filename-shape guess -- and whether its members are independent
        -- resources a collection offers only for an explicit choice.
        page_file_selection_manifest_counts AS (
            SELECT e.manifest_id, COUNT(*) AS manifest_entry_count,
                   MAX(m.independent_members) AS independent_members
            FROM transfer_file_manifest_entries e
            JOIN page_current_file_selection sel ON sel.manifest_id = e.manifest_id
            JOIN transfer_file_manifests m ON m.id = e.manifest_id
            GROUP BY e.manifest_id
        ),
        -- Transfer-level COMMON-SOURCE group MEMBERSHIP summary derived only
        -- from canonical acquisition-candidate storage. Membership is the raw
        -- whole-transfer canonical-host intersection: a host counts as common
        -- when EVERY current authoritative artifact (physical, unblocked,
        -- non-standby — the same current-artifact identity the detail candidate
        -- projection already uses) carries ANY canonical host-scoped candidate
        -- for that host. Membership is deliberately independent of
        -- switch-eligibility, artifact operational state, or which candidate is
        -- currently selected — those are GROUP-ACTIONABILITY facts, computed
        -- lazily from Details when the chooser opens, never here.
        -- ``common_candidate_count`` is the size of that raw intersection; the
        -- group launcher shows only for 2 or more. Host normalization is kept
        -- byte-identical to transfers.repository._group_source_host so this
        -- bounded count and the Details-derived group set agree. Computed once
        -- here: no per-row query, no comprehensive presentation, never a full
        -- candidate/actionability matrix.
        group_member_artifacts AS (
            SELECT
                f.torrent_id AS transfer_id,
                f.id AS artifact_id,
                f.status AS status,
                rs.quiescence_reason AS quiescence_reason,
                f.filename AS filename,
                f.candidates AS candidates,
                f.selected_candidate AS selected_candidate
            FROM download_files f
            JOIN page
              ON page.id = f.torrent_id
            LEFT JOIN artifact_recovery_state rs
              ON rs.artifact_id = f.id
            WHERE {_CANONICAL_ARTIFACT_SQL}
        ),
        group_member_counts AS (
            SELECT transfer_id, COUNT(*) AS artifact_total
            FROM group_member_artifacts
            GROUP BY transfer_id
        ),
        -- Per-artifact currently-SELECTED candidate id, decoded from the same
        -- durable JSON array + index the comprehensive per-file presentation
        -- reads (transfers.repository._candidate_presentation:
        -- ``candidates[selected_candidate].id``). Pure function of already-
        -- stored columns -- no live call, no extra table. Feeds ONLY the
        -- movement check below; membership stays untouched by it.
        group_member_selected_candidate AS (
            SELECT
                transfer_id,
                artifact_id,
                json_extract(candidates, '$[' || selected_candidate || '].id') AS selected_candidate_id
            FROM group_member_artifacts
        ),
        -- Remaining-work membership count: current authoritative artifacts of
        -- the transfer (same identity as group_member_artifacts above) whose
        -- own artifact-lifecycle state is one a candidate switch could ever
        -- apply to (see _SWITCHABLE_SQL above — reused from the exact
        -- classification the comprehensive Details presentation already
        -- uses). Independent of common-source MEMBERSHIP/count — this only
        -- tells the list surfaces whether the group launcher should render
        -- as an interactive action or a static history indicator (Section 10
        -- of the DP 1.0.12 presentation pass); the PER-HOST actionability
        -- verdict (which of the common hosts this remaining work can
        -- actually converge to right now) is still resolved lazily from
        -- Details when the chooser opens, never here.
        group_remaining_counts AS (
            SELECT transfer_id, COUNT(*) AS remaining_count
            FROM group_member_artifacts
            WHERE {_SWITCHABLE_SQL}
            GROUP BY transfer_id
        ),
        -- Current authoritative artifact filenames for the transfer, in the
        -- same current-artifact scope as group_member_artifacts above (never
        -- a divergent definition). Feeds the pure display-name normalizer in
        -- Python — no filename parsing happens in SQL. Row order inside the
        -- aggregate follows the artifact id, matching the same ordering
        -- convention already used for artifact_presentation_facts below.
        group_member_filenames AS (
            SELECT transfer_id, json_group_array(filename) AS filenames
            FROM (
                SELECT transfer_id, filename
                FROM group_member_artifacts
                ORDER BY artifact_id
            )
            GROUP BY transfer_id
        ),
        group_member_hosts AS (
            SELECT DISTINCT
                a.transfer_id,
                a.artifact_id,
                b.provider_id AS provider_id,
                b.candidate_id AS candidate_id,
                rtrim(
                    CASE
                        WHEN lower(trim(b.source_key)) LIKE 'www.%'
                        THEN substr(lower(trim(b.source_key)), 5)
                        ELSE lower(trim(b.source_key))
                    END,
                    '.'
                ) AS host
            FROM group_member_artifacts a
            JOIN canonical_candidate_bindings b
              ON b.canonical_artifact_id = a.artifact_id
             AND b.candidate_order <= 100000
             AND lower(COALESCE(b.source_scope, '')) = 'host'
             AND length(trim(COALESCE(b.source_key, ''))) > 0
        ),
        group_true_common_hosts AS (
            SELECT gmh.transfer_id, gmh.host
            FROM group_member_hosts gmh
            JOIN group_member_counts gmc
              ON gmc.transfer_id = gmh.transfer_id
            WHERE length(gmh.host) BETWEEN 1 AND 253
            GROUP BY gmh.transfer_id, gmh.host
            HAVING COUNT(DISTINCT gmh.artifact_id) = MAX(gmc.artifact_total)
        ),
        group_common_sources AS (
            SELECT common.transfer_id, COUNT(*) AS common_candidate_count
            FROM group_true_common_hosts common
            GROUP BY common.transfer_id
        ),
        -- A common host requires MOVEMENT for the remaining-work set when at
        -- least one remaining-work (switchable-lifecycle, non-completed)
        -- artifact's binding for that host is NOT its currently-selected
        -- candidate. This is the piece a bare "host is common + enabled"
        -- check misses: the already-uniform ACTIVE source is trivially
        -- common and trivially enabled but requires ZERO artifact movement,
        -- so it cannot by itself make the transfer list-actionable (Section
        -- 8/10 of the DP 1.0.12 presentation task) — offering it as the only
        -- "interactive" choice would open a chooser that can never actually
        -- switch anything. is_selected is decoded the same way the
        -- comprehensive per-file presentation already does (candidate id at
        -- the durable ``selected_candidate`` index) — pure stored data, no
        -- live call, no extra table.
        group_remaining_host_movement AS (
            SELECT DISTINCT gmh.transfer_id, gmh.host, gmh.provider_id
            FROM group_member_hosts gmh
            JOIN group_member_artifacts gma
              ON gma.transfer_id = gmh.transfer_id
             AND gma.artifact_id = gmh.artifact_id
             AND {_GMA_SWITCHABLE_SQL}
            JOIN group_member_selected_candidate gmsc
              ON gmsc.transfer_id = gmh.transfer_id
             AND gmsc.artifact_id = gmh.artifact_id
            WHERE gmh.candidate_id IS NOT gmsc.selected_candidate_id
        ),
        -- Universal-convergence VETO, matching the shipped chooser's own
        -- participant scope exactly (ui-group-candidates.js computeGroup:
        -- actionParticipants excludes ONLY completed files — nothing else).
        -- A common host H is vetoed when some non-completed participant is
        -- BOTH (a) not currently selected on H, AND (b) not in a switchable
        -- lifecycle state — i.e. it can never move to H by a candidate
        -- switch, exactly like the chooser's own actionableHosts check
        -- (every actionParticipant must already be selected on H, or
        -- switch_eligible for H — switch_eligible is false whenever the
        -- artifact's own state disqualifies it, regardless of the target).
        -- Membership already guarantees every participant has a binding for
        -- every true common host, so a missing join row cannot silently
        -- hide a veto.
        group_host_vetoes AS (
            SELECT DISTINCT gmh.transfer_id, gmh.host
            FROM group_member_hosts gmh
            JOIN group_member_artifacts gma
              ON gma.transfer_id = gmh.transfer_id
             AND gma.artifact_id = gmh.artifact_id
             AND LOWER(TRIM(COALESCE(gma.status, ''))) != 'completed'
             AND NOT {_GMA_SWITCHABLE_SQL}
            JOIN group_member_selected_candidate gmsc
              ON gmsc.transfer_id = gmh.transfer_id
             AND gmsc.artifact_id = gmh.artifact_id
            WHERE gmh.candidate_id IS NOT gmsc.selected_candidate_id
        ),
        -- Does this transfer have at least one TRUE common host (the exact
        -- same set group_common_sources counts, unmodified) that (a)
        -- requires at least one artifact movement for the remaining-work set
        -- (excludes the already-uniform ACTIVE source), (b) is NOT vetoed by
        -- a participant that can never converge there (the shipped chooser's
        -- own universal-convergence rule, above), AND (c) is backed by a
        -- currently-ENABLED provider (a durable, page-global fact — see
        -- _disabled_provider_ids docstring; live provider health/candidate-
        -- expiry have no durable stored representation anywhere in this
        -- codebase and are only ever re-validated at actual switch time by
        -- manual_candidate_failover.py, even against the comprehensive
        -- Details read — the bounded list is a conservative AFFORDANCE
        -- projection, never more permissive than the chooser, and the write
        -- endpoint remains the final authority). Feeds ONLY
        -- group_remaining_count below; group_common_sources/
        -- common_candidate_count above is completely untouched by this —
        -- membership/history stays independent of actionability, unchanged.
        group_actionable_common_sources AS (
            SELECT DISTINCT common.transfer_id
            FROM group_true_common_hosts common
            JOIN group_remaining_host_movement movement
              ON movement.transfer_id = common.transfer_id
             AND movement.host = common.host
             {disabled_provider_clause}
            LEFT JOIN group_host_vetoes veto
              ON veto.transfer_id = common.transfer_id
             AND veto.host = common.host
            WHERE veto.transfer_id IS NULL
        ),
        -- ── Candidate-action scope (DP 1.0.12 Contextual Candidate Action
        -- Scope task, §3-4) ── a THIRD, orthogonal read fact alongside
        -- membership (group_common_sources/common_candidate_count) and
        -- remaining-work (group_remaining_counts/group_remaining_count):
        -- the smallest unambiguous candidate-switch OPERATION SCOPE the
        -- operator can act on right now (none|artifact|group). Never fed
        -- back into membership/remaining-work and never derived from a
        -- narrowed "movable subset" intersection.
        --
        -- Per-artifact canonical candidate IDENTITY count: protocol/provider-
        -- neutral (no source_scope filter, unlike the host-scoped
        -- group_member_hosts above), counting DISTINCT candidate ids so a
        -- duplicate binding row can never inflate it. Feeds ONLY the
        -- classifier below.
        artifact_candidate_counts AS (
            SELECT
                b.canonical_artifact_id AS artifact_id,
                COUNT(DISTINCT b.candidate_id) AS candidate_count
            FROM canonical_candidate_bindings b
            JOIN group_member_artifacts gma
              ON gma.artifact_id = b.canonical_artifact_id
            -- Current bindings only: the non-current band
            -- (CanonicalOwnership._NON_CURRENT_ORDER) is replaced history,
            -- never a choice.
            WHERE b.candidate_order <= 100000
            GROUP BY b.canonical_artifact_id
        ),
        -- MOVABLE artifacts: an operation-CLASSIFICATION SUBSET of
        -- group_member_artifacts (current authoritative, non-blocked, non-
        -- standby -- inherited, never redefined here). Additionally in a
        -- switchable lifecycle state (the same _GMA_SWITCHABLE_SQL
        -- classification group_remaining_counts already uses) and backed by
        -- at least two distinct canonical candidate identities -- the same
        -- bar generic Details acquisition_candidates.switch_eligible uses
        -- (transfers/repository.py). This NEVER feeds group membership,
        -- common_candidate_count, or which siblings exist -- it is consulted
        -- ONLY by the classifier below.
        movable_artifacts AS (
            SELECT
                gma.transfer_id AS transfer_id,
                gma.artifact_id AS artifact_id,
                acc.candidate_count AS candidate_count
            FROM group_member_artifacts gma
            JOIN artifact_candidate_counts acc
              ON acc.artifact_id = gma.artifact_id
            WHERE {_GMA_SWITCHABLE_SQL}
              AND acc.candidate_count >= 2
        ),
        movable_artifact_counts AS (
            SELECT transfer_id, COUNT(*) AS movable_count
            FROM movable_artifacts
            GROUP BY transfer_id
        ),
        -- Deterministic single-movable-artifact identity/count. Only ever
        -- meaningful in Python when movable_artifact_counts.movable_count = 1
        -- for that transfer (enforced in Python, not here): MIN/MAX over a
        -- one-row group is then exact, never a guess.
        single_movable_artifact AS (
            SELECT
                transfer_id,
                MIN(artifact_id) AS artifact_id,
                MAX(candidate_count) AS candidate_count
            FROM movable_artifacts
            GROUP BY transfer_id
        ),
        -- Strict common targets (group_true_common_hosts -- UNTOUCHED, the
        -- exact same set group_common_sources counts) that are additionally
        -- actionable AND movement-producing -- the identical predicate
        -- group_actionable_common_sources already uses (reused, not re-
        -- derived) -- kept at HOST granularity so they can be counted once
        -- per normalized host instead of collapsed to one existence row per
        -- transfer. group_actionable_common_sources itself is untouched;
        -- this is a purely additive COUNT extension for the classifier.
        group_actionable_common_source_hosts AS (
            SELECT DISTINCT common.transfer_id, common.host
            FROM group_true_common_hosts common
            JOIN group_remaining_host_movement movement
              ON movement.transfer_id = common.transfer_id
             AND movement.host = common.host
             {disabled_provider_clause}
            LEFT JOIN group_host_vetoes veto
              ON veto.transfer_id = common.transfer_id
             AND veto.host = common.host
            WHERE veto.transfer_id IS NULL
        ),
        group_actionable_common_source_counts AS (
            SELECT transfer_id, COUNT(*) AS actionable_count
            FROM group_actionable_common_source_hosts
            GROUP BY transfer_id
        )
        SELECT
            t.id,
            CASE WHEN t.hash LIKE 'deleted:%' OR t.hash LIKE 'retired:%' THEN COALESCE(t.source_fingerprint, '') ELSE t.hash END AS hash,
            t.name,
            t.status,
            t.size_bytes,
            t.progress,
            t.source,
            t.label,
            t.error_message,
            t.extraction_status,
            t.extraction_error,
            t.created_at,
            t.updated_at,
            t.completed_at,
            COALESCE(request_failures.failure_count, 0) AS source_failure_count,
            COALESCE(group_common_sources.common_candidate_count, 0) AS common_candidate_count,
            CASE
                WHEN group_actionable_common_sources.transfer_id IS NULL THEN 0
                ELSE COALESCE(group_remaining_counts.remaining_count, 0)
            END AS group_remaining_count,
            group_member_filenames.filenames AS _group_member_filenames,
            current_route.provider_id AS current_provider_id,
            CASE
                WHEN execution_lead.transfer_id IS NOT NULL THEN
                    CASE WHEN execution_lead.provider_count = 1 THEN execution_lead.provider_id END
                WHEN live_route_providers.provider_count = 1 THEN live_route_providers.provider_id
            END AS active_provider_id,
            CASE
                WHEN execution_lead.transfer_id IS NOT NULL THEN
                    CASE
                        WHEN execution_lead.provider_count = 1 AND execution_lead.provider_id IS NOT NULL
                        THEN 'execution'
                        ELSE 'ambiguous'
                    END
                WHEN live_route_providers.provider_count = 1 THEN 'route'
                ELSE 'none'
            END AS active_provider_basis,
            CASE
                WHEN COALESCE(delivery.provider_count, 0) = 1
                THEN delivery.provider_id
                ELSE NULL
            END AS delivering_provider_id,
            CASE
                WHEN COALESCE(origin.provider_count, 0) = 1 AND COALESCE(origin.unrouted_count, 0) = 0
                THEN origin.provider_id
                ELSE NULL
            END AS origin_provider_id,
            CASE
                WHEN COALESCE(delivery.provider_count, 0) > 0 THEN 'recorded'
                WHEN t.status = 'completed' THEN 'unknown_legacy'
                ELSE 'pending'
            END AS provider_provenance_status,
            current_root_resource.resource_state AS _current_root_resource_state,
            artifact_presentation_facts.artifacts AS _artifact_presentation_facts,
            input_challenge.challenge AS _input_challenge,
            COALESCE(pause_intent.paused, 0) AS _paused_intent,
            page_active_execution.execution_completed AS _execution_completed,
            page_active_execution.execution_total AS _execution_total,
            page_active_execution.execution_writers AS _execution_writers,
            page_active_execution.execution_acquiring AS _execution_acquiring,
            page_active_execution.execution_bytes_known AS _execution_bytes_known,
            page_active_execution.execution_units_known AS _execution_units_known,
            page_active_execution.execution_units_completed AS _execution_units_completed,
            page_active_execution.execution_units_total AS _execution_units_total,
            page_active_execution.execution_estimates_known AS _execution_estimates_known,
            page_active_execution.execution_estimated AS _execution_estimated,
            root_request.payload AS _source_request_payload,
            root_request_kinds.kinds AS _root_request_kinds,
            delivered_source.candidate_source AS _delivered_candidate_source,
            active_source.candidate_source AS _active_candidate_source,
            delivered_source.candidate_input AS _delivered_candidate_input,
            active_source.candidate_input AS _active_candidate_input,
            deferred_source.provider_id AS _deferred_provider_id,
            deferred_source.source AS _deferred_source,
            latest_route.candidate_summary AS _route_candidate_summary,
            COALESCE(movable_artifact_counts.movable_count, 0) AS _movable_artifact_count,
            single_movable_artifact.artifact_id AS _single_movable_artifact_id,
            single_movable_artifact.candidate_count AS _single_movable_artifact_candidate_count,
            COALESCE(group_actionable_common_source_counts.actionable_count, 0) AS _actionable_common_target_count,
            page_current_file_selection.manifest_id AS _file_selection_manifest_id,
            page_current_file_selection.decision AS _file_selection_decision,
            page_current_file_selection.request_kind AS _file_selection_request_kind,
            page_current_file_selection.manifest_committed_at AS _file_selection_committed_at,
            COALESCE(page_file_selection_manifest_counts.manifest_entry_count, 0) AS _file_selection_entry_count,
            COALESCE(page_file_selection_manifest_counts.independent_members, 0) AS _file_selection_explicit_only,
            CASE WHEN {_QUEUE_MEMBER} THEN 1 ELSE 0 END AS queue_member
        FROM page
        JOIN torrents t
          ON t.id = page.id
        LEFT JOIN current_root_resource
          ON current_root_resource.transfer_id = t.id
        LEFT JOIN artifact_presentation_facts
          ON artifact_presentation_facts.transfer_id = t.id
        LEFT JOIN input_challenge
          ON input_challenge.transfer_id = t.id
        LEFT JOIN transfer_pause_intents pause_intent
          ON pause_intent.torrent_id = t.id
        LEFT JOIN page_active_execution
          ON page_active_execution.transfer_id = t.id
        LEFT JOIN latest_route
          ON latest_route.transfer_id = t.id
        LEFT JOIN current_route
          ON current_route.transfer_id = t.id
        LEFT JOIN execution_lead
          ON execution_lead.transfer_id = t.id
        LEFT JOIN live_route_providers
          ON live_route_providers.transfer_id = t.id
        LEFT JOIN delivery
          ON delivery.transfer_id = t.id
        LEFT JOIN origin
          ON origin.transfer_id = t.id
        LEFT JOIN delivered_source
          ON delivered_source.transfer_id = t.id
        LEFT JOIN active_source
          ON active_source.transfer_id = t.id
        LEFT JOIN deferred_source
          ON deferred_source.transfer_id = t.id
        LEFT JOIN root_request
          ON root_request.transfer_id = t.id
        LEFT JOIN root_request_kinds
          ON root_request_kinds.transfer_id = t.id
        LEFT JOIN request_failures
          ON request_failures.transfer_id = t.id
        LEFT JOIN group_common_sources
          ON group_common_sources.transfer_id = t.id
        LEFT JOIN group_remaining_counts
          ON group_remaining_counts.transfer_id = t.id
        LEFT JOIN group_actionable_common_sources
          ON group_actionable_common_sources.transfer_id = t.id
        LEFT JOIN group_member_filenames
          ON group_member_filenames.transfer_id = t.id
        LEFT JOIN movable_artifact_counts
          ON movable_artifact_counts.transfer_id = t.id
        LEFT JOIN single_movable_artifact
          ON single_movable_artifact.transfer_id = t.id
        LEFT JOIN group_actionable_common_source_counts
          ON group_actionable_common_source_counts.transfer_id = t.id
        LEFT JOIN page_current_file_selection
          ON page_current_file_selection.transfer_id = t.id
        LEFT JOIN page_file_selection_manifest_counts
          ON page_file_selection_manifest_counts.manifest_id = page_current_file_selection.manifest_id
        ORDER BY page.page_ordinal
    """

    async with get_db() as db:
        await _prepare_search(db, plan, search_scope)
        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0
        # Every root's provider and whether it has a real alternative come
        # from the one committed-root-route owner and the one selectability
        # rule, read for the whole page in this session -- never per row:
        # a torrent root's always, any other root's where a registry can
        # judge its alternatives.
        routed = [int(row["id"]) for row in rows if routes_judged or (
            len(kinds := _bounded_request_kinds(row)) == 1 and kinds[0] in BITTORRENT_REQUEST_KINDS)]
        route_roots = await root_route_facts_by_root_in(repository, db, routed) if routed else {}
    return {"rows": rows, "total": total, "route_roots": route_roots, "routed": routed}


# Public names for the Downloads route (an edge reaches no underscore name of core).
ACTIVITY_ORDER = _ACTIVITY_ORDER
bounded_request_kinds = _bounded_request_kinds
