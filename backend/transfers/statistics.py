"""The transfer core's statistics read model: counts, totals and per-period
breakdowns of its own transfers and artifacts, for the Dashboard, Statistics,
diagnostics and metrics surfaces. The surfaces shape these into responses; they
never query core's tables themselves (``tests/test_v102_core_boundaries.py``;
operator, 2026-11-22).

Which providers make up a source family or an acquisition method is the caller's
to say (integration metadata, read at the edge): core counts deliveries by the
provider ids it is given, and names no integration.
"""
from __future__ import annotations

from db.database import get_db
from transfers._repository_base import canonical_artifact_membership_sql

# Transfer states at which a retained transfer has a settled disposition.
SETTLED_RAW_STATUSES = ("completed", "error", "failed", "lost", "cancelled", "deleted", "consolidated")


# ── SQL dialect helpers ────────────────────────────────────────────────────────
def sql_now_minus(interval: str) -> str:
    parts = interval.split()
    n, unit = parts[0], parts[1]
    return f"datetime('now','-{n} {unit}')"


def sql_strftime(fmt: str, field: str) -> str:
    # SQLite stores canonical UTC clock values; calendar buckets are operator-local.
    return f"strftime('{fmt}', {field}, 'localtime')"


def sql_date(field: str) -> str:
    return f"DATE({field}, 'localtime')"


# ── reads ──────────────────────────────────────────────────────────────────────

async def status_breakdown(sample: int = 20) -> dict:
    """Every transfer status with its count, and a sample of transfers not yet
    completed or deleted (newest first) with their unblocked file counts."""
    async with get_db() as db:
        counts = await db.fetchall("SELECT status, COUNT(*) AS cnt FROM torrents GROUP BY status ORDER BY cnt DESC")
        open_sample = await db.fetchall(
            """SELECT t.id, t.name, t.status,
                      (SELECT COUNT(*) FROM download_files f WHERE f.torrent_id=t.id AND f.blocked=0) AS file_count
               FROM torrents t
               WHERE t.status NOT IN ('completed', 'deleted')
               ORDER BY t.id DESC LIMIT ?""", (int(sample),))
    return {"status_counts": [dict(row) for row in counts], "sample_non_terminal": [dict(row) for row in open_sample]}


async def overview() -> tuple[dict, dict]:
    """``(by_status, aggregate)``: the transfer count per status, and the Dashboard's
    aggregate facts (completed bytes, active/queued/extracting/error/completed
    counts, recent completions, averages, blocked files)."""
    last_24h_expr = sql_now_minus("1 day")
    last_7d_expr = sql_now_minus("7 days")
    async with get_db() as db:
        rows = await db.fetchall("SELECT status, COUNT(*) as count FROM torrents GROUP BY status")
        aggregate = await db.fetchone(
            f"""SELECT
                   COALESCE(SUM(CASE WHEN status='completed' THEN size_bytes ELSE 0 END), 0)
                       AS total_completed_bytes,
                   SUM(CASE WHEN status IN ('downloading','processing','uploading','paused')
                            THEN 1 ELSE 0 END) AS active_downloads,
                   SUM(CASE WHEN COALESCE(extraction_status,'')='extracting'
                            THEN 1 ELSE 0 END) AS extracting_count,
                   SUM(CASE WHEN status IN ('ready','queued') THEN 1 ELSE 0 END)
                       AS queued_downloads,
                   SUM(CASE WHEN status='downloading' THEN 1 ELSE 0 END)
                       AS operator_active_downloads,
                   AVG(CASE WHEN status='downloading' THEN COALESCE(progress, 0)
                            ELSE NULL END) AS operator_active_progress_pct,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                   SUM(CASE WHEN status='completed' AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END) AS completed_count,
                   SUM(CASE WHEN completed_at >= {last_24h_expr}
                                AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END)
                       AS completed_last_24h,
                   SUM(CASE WHEN completed_at >= {last_7d_expr}
                                AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END)
                       AS completed_last_7d,
                   AVG(CASE
                       WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                       THEN CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)
                       ELSE NULL END) AS avg_download_duration_seconds,
                   AVG(CASE WHEN status='completed' AND size_bytes>0 THEN size_bytes
                            ELSE NULL END) AS avg_torrent_size_bytes,
                   (SELECT COUNT(*) FROM download_files WHERE blocked=1)
                       AS total_blocked_files
               FROM torrents"""
        ) or {}
    return {row["status"]: row["count"] for row in rows}, dict(aggregate)


