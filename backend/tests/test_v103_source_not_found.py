"""1.0.3 (operator, 2026-11-22): a source that answers "not found" is not asked
again. A direct link whose server says 404 (or 410) used to wait for one timed
retry of the same address before reaching Requires attention -- even with
another source available. Now the route is done at once: an alternate is tried
now, with none the file needs the operator. A refreshable route (a
provider-issued link that may only have expired) still refreshes first.
"""
from __future__ import annotations

import pytest

from executors.aria2.translation import native_failure
from transfers.errors import Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy

POLICY = TransferPolicy(retry_delay=60, same_candidate_no_progress_limit=2)
NOT_FOUND = native_failure("3", "Resource not found")


def test_an_http_404_and_410_are_the_same_not_found_fact_never_marked_permanent():
    gone = native_failure("22", "Unexpected status. status=410")
    for error in (NOT_FOUND, gone):
        assert (error.category, error.stage, error.retryability, error.permanence) == (
            Category.SOURCE_NOT_FOUND, Stage.EXECUTION, Retryability.AFTER_RERESOLUTION, Permanence.UNKNOWN)
    # Other statuses keep their own facts.
    assert native_failure("22", "status=503").category == Category.SOURCE_TEMPORARILY_UNAVAILABLE
    assert native_failure("22", "status=429").category == Category.RATE_LIMITED


def test_a_direct_link_not_found_needs_the_operator_at_once_without_a_timed_retry():
    decision = POLICY.recover(NOT_FOUND, RecoveryContext(consecutive_no_progress_failures=1,
                                                         same_signature_failures=1), 100.0)
    assert decision.action == RecoveryAction.WAIT_FOR_OPERATOR
    assert (decision.reason, decision.quiescence_reason, decision.wake_condition) == (
        "source_not_found", "recovery_exhausted", "operator_retry")
    assert decision.retry_at is None


def test_with_another_source_it_is_tried_now_not_after_retrying_the_dead_one():
    decision = POLICY.recover(NOT_FOUND, RecoveryContext(consecutive_no_progress_failures=1, has_alternate=True), 100.0)
    assert (decision.action, decision.reason, decision.retry_at) == (
        RecoveryAction.TRY_ALTERNATE_CANDIDATE, "source_not_found_alternate", 100.0)


def test_after_progress_too_the_route_is_done():
    decision = POLICY.recover(NOT_FOUND, RecoveryContext(failures_since_meaningful_progress=1,
                                                         observed_completed_bytes=4096), 100.0)
    assert decision.action == RecoveryAction.WAIT_FOR_OPERATOR and decision.reason == "source_not_found"


def test_a_refreshable_link_still_refreshes_first_then_the_route_is_done():
    # Nothing was transferred (aria2 reported zero bytes): the expired-link case.
    fresh = POLICY.recover(NOT_FOUND, RecoveryContext(consecutive_no_progress_failures=1, can_refresh=True,
                                                      observed_completed_bytes=0), 100.0)
    assert fresh.action == RecoveryAction.REFRESH_CANDIDATE
    # Its refresh spent and the fresh link not found either: an alternate, else the operator.
    spent = RecoveryContext(consecutive_no_progress_failures=1, can_refresh=True, candidate_refreshes=1)
    assert POLICY.recover(NOT_FOUND, spent, 100.0).reason == "source_not_found"
    from dataclasses import replace
    assert POLICY.recover(NOT_FOUND, replace(spent, has_alternate=True), 100.0).action == \
        RecoveryAction.TRY_ALTERNATE_CANDIDATE


@pytest.mark.parametrize("context,action", [
    (RecoveryContext(provider_ready=False), RecoveryAction.WAIT_FOR_PROVIDER),
    (RecoveryContext(executor_ready=False), RecoveryAction.WAIT_FOR_RESOURCE),
    (RecoveryContext(input_required=True), RecoveryAction.WAIT_FOR_OPERATOR),
])
def test_an_unavailable_provider_executor_or_pending_input_still_decides_first(context, action):
    decision = POLICY.recover(NOT_FOUND, context, 100.0)
    assert decision.action == action and decision.reason != "source_not_found"


def test_a_transient_failure_keeps_its_timed_retry():
    busy = NormalizedError(Domain.NETWORK, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.EXECUTION,
                           retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE)
    decision = POLICY.recover(busy, RecoveryContext(consecutive_no_progress_failures=1), 100.0)
    assert decision.action == RecoveryAction.BACKOFF and decision.retry_at > 100.0
