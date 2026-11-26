"""SABnzbd runs only while it is needed (1.0.5 WS9).

The administration's run rule, each against its own facts: the enable check,
the idle grace, queued work waiting, work core owns or observes (whatever the
enable toggle), a server Test, a configuration change while idle; a failed
start backed off and said; the native-submission fence a stop takes; Idle only
when it is true; the executor ready only once its service is started and
converged; a deliberate stop that never reads as a crash; and core's runtime
coordinator, which defers a lift while an executor's runtime is stopped and
wakes whoever brings it up, at a bounded rate. The runtime and the work facts
are recorded fakes; the clock is injected.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from executors.sabnzbd import admin as admin_module
from executors.sabnzbd.admin import IDLE_GRACE_SECONDS, START_BACKOFF_SECONDS, SabnzbdAdministration
from executors.sabnzbd.client import ServiceNotRunning
from integrations.definition import ConfigurationApplication


class Runtime:
    def __init__(self, *, starts_ok=True, installed=True):
        self.running, self.starts_ok, self._installed = False, starts_ok, installed
        self.calls = []
        self.demand = None                       # the run state's slot, as the real runtime has

    def installed(self):
        return self._installed

    async def start(self):
        self.calls.append("start")
        self.running = self.starts_ok
        return await self.status()

    async def restart(self):
        self.calls.append("restart")
        self.running = self.starts_ok
        return await self.status()

    async def stop(self):
        self.calls.append("stop")
        self.running = False
        return await self.status()

    async def status(self):
        return {"running": self.running, "last_error": "" if self.starts_ok else "it would not start"}

    async def healthy(self):
        return self.running


class Work:
    """Core's facts about this executor, as ExecutionGrants lends them."""

    def __init__(self):
        self.live = self.observed = self.waiting = False
        self.order = []

    async def has_live_work(self, _identity):
        self.order.append("live")
        return self.live

    async def has_observed_work(self, _identity):
        self.order.append("observed")
        return self.observed

    async def has_waiting_work(self, _identity):
        self.order.append("waiting")
        return self.waiting


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build(*, enabled=True, applies=True, **runtime):
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    descriptor = SimpleNamespace(id="sabnzbd", enabled=enabled)
    work, clock, rt = Work(), Clock(), Runtime(**runtime)
    admin = SabnzbdAdministration(SimpleNamespace(), UsenetOptions(servers=[UsenetServer(host="news.a.net")]),
                                  "/download", rt, SimpleNamespace(descriptor=descriptor), executions=work)
    admin._clock = clock
    applied = []

    async def apply():
        applied.append(clock.now)
        return ConfigurationApplication(applies, "configuration applied" if applies else "the servers were refused")
    admin._apply_configuration = apply        # the native configuration push is other tests' concern
    return admin, rt, work, clock, descriptor, applied


@pytest.mark.asyncio
async def test_enabled_it_is_checked_once_then_stopped_after_the_idle_grace_and_said_idle():
    admin, rt, work, clock, _, applied = build()
    assert (await admin.status())["state"] == "checking"           # not yet seen to start
    await admin.maintain()
    assert rt.calls == ["start"] and applied == [clock.now] and admin.ready_for_work()
    clock.now += IDLE_GRACE_SECONDS - 1
    await admin.maintain()
    assert rt.calls == ["start"]                                     # within the grace: kept
    clock.now += 2
    await admin.maintain()
    assert rt.calls == ["start", "stop"] and not admin.ready_for_work()
    assert (await admin.status())["state"] == "idle"


@pytest.mark.asyncio
async def test_waiting_work_brings_it_up_and_owned_or_observed_work_keeps_it_whatever_the_toggle():
    admin, rt, work, clock, descriptor, _ = build()
    await admin.maintain()
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    assert rt.calls[-1] == "stop"
    work.waiting = True
    await admin.maintain()
    assert rt.calls[-1] == "start" and admin.ready_for_work()
    assert work.order.index("waiting") < work.order.index("live")    # waiting read before owned work
    # Disabled: waiting work no longer counts, but observed work (a succeeded attempt verifying) does.
    descriptor.enabled, work.waiting, work.observed = False, False, True
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    assert rt.calls[-1] == "start"
    work.observed = False
    await admin.maintain()
    assert rt.calls[-1] == "stop"