async def completion_buckets(bucket_sql: str, since_sql: str, family_members: dict[str, list[str]]) -> list[dict]:
    """Completed transfers per bucket since ``since_sql``: ``count`` as it has
    always been counted and how many of them each source family delivered.

    A completion belongs to a family only when every one of its current
    canonical artifacts was delivered -- by its latest delivered execution
    (``execution_attempt_provenance.delivered``, written by the one completion
    funnel; earlier failed or replaced attempts never count) -- through a
    provider of that same family (``family_members``: family -> provider ids). A
    completion whose material came through both families, or any part of which
    has no recorded delivering provider (history from before provenance, an
    adopted payload), is unattributed and stays in ``count``:
    ``count == premium + network + unattributed``."""
    family_sql = " ".join(
        f"WHEN p.provider_id IN ({', '.join('?' for _ in ids) or 'NULL'}) THEN '{family}'"
        for family, ids in family_members.items())
    async with get_db() as db:
        rows = await db.fetchall(f"""
            WITH done AS (
                SELECT id, {bucket_sql} AS date FROM torrents WHERE completed_at >= {since_sql} AND status='completed'
            ),
            artifact AS (
                SELECT f.torrent_id AS transfer_id,
                       (SELECT CASE {family_sql} END FROM execution_attempt_provenance p
                        WHERE p.artifact_id = f.id AND p.delivered = 1 ORDER BY p.ordinal DESC LIMIT 1) AS family
                FROM download_files f JOIN done ON done.id = f.torrent_id
                WHERE {canonical_artifact_membership_sql("f")}
            ),
            attributed AS (
                SELECT transfer_id,
                       CASE WHEN COUNT(*) = COUNT(family) AND MIN(family) = MAX(family) THEN MIN(family) END AS family
                FROM artifact GROUP BY transfer_id
            )
            SELECT done.date AS date, COUNT(*) AS count,
                   COUNT(CASE WHEN attributed.family = 'premium' THEN 1 END) AS premium,
                   COUNT(CASE WHEN attributed.family = 'network' THEN 1 END) AS network
            FROM done LEFT JOIN attributed ON attributed.transfer_id = done.id
            GROUP BY done.date ORDER BY done.date ASC""",
            [provider for ids in family_members.values() for provider in ids])
    return [{**row, "unattributed": int(row["count"]) - int(row["premium"]) - int(row["network"])} for row in rows]


async def transfer_methods(where_sql: str, methods: dict[str, list[str]]) -> list[dict]:
    """How the transfers added in the period were acquired, ranked, at most ten rows.

    A transfer counts ONCE under each acquisition method (``methods``: method ->
    provider ids) that delivered any of its current canonical artifacts -- by that
    artifact's latest delivered execution (the same delivery truth the completion
    families read; failed, replaced or standby attempts never count). A transfer
    that completed with no delivery attributable to any method -- history from
    before provenance, an adopted payload -- is "Unattributed", never guessed."""
    method_sql = " ".join(
        f"WHEN p.provider_id IN ({', '.join('?' for _ in ids)}) THEN ?" for ids in methods.values()) or "WHEN 0 THEN NULL"
    parameters = [value for method, ids in methods.items() for value in (*ids, method)]
    async with get_db() as db:
        return await db.fetchall(f"""
            WITH scope AS (
                SELECT id, completed_at FROM torrents {where_sql}
            ),
            delivered AS (
                SELECT DISTINCT f.torrent_id AS transfer_id,
                       (SELECT CASE {method_sql} END FROM execution_attempt_provenance p
                        WHERE p.artifact_id = f.id AND p.delivered = 1 ORDER BY p.ordinal DESC LIMIT 1) AS method
                FROM download_files f JOIN scope ON scope.id = f.torrent_id
                WHERE {canonical_artifact_membership_sql("f")}
            ),
            attributed AS (SELECT transfer_id, method FROM delivered WHERE method IS NOT NULL)
            SELECT method, COUNT(*) AS count FROM attributed GROUP BY method
            UNION ALL
            SELECT 'Unattributed', COUNT(*) FROM scope
            WHERE completed_at IS NOT NULL AND id NOT IN (SELECT transfer_id FROM attributed)
            HAVING COUNT(*) > 0
            ORDER BY count DESC, method ASC LIMIT 10""", parameters)


