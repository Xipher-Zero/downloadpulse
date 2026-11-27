"""The transfer core's PUBLIC read-model vocabulary for edges.

An edge (``api/``, ``external/``) that projects core state -- the bounded
Downloads read model, an external client's job phase -- reads it through these
names, never through ``transfers._repository_base`` or an underscore name:
the definitions stay the core's own, in one place, and an edge cannot reach
anything else of the repository's internals.
"""
from __future__ import annotations

from transfers._repository_base import (  # noqa: F401 -- re-exported
    _ENDED_ROUTE_STATES as ENDED_ROUTE_STATES,
    _MUTATING_EXECUTION_STATES as MUTATING_EXECUTION_STATES,
    _QUEUE_MEMBER_SQL as QUEUE_MEMBER_SQL,
    DemandChanged,
    active_execution_bytes,
    active_execution_progress_sql,
    active_execution_projection,
    canonical_artifact_membership_sql,
    effective_progress,
)
from transfers.presentation_repository import (  # noqa: F401 -- re-exported
    _CAPACITY_WAIT_PRESENTATION as CAPACITY_WAIT_PRESENTATION,
    _RAW_PRESENTATION as RAW_PRESENTATION,
    _TERMINAL_PRESENTATION as TERMINAL_PRESENTATION,
    _WAIT_PRESENTATION as WAIT_PRESENTATION,
    WorkStanding,
)


async def root_route_facts_by_root_in(repository, db, root_ids) -> dict:
    """Each root request's route facts, read on the caller's ``db`` (the bounded read
    model's own connection) -- the repository's one derivation."""
    return await repository._root_route_facts_by_root(db, root_ids)
