"""What the core lends an integration -- never the core itself.

An integration is built with an ``IntegrationEnvironment`` (``integrations
.definition``). It must not hold the transfer repository or the application
service: either would let it read any other integration's executions (with
their candidates' provider-issued addresses and headers) or command anything.
It gets exactly these instead:

* ``ExecutionGrants`` -- core's own decisions about executions: whether one
  attempt (or a batch) may still act (``authorize``), the attempts of ONE
  executor identity without their candidates (``attempts_of``), and whether
  that executor still has live work (``has_live_work``). Read-only.
* ``IntegrationCommands`` -- the operator commands an integration-owned
  administration surface may forward (pause, resume, cancel one artifact) and
  the one status notification. Nothing else of the application.
"""
from __future__ import annotations

from dataclasses import is_dataclass, replace


class ExecutionGrants:
    """Core's execution decisions, lent read-only (module docstring)."""

    __slots__ = ("_repository",)

    def __init__(self, repository):
        self._repository = repository

    def __repr__(self) -> str:
        return "<ExecutionGrants>"

    async def authorize(self, handle, action: str) -> bool:
        """Whether core still authorizes ``handle`` for ``action`` (the one decision)."""
        return await self._repository.authorize_execution(handle, action)

    async def authorize_many(self, handles, action: str) -> tuple[bool, ...]:
        return await self._repository.authorize_executions(list(handles), action)

    async def attempts_of(self, executor_id: str, *, live: bool = False) -> tuple:
        """The execution attempts of ONE executor identity -- ``live`` only those of
        unfinished transfers -- each WITHOUT its candidate: an integration needs the
        handle and the transfer/artifact it serves, never another integration's
        addresses or headers. Its own handles' opaque correlation is the executor's
        to read; core never parses it."""
        attempts = await (self._repository.live_executions() if live else self._repository.executions())
        return tuple(replace(attempt, candidate=None) if is_dataclass(attempt) else attempt for attempt in attempts
                     if attempt.handle.executor_id == executor_id)

    async def has_live_work(self, executor_id: str) -> bool:
        """Whether core still holds live work for this executor identity."""
        return str(executor_id) in await self._repository.executors_with_live_work()


class IntegrationCommands:
    """The operator commands an integration may forward (module docstring)."""

    __slots__ = ("_application",)

    def __init__(self, application):
        self._application = application

    def __repr__(self) -> str:
        return "<IntegrationCommands>"

    async def pause(self, transfer_id: int):
        return await self._application.pause(transfer_id)

    async def resume(self, transfer_id: int):
        return await self._application.resume(transfer_id)

    async def cancel_artifact(self, transfer_id: int, artifact_id: int):
        return await self._application.cancel_artifact(transfer_id, artifact_id)

    async def notify_status_changed(self) -> None:
        await self._application.notify_status_changed()