async def period_totals(cutoff: str | None) -> dict:
    """The Statistics totals and breakdowns for a period (``cutoff``: an SQL time
    expression, or ``None`` for all time): totals, the settled transfer
    dispositions and the file status breakdown."""
    where_ts = f"WHERE created_at >= {cutoff}" if cutoff else ""
    where_comp = (
        f"WHERE status='completed' AND COALESCE(extraction_status,'')!='extracting' AND completed_at >= {cutoff}"
        if cutoff
        else "WHERE status='completed' AND COALESCE(extraction_status,'')!='extracting'"
    )
    async with get_db() as db:
        totals = dict(await db.fetchone(
            f"SELECT COUNT(*) as torrent_total, COALESCE(SUM(size_bytes),0) as torrent_size_total "
            f"FROM torrents {where_ts}"
        ) or {})
        completed_count = (await db.fetchone(
            f"SELECT COUNT(*) as c FROM torrents {where_comp}") or {}).get("c", 0)
        error_count = (await db.fetchone(
            "SELECT COUNT(*) as c FROM torrents WHERE status='error'"
            + (f" AND created_at >= {cutoff}" if cutoff else "")) or {}).get("c", 0)
        terminal = completed_count + error_count
        totals["success_rate_pct"] = round(completed_count / terminal * 100, 1) if terminal > 0 else None
        completed_size_row = await db.fetchone(
            f"SELECT COALESCE(SUM(size_bytes),0) as v FROM torrents {where_comp}")
        totals["completed_size"] = completed_size_row["v"] if completed_size_row else 0
        totals["completed_count"] = completed_count
        partial_row = await db.fetchone(
            "SELECT COUNT(*) as c FROM torrents "
            "WHERE (status IN ('processing','downloading','dispatched','partial') OR COALESCE(extraction_status,'')='extracting')"
            + (f" AND created_at >= {cutoff}" if cutoff else ""))
        totals["partial_total"] = partial_row["c"] if partial_row else 0
        # Transfer Disposition: each retained transfer at its current SETTLED
        # status, at most once; anything still active, pending or extracting
        # (the in-progress KPI's own rule) has no disposition yet. Remove
        # persists `deleted`, so there is no separate Removed to count.
        torrent_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count FROM torrents "
            f"WHERE status IN ({','.join('?' * len(SETTLED_RAW_STATUSES))}) "
            f"AND COALESCE(extraction_status,'')!='extracting'"
            + (f" AND created_at >= {cutoff}" if cutoff else "")
            + " GROUP BY status ORDER BY count DESC", SETTLED_RAW_STATUSES)
        where_files = (f"WHERE updated_at >= {cutoff}" if cutoff else "")
        file_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count, COALESCE(SUM(size_bytes),0) as size_bytes "
            f"FROM download_files {where_files} GROUP BY status ORDER BY count DESC")
    return {"totals": totals, "torrent_status": torrent_status, "file_status": file_status, "where_ts": where_ts}


async def completed_local_paths() -> list[str]:
    """The local path of every completed artifact that has one."""
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT local_path FROM download_files WHERE status='completed' AND local_path IS NOT NULL")
    return [row["local_path"] for row in rows if row["local_path"]]


async def metric_counts() -> dict:
    """``transfers_by_status``, ``files_by_status`` and ``completed_bytes``."""
    async with get_db() as db:
        rows = await db.fetchall("SELECT status, COUNT(*) AS c FROM torrents GROUP BY status")
        frows = await db.fetchall("SELECT status, COUNT(*) AS c FROM download_files GROUP BY status")
        size_row = await db.fetchone(
            "SELECT COALESCE(SUM(size_bytes),0) AS total FROM torrents WHERE status='completed'")
    return {"transfers_by_status": {row["status"]: int(row["c"]) for row in rows},
            "files_by_status": {row["status"]: int(row["c"]) for row in frows},
            "completed_bytes": int((size_row["total"] if size_row else 0) or 0)}