@pytest.mark.asyncio
async def test_a_rebuilt_administration_shares_its_services_run_state_lock_and_fence():
    """Every configuration rebuilds the administration; the service goes on.
    The new surface knows the service is up (no false 'not ready', no
    restart), and a stop decided through the old one cannot pass a submission
    held through the new one."""
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    admin, rt, work, clock, descriptor, _ = build()
    await admin.maintain()
    rebuilt = SabnzbdAdministration(SimpleNamespace(), UsenetOptions(servers=[UsenetServer(host="news.a.net")]),
                                    "/download", rt, SimpleNamespace(descriptor=descriptor), executions=work)
    rebuilt._clock = clock
    assert rebuilt.ready_for_work() and rebuilt._lock is admin._lock and rebuilt._uses is admin._uses
    await rebuilt.maintain()
    assert rt.calls == ["start"]                                      # no second start, no restart
    clock.now += IDLE_GRACE_SECONDS + 1
    holding, finish = asyncio.Event(), asyncio.Event()

    async def submit():
        async with rebuilt.native_use():
            holding.set()
            await finish.wait()
    task = asyncio.create_task(submit())
    await holding.wait()
    stopping = asyncio.create_task(admin.maintain())                  # the old surface decides a stop
    await asyncio.sleep(0.05)
    assert "stop" not in rt.calls and not stopping.done()             # it waits for the new surface's hold
    finish.set()
    await asyncio.wait_for(asyncio.gather(task, stopping), 2)
    assert rt.calls[-1] == "stop" and not rebuilt.ready_for_work()


@pytest.mark.asyncio
async def test_enabling_through_a_save_or_a_test_still_reaches_the_idle_grace():
    admin, rt, work, clock, descriptor, _ = build(enabled=False)
    descriptor.enabled = True
    assert (await admin.apply_configuration()).ok                     # enabled by a Save
    for _ in range(3):
        clock.now += IDLE_GRACE_SECONDS / 2 + 1
        await admin.maintain()
    assert rt.calls[-1] == "stop"
    # Disabled while it owns work, enabled again while still running: the grace still ends it.
    await admin.maintain()
    work.live = True
    await admin.maintain()
    descriptor.enabled = False
    await admin.maintain()
    descriptor.enabled = True
    work.live = False
    await admin.maintain()
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    assert rt.calls[-1] == "stop"


@pytest.mark.asyncio
async def test_a_failed_start_is_said_and_backed_off_however_often_it_is_woken():
    admin, rt, work, clock, _, _ = build(starts_ok=False)
    work.waiting = True
    for _ in range(20):                                               # woken every second
        await admin.maintain()
        clock.now += 1
    assert rt.calls.count("start") + rt.calls.count("restart") == 1
    status = await admin.status()
    assert status["state"] == "unhealthy" and status["error"] == "it would not start"
    clock.now += START_BACKOFF_SECONDS[0]
    await admin.maintain()
    assert rt.calls.count("start") + rt.calls.count("restart") == 2


@pytest.mark.asyncio
async def test_a_failed_apply_is_a_failed_start_never_idle():
    admin, rt, work, clock, _, _ = build(applies=False)
    await admin.maintain()
    assert not admin.ready_for_work()
    status = await admin.status()
    assert status["state"] == "unhealthy" and status["error"] == "the servers were refused"


