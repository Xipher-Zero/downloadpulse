"""One scenario of core's request context (``transfers.request_context``)
against the canonical engine and repository, run as its own process by
``test_v102_request_context_holder.py``.

The process is the isolation: ``DB_PATH`` names this scenario's database, and
the holder is the module's own singleton -- nothing is patched. Accounts and
browser tokens are the real ones (``auth.accounts``). The provider and the
executor are the ledger fakes, recording only what core hands them on the
requests it passes (``TransferRequest.context``, ``ExecutionRequest.context``):
neither can reach the holder.

Usage: python request_context_world.py <scenario> <work-dir>   (prints one JSON object)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

scenario, work = sys.argv[1], Path(sys.argv[2])
os.environ["DB_PATH"] = str(work / "state.db")
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import db.database as database  # noqa: E402
from auth import accounts  # noqa: E402
from executor_fakes import LedgerExecutor, LedgerProvider  # noqa: E402
from transfers.convergence_engine import TransferEngine  # noqa: E402
from transfers.models import SubmissionDemand, TransferRequest  # noqa: E402
from transfers.policy import TransferPolicy  # noqa: E402
from transfers.recovery_repository import TransferRepository  # noqa: E402
from transfers.registry import IntegrationRegistry  # noqa: E402
from transfers.request_context import CAPACITY, ContextCapacity, RequestContext, request_contexts  # noqa: E402

VALUE = "held-value-for-one-job"
CONTEXT = RequestContext.from_submission([{"name": "SID", "value": VALUE, "domain": ".video.test"}],
                                         "https://www.video.test/list", ["https://www.video.test/v/1"])


def carried(context) -> bool:
    return bool(context) and context.cookies[0].value == VALUE and context.referrer == "https://www.video.test/list"


class ContextProvider(LedgerProvider):
    seen: list = []

    async def resolve(self, request):
        self.seen.append(carried(request.context))
        return await super().resolve(request)


class ContextExecutor(LedgerExecutor):
    seen: list = []

    async def start(self, request, handle):
        self.seen.append(carried(request.context))
        return await super().start(request, handle)


def grant(token_id: str):
    """The registrant's grant, as the browser intake gives it: its token is not revoked."""
    async def valid() -> bool:
        async with database.get_db() as db:
            row = await db.fetchone("SELECT revoked_at FROM account_tokens WHERE id=?", (token_id,))
        return row is not None and row["revoked_at"] is None
    return valid


async def never() -> bool:
    return False


async def person(name):
    account = await accounts.create_local_account(name, "correct horse battery staple", role="user")
    token, _raw = await accounts.issue_browser_token(account.id, f"{name}'s browser")
    return account.id, token["id"]


async def main() -> None:
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = ContextProvider()
    registry.register_provider(provider)
    executor = ContextExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(work / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0))
    await engine.initialize()
    report: dict = {"unknown transfer": await request_contexts.for_transfer(999)}

    if scenario == "capacity":
        for index in range(CAPACITY):
            request_contexts.register(f"d-{index}", CONTEXT, owner="a", valid=never)
        try:
            request_contexts.register("one-more", CONTEXT, owner="a", valid=never)
            report["over capacity"] = "accepted"
        except ContextCapacity:
            report["over capacity"] = "refused"
        report["settled"] = await request_contexts.settle()   # no registrant still stands behind one
        report["held after settle"] = len(request_contexts)
        print(json.dumps(report, default=str))
        return

    una, una_token = await person("una")
    demand = SubmissionDemand(una, una_token)
    if scenario != "unregistered":
        request_contexts.register(demand.id, CONTEXT, owner=una, valid=grant(una_token))
    request = TransferRequest("ledger", "clip", name="clip")
    transfer = await engine.submit((request,), demand=demand)
    if scenario == "merged":
        bo, bo_token = await person("bo")
        merged = await engine.submit((request,), demand=SubmissionDemand(bo, bo_token))
        report["merged into the same transfer"] = merged.id == transfer.id
    if scenario == "revoked":
        await accounts.revoke_token(una, una_token, kind="browser")
    await engine.tick()
    report["resolution was handed it"] = provider.seen
    report["execution was handed it"] = executor.seen
    stored = (await repository.requests(transfer.id))[0].request
    report["the stored request carries none"] = stored.context is None
    report["settled while running"] = await request_contexts.settle()
    if scenario == "one":
        await engine.withdraw_demand([demand.id], by=una)
        report["settled after withdrawal"] = await request_contexts.settle()
        report["held"] = request_contexts.held(demand.id)
        report["after withdrawal"] = await request_contexts.for_transfer(transfer.id)
    print(json.dumps(report, default=str))


asyncio.run(main())
