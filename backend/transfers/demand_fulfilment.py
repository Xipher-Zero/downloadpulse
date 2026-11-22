"""What fulfils a demand -- the transfer core's read model for an edge that follows a
submission it accepted (the External API's jobs, a browser's downloads).

Core owns what its state MEANS: whether a demand is registered or withdrawn (and by
whom), whether its transfer's work is still open, which artifacts fulfil it (its own
canonical members and those it was consolidated onto), and the facts a delivery is
proven from. An edge composes these -- a predicate into its own statement, on its own
transaction, or a read -- and never derives them from core's tables itself
(``tests/test_v102_core_boundaries.py``; operator, 2026-11-22).

Every predicate takes the SQL expression that names the demand (or transfer) in the
caller's statement and returns a self-contained condition over core's tables.
"""
from __future__ import annotations

import json

from db.database import get_db
from transfers._repository_base import canonical_artifact_membership_sql

# The transfer states in which a transfer's work is settled: nothing further will be
# acquired for it unless an operator reopens it.
SETTLED_TRANSFER_STATES = ("cancelled", "completed", "consolidated", "deleted")


# ── predicates an edge composes into its own statement ─────────────────────────

def demand_registered_sql(demand: str) -> str:
    """The demand named by ``demand`` has been registered with core (admitted)."""
    return f"EXISTS (SELECT 1 FROM transfer_demands core_d WHERE core_d.id={demand})"


def demand_withdrawn_sql(demand: str) -> str:
    """The demand named by ``demand`` is registered and withdrawn."""
    return (f"EXISTS (SELECT 1 FROM transfer_demands core_d WHERE core_d.id={demand} "
            f"AND core_d.withdrawn_at IS NOT NULL)")


def demand_withdrawn_by_sql(demand: str, *, of_transfer: bool = False) -> str:
    """The demand named by ``demand`` was withdrawn by the withdrawer bound to the
    statement's next parameter -- and, with ``of_transfer``, the demand serves the
    transfer bound to the parameter BEFORE it."""
    scope = "AND core_d.transfer_id=? " if of_transfer else ""
    return (f"EXISTS (SELECT 1 FROM transfer_demands core_d WHERE core_d.id={demand} "
            f"{scope}AND core_d.withdrawn_by=?)")


def demand_work_open_sql(demand: str) -> str:
    """The transfer the demand named by ``demand`` serves still has open work (not
    settled: ``SETTLED_TRANSFER_STATES``). Binds no parameter."""
    settled = ",".join(f"'{state}'" for state in SETTLED_TRANSFER_STATES)
    return (f"EXISTS (SELECT 1 FROM transfer_demands core_d JOIN torrents core_t ON core_t.id=core_d.transfer_id "
            f"WHERE core_d.id={demand} AND core_t.status NOT IN ({settled}))")


# ── reads ──────────────────────────────────────────────────────────────────────

async def transfer_facts(transfer_ids) -> dict[int, dict]:
    """Each transfer's public lifecycle facts: ``id``, ``status``, ``progress``,
    ``extraction_status``, ``normalized_error`` -- keyed by id; a transfer that does
    not exist is absent."""
    ids = sorted({int(item) for item in transfer_ids})
    if not ids:
        return {}
    async with get_db() as db:
        rows = await db.fetchall(
            f"SELECT id, status, progress, extraction_status, normalized_error FROM torrents "
            f"WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    return {int(row["id"]): dict(row) for row in rows}


async def transfer_fact(transfer_id: int) -> dict | None:
    return (await transfer_facts((transfer_id,))).get(int(transfer_id))


async def fulfilment(transfer_id: int, status: str) -> dict:
    """The artifacts that fulfil a transfer's demand: its own canonical members (none
    when it was consolidated away; only completed ones once it completed) and every
    canonical artifact of another transfer one of its requests was consolidated onto,
    or is still associated with, unverified. ``rows`` (all), ``total``, ``completed``
    and ``foreign`` (with each owner's ``owner_status`` and whether it was a
    consolidation)."""
    async with get_db() as db:
        own = [] if status == "consolidated" else await db.fetchall(
            f"SELECT f.id, f.torrent_id, f.status FROM download_files f WHERE f.torrent_id=? "
            f"AND {canonical_artifact_membership_sql('f')}", (int(transfer_id),))
        foreign = await db.fetchall(
            """SELECT f.id, f.torrent_id, f.status, f.withdrawn_attempt, f.execution_attempt_id,
                   t.status AS owner_status, MAX(m.consolidated) AS consolidated FROM (
                   SELECT canonical_artifact_id AS id, 1 AS consolidated FROM artifact_consolidations
                       WHERE source_transfer_id=:t
                   UNION SELECT r.equivalence_target_artifact_id, 0 FROM transfer_requests r
                       WHERE r.transfer_id=:t AND r.equivalence_disposition='unverified'
                       AND r.equivalence_target_artifact_id IS NOT NULL) m
               JOIN download_files f ON f.id=m.id JOIN torrents t ON t.id=f.torrent_id
               WHERE f.torrent_id<>:t GROUP BY f.id""", {"t": int(transfer_id)})
    if status == "completed":
        own = [row for row in own if row["status"] == "completed"]
    rows = [*own, *foreign]
    return {"rows": rows, "total": len(rows), "completed": sum(row["status"] == "completed" for row in rows),
            "foreign": foreign}


async def material_generations(artifact_ids) -> dict[int, int]:
    """Each artifact's committed material generation (absent: none recorded)."""
    ids = sorted({int(item) for item in artifact_ids})
    if not ids:
        return {}
    async with get_db() as db:
        rows = await db.fetchall(
            f"SELECT artifact_id, material_generation FROM artifact_material_state "
            f"WHERE artifact_id IN ({','.join('?' * len(ids))})", tuple(ids))
    return {int(row["artifact_id"]): int(row["material_generation"]) for row in rows}


async def finalized_outputs(transfer_id: int) -> list[str] | None:
    """The final file set a finished, successful post-processing recorded for the
    transfer, or ``None`` when a successful post-processor recorded none (an outcome
    written before outputs were recorded) or none succeeded."""
    async with get_db() as db:
        rows = await db.fetchall("SELECT outcome FROM postprocess_attempts WHERE transfer_id=? "
                                 "AND state='finished'", (int(transfer_id),))
    outputs: set[str] = set()
    recorded = False
    for row in rows:
        outcome = json.loads(row["outcome"]) if row["outcome"] else {}
        if outcome.get("kind") != "success":
            continue
        if not isinstance(outcome.get("outputs"), list):
            return None
        recorded = True
        outputs.update(str(item) for item in outcome["outputs"])
    return sorted(outputs) if recorded else None


async def completed_canonical_artifact_ids(transfer_id: int) -> set[int]:
    """The transfer's own completed canonical artifacts."""
    async with get_db() as db:
        rows = await db.fetchall(
            f"SELECT f.id FROM download_files f WHERE f.torrent_id=? AND f.status='completed' "
            f"AND {canonical_artifact_membership_sql('f')}", (int(transfer_id),))
    return {int(row["id"]) for row in rows}