@pytest.mark.asyncio
async def test_not_installed_is_never_idle():
    admin, rt, *_ = build(installed=False)
    assert (await admin.status())["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_a_test_meanwhile_maintain_and_its_own_ending_never_deadlock_or_stop_it_under_the_test():
    admin, rt, work, clock, _, _ = build(enabled=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def test():
        async with admin._serving_for_test():
            entered.set()
            await release.wait()
    task = asyncio.create_task(test())
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(admin.maintain(), 2)                      # the lock is not held across the Test
    assert rt.running
    release.set()
    await asyncio.wait_for(task, 2)
    assert rt.calls[-1] == "stop"                                     # disabled, nothing owned: stopped after


@pytest.mark.asyncio
async def test_a_stop_waits_for_a_submission_and_keeps_the_service_when_it_made_work():
    admin, rt, work, clock, _, _ = build()
    await admin.maintain()
    clock.now += IDLE_GRACE_SECONDS + 1
    holding, finish = asyncio.Event(), asyncio.Event()

    async def submit():
        async with admin.native_use():
            holding.set()
            await finish.wait()
            work.live = True                                          # the submission is live work now
    task = asyncio.create_task(submit())
    await holding.wait()
    stopping = asyncio.create_task(admin.maintain())
    await asyncio.sleep(0.05)
    assert not stopping.done() and "stop" not in rt.calls            # waits for the hold
    assert not admin.ready_for_work()                                 # and no new submission is admitted
    finish.set()
    await asyncio.wait_for(asyncio.gather(task, stopping), 2)
    assert "stop" not in rt.calls and admin.ready_for_work()         # read again under the fence: kept


@pytest.mark.asyncio
async def test_a_submission_after_a_stop_is_refused_before_anything_is_sent():
    admin, rt, work, clock, _, _ = build()
    await admin.maintain()
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    with pytest.raises(ServiceNotRunning):
        async with admin.native_use():
            pytest.fail("a stopped service took a submission")


@pytest.mark.asyncio
async def test_a_configuration_change_while_idle_starts_applies_and_says_the_truth():
    admin, rt, work, clock, _, applied = build()
    await admin.maintain()
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    assert rt.calls[-1] == "stop"
    result = await admin.apply_configuration()
    assert result.ok and rt.calls[-1] == "start" and applied[-1] == clock.now
    await admin.maintain()
    assert rt.calls[-1] == "start"                                    # the grace runs from the change


@pytest.mark.asyncio
async def test_the_executor_is_ready_only_once_converged_and_refuses_definitely_otherwise():
    from executors.sabnzbd.executor import SabnzbdExecutor
    from transfers.models import ExecutionState
    admin, rt, work, clock, _, _ = build()
    answered = []

    class Client:
        async def version(self):
            answered.append(True)
            return "4.5.0"
    executor = SabnzbdExecutor.__new__(SabnzbdExecutor)
    executor.client, executor.lifecycle = Client(), admin
    executor.configuration = SimpleNamespace(secrets=())
    health = await executor.health()
    assert (health.reachable, health.ready) == (False, False) and not answered   # its API is not asked
    await admin.maintain()
    assert (await executor.health()).ready
    clock.now += IDLE_GRACE_SECONDS + 1
    await admin.maintain()
    # A start that reaches a stopped service: refused before anything is sent, definitely.
    from transfers.errors import Category
    sent = []

    async def addfile(*_args, **_kwargs):
        sent.append(True)
        return "SABnzbd_nzo_x"
    executor.client.addfile = addfile
    handle = SimpleNamespace(attempt_id="a1")

    async def checked(*_args):
        return None

    async def staged(_request):
        return "manifest"
    executor._check, executor.prepare, executor._token, executor._staged = checked, (lambda _r: handle), (lambda _h: "t"), staged
    executor._secrets = lambda: ()
    executor.staged_input = SimpleNamespace(opened=lambda _s: pytest.fail("input opened for a stopped service"))
    observed = await executor.start(SimpleNamespace(paused=False), handle)
    assert observed.state == ExecutionState.FAILED and observed.error.category == Category.EXECUTOR_UNAVAILABLE
    assert not sent


@pytest.mark.asyncio
async def test_a_deliberate_stop_is_no_exit_to_report():
    """The runtime itself (no process launched): a process it was asked to stop
    leaves no "exited with code N" behind, while one that ended unasked does.
    The stop's own answer still says how that stop ended (its exit code: the
    desktop artifacts' Usenet check proves a clean stop is exit 0)."""
    from executors.sabnzbd.runtime import UsenetRuntime

    class Process:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = 0                                       # it stopped itself cleanly

        async def wait(self):
            return self.returncode
    runtime = UsenetRuntime()
    runtime._process = Process()
    status = await runtime.stop()
    assert runtime._process is None and not status.get("last_error")
    assert status["exit_code"] == 0 and status["running"] is False         # this stop's own exit
    later = await runtime.status()
    assert later["exit_code"] is None and not later["last_error"]          # never read later as a crash
    crashed = UsenetRuntime()
    crashed._process = Process()
    crashed._process.returncode = 1                                   # ended without being asked
    assert "exited with code 1" in (await crashed.status()).get("last_error", "")


# ── core: the runtime coordinator ──────────────────────────────────────────

class Executor:
    def __init__(self, identity, *, current=True):
        self.descriptor = SimpleNamespace(id=identity)
        self.capabilities = SimpleNamespace(aggregate_bandwidth_ceiling=True, minimum_bandwidth_ceiling=101)
        self.current, self.ceilings = current, []

    async def health(self):
        from transfers.models import ExecutorHealth, ExecutorRuntimeCapability
        if not self.current:
            return ExecutorHealth(False, False)
        return ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def set_bandwidth_ceiling(self, value):
        from transfers.models import ExecutorRuntimeControlResult
        self.ceilings.append(value)
        return ExecutorRuntimeControlResult(value, value)


def coordinator(*executors):
    from transfers.runtime_coordination import ExecutionRuntimeCoordinator
    clock = Clock()
    registry = SimpleNamespace(executors={e.descriptor.id: e for e in executors})

    async def live():
        return frozenset()
    c = ExecutionRuntimeCoordinator(lambda: registry, SimpleNamespace(executors_with_live_work=live), clock=clock)
    return c, clock


@pytest.mark.asyncio
async def test_a_cap_lifted_while_the_runtime_is_stopped_is_no_error_and_holds_before_its_next_admission():
    sab = Executor("sabnzbd")
    c, clock = coordinator(sab)
    c.configure(1_000_000)
    assert await c.admit(sab)
    await c.release_absent(frozenset())                               # its work ended: not reserved
    sab.current = False                                               # and its runtime stopped while idle
    c.configure(0)
    status = await c.converge()
    assert status.ok and c._error is None and "sabnzbd" not in c._unproven
    sab.current = True
    ceilings_before = list(sab.ceilings)
    assert await c.admit(sab)
    assert sab.ceilings[len(ceilings_before):] == [0]                 # lifted, proven, before it acquires


@pytest.mark.asyncio
async def test_only_a_not_current_refusal_wakes_and_at_a_bounded_rate():
    from transfers.runtime_coordination import WAKE_INTERVAL_SECONDS
    sab = Executor("sabnzbd", current=False)
    c, clock = coordinator(sab)
    woken = []
    c.not_current = lambda: woken.append(clock.now)
    for _ in range(10):
        assert not await c.admit(sab)
        clock.now += 1
    assert len(woken) == 1 + int(9 // WAKE_INTERVAL_SECONDS)
    sab.current = True
    assert await c.admit(sab)
    assert len(woken) == 1 + int(9 // WAKE_INTERVAL_SECONDS)          # an admission wakes nothing


_ = admin_module


# ── core: the engine's waiting- and observed-work facts ────────────────────

def _snapshot(path):
    import sqlite3
    with sqlite3.connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}


@pytest.mark.asyncio
async def test_queued_work_for_a_runtime_that_is_down_is_waiting_work_and_reading_it_changes_nothing(tmp_path, monkeypatch):
    from executor_fakes import artifact_of, ledger_core, submit_ledger
    core = await ledger_core(tmp_path, monkeypatch)
    executor = core.registry.executors["ledger-copy"]
    executor.reachable = executor.ready = False                      # its runtime is stopped
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.state == "queued" and artifact.execution is None  # admission refused: still queued
    before = _snapshot(tmp_path / "state.db")
    assert await core.engine.executors_with_waiting_work() == frozenset({"ledger-copy"})
    assert await core.engine.executors_with_observed_work() == frozenset()
    assert _snapshot(tmp_path / "state.db") == before                # reads only: no quota turn, no state, no attempt
    # Up again: the next pass admits it; it is observed work now, no longer waiting.
    executor.reachable = executor.ready = True
    await core.engine.tick()
    assert (await artifact_of(core, transfer.id)).execution is not None
    assert "ledger-copy" in await core.engine.executors_with_observed_work()
    assert await core.engine.executors_with_waiting_work() == frozenset()


@pytest.mark.asyncio
async def test_work_dispatch_would_not_admit_is_not_waiting_work(tmp_path, monkeypatch):
    from executor_fakes import ledger_core, submit_ledger
    core = await ledger_core(tmp_path, monkeypatch)
    executor = core.registry.executors["ledger-copy"]
    executor.reachable = executor.ready = False
    transfer = await submit_ledger(core)
    assert await core.engine.executors_with_waiting_work() == frozenset({"ledger-copy"})
    core.engine.dispatch_permitted = False                            # the same gate dispatch reads
    assert await core.engine.executors_with_waiting_work() == frozenset()
    core.engine.dispatch_permitted = True
    await core.engine.pause(transfer.id)                              # a paused transfer is not admitted
    assert await core.engine.executors_with_waiting_work() == frozenset()


# Parity: the waiting-work fact says yes exactly where dispatch, with the
# executor's runtime up, goes on to admit -- over the shared gates, each
# scenario on its own core.
async def _scenario(tmp_path, monkeypatch, name, change):
    from dataclasses import replace

    from executor_fakes import artifact_of, ledger_core, submit_ledger
    core = await ledger_core(tmp_path / name, monkeypatch)
    executor = core.registry.executors["ledger-copy"]
    executor.reachable = executor.ready = False
    transfer = await submit_ledger(core)
    await change(core, executor, transfer, replace)
    waiting = "ledger-copy" in await core.engine.executors_with_waiting_work()
    executor.reachable = executor.ready = True
    await core.engine.tick()
    admitted = (await artifact_of(core, transfer.id)).execution is not None
    return waiting, admitted


async def _nothing(core, executor, transfer, replace):
    return None


async def _paused(core, executor, transfer, replace):
    await core.engine.pause(transfer.id)


async def _provider_off(core, executor, transfer, replace):
    provider = next(iter(core.registry.providers.values()))
    provider.descriptor = replace(provider.descriptor, enabled=False)


async def _executor_off(core, executor, transfer, replace):
    executor.descriptor = replace(executor.descriptor, enabled=False)


async def _dispatch_off(core, executor, transfer, replace):
    core.engine.dispatch_permitted = False


@pytest.mark.asyncio
@pytest.mark.parametrize("name,change,expected", [
    ("ready", _nothing, True), ("paused", _paused, False), ("provider_off", _provider_off, False),
    ("executor_off", _executor_off, False), ("dispatch_off", _dispatch_off, False)])
async def test_the_waiting_work_fact_agrees_with_what_dispatch_admits(tmp_path, monkeypatch, name, change, expected):
    waiting, admitted = await _scenario(tmp_path, monkeypatch, name, change)
    assert (waiting, admitted) == (expected, expected)


@pytest.mark.asyncio
async def test_an_admission_refused_because_the_runtime_is_down_wakes_whoever_brings_it_up(tmp_path, monkeypatch):
    from executor_fakes import ledger_core, submit_ledger
    core = await ledger_core(tmp_path, monkeypatch)
    executor = core.registry.executors["ledger-copy"]
    executor.reachable = executor.ready = False
    woken = []
    core.engine.runtime.not_current = lambda: woken.append(True)
    await submit_ledger(core)
    for _ in range(3):
        await core.engine.tick()                                       # every pass refuses it again
    assert woken == [True]                                              # once within the interval
    executor.reachable = executor.ready = True
    await core.engine.tick()
    assert woken == [True]                                              # an admission wakes nothing


@pytest.mark.asyncio
async def test_a_cleanup_its_runtime_cannot_answer_wakes_whoever_brings_it_up(tmp_path, monkeypatch):
    """A cleanup can come due while an executor's runtime is stopped (a cancel
    of work it holds): the pass that cannot observe it wakes the owner."""
    from executor_fakes import artifact_of, ledger_core, submit_ledger
    from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
    core = await ledger_core(tmp_path, monkeypatch)
    executor = core.registry.executors["ledger-copy"]
    transfer = await submit_ledger(core)
    assert (await artifact_of(core, transfer.id)).execution is not None
    woken = []
    core.engine.runtime.not_current = lambda: woken.append(True)

    executor.observe_failure = NormalizedError(                          # its runtime has stopped
        Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    await core.engine.cancel(transfer.id)
    await core.engine.tick()
    assert woken                                                         # the cleanup pass woke its owner
