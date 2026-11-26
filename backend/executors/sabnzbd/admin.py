"""Integration-owned administration for the Usenet/SAB-backed integration.

Reaches the application through the existing generic ``AdministeredIntegration``
seam; application composition names no executor.

Ownership, exactly:
* The acquisition service is external to the transfer core's implementation --
  core speaks only the generic executor contract and names no service -- and
  internal to DownloadPulse product ownership. DownloadPulse bundles it, starts it,
  stops it, restarts it when it is unhealthy, owns its whole configuration, and
  never exposes it to an operator. No operator-run instance exists for this
  integration, so there is nothing to reconcile ownership with.
* This module therefore owns that lifecycle: ``start``, ``stop`` and
  ``maintain`` below are the seam through which the service runs at all.
* Configuration flows ONE way (DP canonical desired state -> the service). It
  is applied on a configuration change and again whenever the service is
  (re)started, so a restart cannot leave the service running with stale native
  configuration. Service-side state is never imported as canonical DP
  configuration and is never continuously reconciled back; drift is DETECTED
  and REPORTED, then corrected by re-applying the canonical state.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

import asyncio
import time

from executors.sabnzbd import topology
from executors.sabnzbd.client import SabApiError, SabTransportError, ServiceNotRunning
from integrations.definition import ConfigurationApplication
from integrations.usenet.definition import MAX_CONNECTIONS, MIN_CONNECTIONS

# SAB's own server-configuration section/keys.
SERVERS_SECTION = "servers"
MISC_SECTION = "misc"
# Non-secret server fields DownloadPulse can compare against SAB's readback.
# `password` is deliberately absent: SAB returns a constant mask for it, so
# password drift is not detectable (an accepted, reported limitation).
COMPARABLE_SERVER_FIELDS = ("host", "port", "ssl", "connections", "priority",
                            "pipelining_requests", "timeout", "enable")

# Executor-wide acquisition tuning: DownloadPulse canonical field -> the native
# key that carries it. Characterized against the bundled SABnzbd 5.1.3; every
# one of them applies live, none needs a restart. The DownloadPulse canonical
# field names the SEMANTIC; the native key never leaves this module.
_CACHE_LIMIT_KEY = "cache_limit"
_DIRECT_WRITE_KEY = "direct_write"
_ACQUISITION_RETRIES_KEY = "max_art_tries"

_UNIT_MULTIPLIERS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _native_bytes(value) -> int:
    """One native K/M/G/T size string as bytes.

    The native side stores the article-cache limit verbatim, so "1G" and
    "1024M" are the same desired state expressed two ways. Drift detection
    compares the MEANING, never the spelling.
    """
    text = str(value or "").strip().upper()
    if not text:
        return 0
    multiplier = _UNIT_MULTIPLIERS.get(text[-1:], 1)
    if multiplier != 1:
        text = text[:-1]
    try:
        return max(0, int(float(text or 0) * multiplier))
    except (TypeError, ValueError):
        return 0


def _native_bool(value) -> bool:
    """A native boolean readback, which arrives as a JSON bool or as 0/1."""
    return value in (1, True, "1", "True", "true")


def derived_display_name(server) -> str:
    """The operator-facing name: an explicit override, else derived from host."""
    override = str(getattr(server, "display_name", "") or "").strip()
    return override or str(getattr(server, "host", "") or "").strip()


# Every native server DownloadPulse manages carries this prefix, so a stale one
# left behind by an earlier configuration is always recognisable as ours.
MANAGED_PREFIX = "dp-"


def server_keyword(server) -> str:
    """The native key for one configured server: its CANONICAL id.

    Deliberately derived from nothing else. A key built from list position,
    host, priority or display name would move whenever the operator reorders,
    re-hosts, re-prioritises or renames a server, silently orphaning the
    native record and its credential. The canonical id is stable across all of
    those, so the native identity is too.
    """
    identity = str(getattr(server, "id", "") or "").strip()
    safe = "".join(ch for ch in identity if ch.isalnum() or ch in "-_")
    if not safe:
        raise ServerIdentityError("a configured server has no canonical identity")
    return MANAGED_PREFIX + safe


# Enabled but idle: how long after the last work needed it the service stops
# (1.0.5, operator: 1 minute; it was 10). Owned and observed work (a
# succeeded attempt still verifying) keeps it up whatever this is, so the
# grace only bridges gaps between submissions; the maintenance pass that
# applies it runs about once a minute, so the stop comes 1-2 minutes after.
IDLE_GRACE_SECONDS = 60.0
# After a failed start or apply, how long before the next try (then the last, repeatedly).
START_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0)


class ServiceRunState:
    """One managed service's run decision (1.0.5 WS9), held by its runtime so
    it outlives every administration built on it: Tests in progress; the
    last time work needed it; whether the enable check ran; whether it is
    started and converged (``ready_for_work``); the last failed start or apply
    and its backoff; the decision lock; and the native-submission fence."""

    def __init__(self) -> None:
        self.tests = 0
        self.needed_at: float | None = None
        self.checked = False
        self.ready = False
        self.start_failed: str | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.lock = asyncio.Lock()
        self.uses = asyncio.Condition()
        self.in_use = 0
        self.stopping = False


def _shared(name: str) -> property:
    """An administration attribute that is its service's run state."""
    return property(lambda self: getattr(self._state, name),
                    lambda self, value: setattr(self._state, name, value))


class ServerIdentityError(ValueError):
    """A configured server cannot be mapped to a stable native identity."""


@dataclass
class DriftReport:
    """What SAB's effective configuration says versus DP's desired state.

    Purely informational. Nothing here is ever written back into DownloadPulse's
    canonical configuration.
    """
    reachable: bool
    differences: tuple[str, ...] = ()
    error: str = ""

    @property
    def drifted(self) -> bool:
        return bool(self.differences)

    def public(self) -> dict:
        return {
            "reachable": self.reachable,
            "drifted": self.drifted,
            # Field names only -- never values, which sit beside credentials.
            "differences": list(self.differences),
            "error": self.error,
            "password_drift_detectable": False,
            "synchronization": "none",
        }


def _without(text: str, *secrets: str) -> str:
    """Remove operator credentials from anything surfaced back to the operator.

    The native service quotes parts of a failed request in its own message.
    That message reaches an API response and the application log, so a
    credential the operator just typed must not ride along in it.
    """
    result = str(text or "")
    for secret in secrets:
        value = str(secret or "")
        if len(value) >= 4:
            result = result.replace(value, "<redacted>")
    return result


class SabnzbdAdministration:
    # The ONE canonical settings namespace this surface applies. Declared by the
    # implementation so the generic composition seam names no integration.
    configuration_namespace = "usenet"

    def __init__(self, client, options, download_root: str, runtime=None, executor=None,
                 executions=None, notify_status=None):
        self.client = client
        self.options = options
        self.download_root = download_root
        self.runtime = runtime
        self.executor = executor
        # Core's execution decisions (``ExecutionGrants``), never the repository.
        self.executions = executions
        # The application's neutral live-status signal: how a runtime
        # transition this lifecycle owns reaches Provider Status.
        self._notify_status = notify_status
        self._clock = time.monotonic
        # The service's run state lives with the service, not with this
        # surface: every configuration rebuilds the administration while the
        # one process (``runtime``) goes on, so its decision facts, its lock and
        # its fence are the runtime's own (``ServiceRunState``), shared by every
        # administration built on it (1.0.5 WS9). A runtime without that slot
        # (a test double) gives this surface a state of its own.
        state = getattr(runtime, "demand", None) if runtime is not None else None
        if state is None:
            state = ServiceRunState()
            if runtime is not None and hasattr(runtime, "demand"):
                runtime.demand = state
        self._state = state

    _tests = _shared("tests")
    _needed_at = _shared("needed_at")
    _checked = _shared("checked")
    _ready = _shared("ready")
    _start_failed = _shared("start_failed")
    _failures = _shared("failures")
    _retry_at = _shared("retry_at")
    _lock = _shared("lock")
    _uses = _shared("uses")
    _in_use = _shared("in_use")
    _stopping = _shared("stopping")

    # --- managed lifecycle (the generic IntegrationLifecycle seam) --------
    #
    # The service runs while it is needed (1.0.5 WS9): a server Test, work
    # core owns or observes through this executor, queued work that waits for
    # it, the one check when Usenet is enabled (or DownloadPulse starts with it
    # enabled), and an idle grace after the last of those. Enabled but idle, it
    # is stopped; queued work brings it up again (core's admission refusal
    # wakes ``maintain``). Every decision and transition holds ``_lock``; the
    # executor's native submissions hold the fence (``native_use``), which a
    # stop takes exclusively -- so a submission never meets a service being
    # stopped, and a stop never ends one under way.

    @property
    def _accepts_new_work(self) -> bool:
        """Whether NEW work may be routed here. This is the enable toggle, and
        it is deliberately NOT the same question as whether the service is
        needed."""
        descriptor = getattr(self.executor, "descriptor", None)
        return bool(getattr(descriptor, "enabled", False))

    @property
    def _identity(self) -> str:
        return str(getattr(getattr(self.executor, "descriptor", None), "id", ""))

    async def _owns_durable_work(self) -> bool:
        """Whether any execution still belongs to this executor: durable live
        work, or work the reconcile pass observes through it (a succeeded
        attempt still verifying) -- whatever the enable toggle, so disabling
        Usenet never strands one. Uncertainty counts as ownership."""
        if self.executions is None:
            return True
        try:
            return (await self.executions.has_live_work(self._identity)
                    or await self.executions.has_observed_work(self._identity))
        except Exception:
            return True

    async def _work_waiting(self) -> bool:
        """Whether queued work waits for this executor (enabled only: core
        routes nothing to a disabled one). Uncertainty counts as waiting."""
        if not self._accepts_new_work or self.executions is None:
            return False
        try:
            return await self.executions.has_waiting_work(self._identity)
        except Exception:
            return True

    def ready_for_work(self) -> bool:
        """The executor's readiness: the service was started AND converged on
        the canonical configuration (its own API answers before that), and no
        stop is under way."""
        return self._ready and not self._stopping

    @contextlib.asynccontextmanager
    async def native_use(self):
        """One native submission's hold on the service. Refused -- definitely,
        before anything is sent -- while it is not ready for work (stopped, or
        being stopped); a stop waits for every hold to end."""
        async with self._uses:
            if not self.ready_for_work():
                raise ServiceNotRunning("the acquisition service is not running")
            self._in_use += 1
        try:
            yield
        finally:
            async with self._uses:
                self._in_use -= 1
                self._uses.notify_all()

    async def start(self) -> None:
        await self.maintain()

    async def stop(self) -> None:
        if self.runtime is not None:
            async with self._lock:
                self._ready = False
                await self.runtime.stop()

    async def maintain(self) -> None:
        """Converge the internal service on what is actually required.

        Nothing here inspects or mutates DP-owned executions; it only decides
        whether the executor's service must be available. Waiting work is read
        before owned work, so a submission moving from queued to prepared in
        between is seen as one or the other."""
        if self.runtime is None:
            return
        async with self._lock:
            now = self._clock()
            enabled = self._accepts_new_work
            if not enabled:
                self._checked = False                         # enabling again is checked again
            waiting = await self._work_waiting()
            owned = await self._owns_durable_work()
            if waiting or owned:
                self._needed_at = now
            grace = enabled and self._needed_at is not None and now - self._needed_at < IDLE_GRACE_SECONDS
            if self._tests or owned or waiting or grace or (enabled and not self._checked):
                if await self._service_converged():
                    if enabled and not self._checked:
                        # Converged by another path (a configuration change, a Test):
                        # the enable check is done; the grace runs from here.
                        self._checked, self._needed_at = True, now
                    return
                if now < self._retry_at:
                    return                                    # a failed start backs off; no wakeup hurries it
                await self._converge()
                if enabled and self._ready and not self._checked:
                    self._checked = True                      # the enable check: then the grace runs from here
                    self._needed_at = now
                return
            if (await self.runtime.status())["running"]:
                await self._stop_when_unused()

    async def _service_converged(self) -> bool:
        return self._ready and await self.runtime.healthy()

    async def _stop_when_unused(self) -> None:
        """Stop the service once no native submission holds it, unless one
        that began meanwhile made it needed again (read again under the fence)."""
        async with self._uses:
            self._stopping = True
            while self._in_use:
                await self._uses.wait()
        try:
            if await self._work_waiting() or await self._owns_durable_work():
                return
            self._ready = False
            await self.runtime.stop()
            await self._status_changed()
        finally:
            async with self._uses:
                self._stopping = False

    @contextlib.asynccontextmanager
    async def _serving_for_test(self):
        """The service for one server Test, enabled or not: brought up for it
        when it is not running (the managed path, ``_converge``), and
        afterwards left exactly as ``maintain`` decides. The lock is held for
        the decisions, never across the Test itself, so ``maintain`` runs
        meanwhile -- and finds a Test counted, so never stops the service."""
        if self.runtime is None:
            yield
            return
        async with self._lock:
            self._tests += 1
            try:
                if not await self._service_converged():
                    await self._converge()
            except BaseException:
                self._tests -= 1
                raise
        try:
            yield
        finally:
            async with self._lock:
                self._tests -= 1
            await self.maintain()

    async def _converge(self) -> None:
        """Bring the service to healthy, then to the canonical configuration
        (``_lock`` held). A live-but-unhealthy process is RESTARTED: starting an
        already-running process is a no-op, so it would otherwise never
        recover. Ready for work only once started AND, while Usenet is enabled,
        converged on the configuration: a failed start or apply is recorded
        (``start_failed``) and retried after a backoff, never hidden.
        Both runtime transitions are announced as they happen."""
        self._ready = False
        await self._status_changed()
        # A healthy running service is left running (only converged on the
        # configuration); a live-but-unhealthy one is restarted; else started.
        if not await self.runtime.healthy():
            if (await self.runtime.status())["running"]:
                await self.runtime.restart()
            else:
                await self.runtime.start()
        problem = None
        if not await self.runtime.healthy():
            problem = (await self.runtime.status()).get("last_error") or "the acquisition service did not start"
        elif self._accepts_new_work:
            applied = await self._apply_configuration()
            if not applied.ok:
                problem = applied.detail or "the configuration did not apply"
        if problem is None:
            self._ready, self._start_failed, self._failures, self._retry_at = True, None, 0, 0.0
        else:
            self._start_failed = str(problem)
            self._failures += 1
            self._retry_at = self._clock() + START_BACKOFF_SECONDS[min(self._failures, len(START_BACKOFF_SECONDS)) - 1]
        await self._status_changed()

    async def _status_changed(self) -> None:
        if self._notify_status is not None:
            await self._notify_status()

    async def apply_configuration(self) -> ConfigurationApplication:
        """Push the canonical desired configuration into the native service:
        the ONE production path from a settings mutation (and the configuration
        applier's entry). While the service is stopped because it was idle,
        the change brings it up for that change as a Test does -- start, apply,
        drift check, the honest result -- and the grace runs from there."""
        if self.runtime is None:
            return await self._apply_configuration()
        async with self._lock:
            if self._accepts_new_work and not await self._service_converged():
                await self._converge()
                self._needed_at = self._clock()
                if self._ready:
                    self._checked = True
                if self._start_failed is not None:
                    return ConfigurationApplication(False, self._start_failed, ("service",))
                return ConfigurationApplication(True, "configuration applied")
            return await self._apply_configuration()

    async def _apply_configuration(self) -> ConfigurationApplication:
        """Push the canonical desired configuration into the native service.

        This is the ONE production path from a settings mutation to the native
        configuration transaction. It runs on a configuration change and on
        service (re)start, flows one way, and imports nothing back. A failure
        is reported, never swallowed: the canonical namespace remains the
        desired state, but the operator is told the service does not yet match
        it.
        """
        failures: list[str] = []
        try:
            converged = await self.apply_topology()
        except (SabTransportError, SabApiError) as exc:
            return ConfigurationApplication(False, f"could not reach the service: {exc}", ("service",))
        except Exception as exc:
            failures.append("topology")
            return ConfigurationApplication(False, f"working-path topology was rejected: {exc}",
                                            tuple(failures))
        if not converged:
            failures.append("topology")
            return ConfigurationApplication(
                False, "the service did not accept the required working-path topology",
                tuple(failures))
        try:
            # Executor-wide acquisition tuning is pushed BEFORE the servers, so
            # the drift check below is only ever evaluated against the whole
            # desired state rather than a half-applied one.
            await self.apply_tuning()
        except (SabTransportError, SabApiError) as exc:
            failures.append("tuning")
            return ConfigurationApplication(False, f"acquisition tuning failed: {exc}",
                                            tuple(failures))
        try:
            await self.apply_servers()
        except (SabTransportError, SabApiError) as exc:
            failures.append("servers")
            return ConfigurationApplication(False, f"server configuration failed: {exc}",
                                            tuple(failures))
        report = await self.drift()
        if not report.reachable:
            return ConfigurationApplication(False, f"could not verify configuration: {report.error}",
                                            ("verification",))
        if report.drifted:
            return ConfigurationApplication(
                False, "the service did not converge on the requested configuration",
                report.differences)
        return ConfigurationApplication(True, "configuration applied")

    async def _topology_matches(self) -> bool:
        try:
            misc = (await self.client.get_config(MISC_SECTION)).get("misc") or {}
        except (SabTransportError, SabApiError):
            return False
        return (Path(str(misc.get("download_dir") or "")) == Path(topology.incomplete_root(self.download_root))
                and Path(str(misc.get("complete_dir") or "")) == Path(topology.complete_root(self.download_root)))

    # --- Test (no persistence) ------------------------------------------

    async def test_server(self, *, host, port, ssl, username, password, connections) -> dict:
        """Validate a prospective server WITHOUT persisting anything.

        SAB's ``test_server`` accepts ad-hoc connection parameters, performs a
        real NNTP connection and leaves the stored server list untouched, so an
        edited-but-unsaved card can be tested with no shadow configuration.

        A connection count below the DownloadPulse floor is REFUSED rather than
        raised to 1 for the duration of the test. Coercing it would let a
        configuration report a successful test and then sit there unable to
        acquire, which is precisely the false readiness this guards against.
        """
        try:
            requested = int(connections)
        except (TypeError, ValueError):
            requested = 0
        if requested < MIN_CONNECTIONS:
            return {"ok": False, "reachable": False,
                    "message": f"A server needs at least {MIN_CONNECTIONS} connection to acquire.",
                    "detail": f"connections must be between {MIN_CONNECTIONS} and {MAX_CONNECTIONS}"}
        try:
            async with self._serving_for_test():
                ok, message = await self.client.test_server(
                    host=host, port=int(port), ssl=1 if ssl else 0,
                    username=username or "", password=password or "",
                    connections=requested,
                )
        except (SabTransportError, SabApiError) as exc:
            return {"ok": False, "reachable": False, "message": _without(str(exc), password)}
        # The native message is echoed to the operator, so anything the service
        # quoted back out of the request is scrubbed before it is surfaced.
        return {"ok": ok, "reachable": True, "message": _without(str(message), password)}

    # --- one-way apply (canonical desired state -> native service) -------
    #
    # Driven by an explicit configuration mutation OR by lifecycle convergence
    # after the service starts/restarts. One-way authority is the invariant,
    # not who invoked it.

    async def apply_topology(self) -> bool:
        """Converge the service's working paths onto the DownloadPulse topology.

        **Idempotent by design.** The service legitimately refuses to move its
        working directory while it has queue or post-processing state, so a
        redundant rewrite would make an ordinary news-server edit fail during
        an active download. Effective topology is therefore read first and
        written ONLY when it actually differs.

        Order matters when a write is needed: the service refuses a
        ``complete_dir`` that is the same as, or inside, the CURRENT
        ``download_dir``, so the incomplete path moves first.

        Returns whether the topology is correct once this returns.
        """
        if await self._topology_matches():
            return True
        await self.client.set_config(MISC_SECTION, "download_dir",
                                     value=topology.incomplete_root(self.download_root))
        await self.client.set_config(MISC_SECTION, "complete_dir",
                                     value=topology.complete_root(self.download_root))
        return await self._topology_matches()

    async def apply_tuning(self) -> None:
        """Push the canonical executor-wide acquisition tuning. One way, never back.

        Every value here is a DownloadPulse-owned canonical field projected onto
        the native key that carries it; nothing native is ever read back into
        the canonical namespace. The service applies all three live.
        """
        options = self.options
        megabytes = max(0, int(getattr(options, "article_cache_megabytes", 0) or 0))
        # The native side takes a K/M/G string; "0" means no article cache.
        await self.client.set_config(MISC_SECTION, _CACHE_LIMIT_KEY,
                                     value=f"{megabytes}M" if megabytes else "0")
        await self.client.set_config(MISC_SECTION, _DIRECT_WRITE_KEY,
                                     value=1 if getattr(options, "direct_write", True) else 0)
        await self.client.set_config(MISC_SECTION, _ACQUISITION_RETRIES_KEY,
                                     value=int(getattr(options, "max_acquisition_retries", 3) or 3))

    async def apply_servers(self) -> None:
        """Push the canonical desired server set to SAB. One way, never back."""
        desired = list(getattr(self.options, "servers", []) or [])
        keywords = set()
        for server in desired:
            keyword = server_keyword(server)
            keywords.add(keyword)
            await self.client.set_config(
                SERVERS_SECTION, keyword,
                host=server.host, port=server.port, ssl=1 if server.ssl else 0,
                username=server.username, password=server.password,
                connections=server.connections, priority=server.priority,
                pipelining_requests=server.articles_per_request,
                timeout=server.timeout_seconds,
                enable=1 if server.enabled else 0,
                displayname=derived_display_name(server),
            )
        # Remove EVERY native server the canonical configuration does not
        # declare. The service is internal and private -- no operator UI, no
        # published port, no operator credential -- so a server DownloadPulse did
        # not declare has no legitimate origin. Left in place it would still
        # take part in acquisition, with a credential DP never stored and the
        # operator cannot see or revoke, so tolerating it would put the
        # canonical list beside a second, invisible authority.
        try:
            effective = (await self.client.get_config(SERVERS_SECTION)).get("servers") or []
        except (SabTransportError, SabApiError):
            return
        for entry in effective:
            name = str(entry.get("name") or "")
            if name and name not in keywords:
                await self.client.del_config(SERVERS_SECTION, name)

    # --- drift detection (report only) -----------------------------------

    async def drift(self) -> DriftReport:
        """Compare the service's effective configuration with DP's desired state.

        THIS FUNCTION is detection only: it reads, compares and reports.
        Nothing here imports, merges or reconciles anything, and it is not a
        synchronization loop.

        Correcting reported drift is a separate step, and it is one-way
        (canonical desired state -> native service). It happens through either
        canonical configuration application or lifecycle convergence after the
        service starts/restarts -- not only when an operator re-saves.
        """
        try:
            servers = (await self.client.get_config(SERVERS_SECTION)).get("servers") or []
            misc = (await self.client.get_config(MISC_SECTION)).get("misc") or {}
        except (SabTransportError, SabApiError) as exc:
            return DriftReport(False, error=str(exc))

        differences: list[str] = []
        effective = {str(entry.get("name") or ""): entry for entry in servers}
        expected: set[str] = set()
        for server in (getattr(self.options, "servers", []) or []):
            keyword = server_keyword(server)
            expected.add(keyword)
            entry = effective.get(keyword)
            if entry is None:
                differences.append(f"servers.{keyword}.missing")
                continue
            desired = {
                "host": str(server.host or "").lower(), "port": int(server.port),
                "ssl": 1 if server.ssl else 0, "connections": int(server.connections),
                "priority": int(server.priority),
                "pipelining_requests": int(server.articles_per_request),
                "timeout": int(server.timeout_seconds),
                "enable": 1 if server.enabled else 0,
            }
            for field in COMPARABLE_SERVER_FIELDS:
                native = entry.get(field)
                if field in ("ssl", "enable"):
                    native = 1 if native in (1, True, "1") else 0
                elif field in ("port", "connections", "priority",
                               "pipelining_requests", "timeout"):
                    try:
                        native = int(native)
                    except (TypeError, ValueError):
                        native = -1
                else:
                    native = str(native or "").lower()
                if native != desired[field]:
                    differences.append(f"servers.{keyword}.{field}")

        # ANY server the canonical configuration does not declare is drift --
        # an orphan DP once managed, or one injected out of band. Either can
        # still take part in acquisition, so neither may read as "fully
        # synchronized". Names only: the values sit beside credentials.
        for name in sorted(effective):
            if name and name not in expected:
                differences.append(f"servers.{name}.unexpected")

        for key, want in (("download_dir", topology.incomplete_root(self.download_root)),
                          ("complete_dir", topology.complete_root(self.download_root))):
            actual = str(misc.get(key) or "")
            if actual and Path(actual) != Path(want):
                # An out-of-band path change breaks the DP topology invariant.
                differences.append(f"misc.{key}")

        # Executor-wide acquisition tuning. Names only, never values -- and the
        # article cache is compared by MEANING, so an equivalent native unit
        # spelling is not reported as drift.
        options = self.options
        desired_cache = max(0, int(getattr(options, "article_cache_megabytes", 0) or 0)) * 1024 ** 2
        if _native_bytes(misc.get(_CACHE_LIMIT_KEY)) != desired_cache:
            differences.append(f"misc.{_CACHE_LIMIT_KEY}")
        if _native_bool(misc.get(_DIRECT_WRITE_KEY)) is not bool(getattr(options, "direct_write", True)):
            differences.append(f"misc.{_DIRECT_WRITE_KEY}")
        try:
            native_retries = int(misc.get(_ACQUISITION_RETRIES_KEY))
        except (TypeError, ValueError):
            native_retries = -1
        if native_retries != int(getattr(options, "max_acquisition_retries", 3) or 3):
            differences.append(f"misc.{_ACQUISITION_RETRIES_KEY}")
        return DriftReport(True, tuple(differences))

    # --- readiness -------------------------------------------------------

    async def status(self) -> dict:
        """Neutral readiness for the sidebar provider-status surface.

        Healthy means the native configuration is genuinely usable RIGHT NOW:
        reachable, required working-path topology in place, at least one usable
        server, and no detected drift from the canonical desired state.
        Enabled-but-unconfigured is a legitimate configuration state and is
        explicitly NOT ready.
        """
        # The acquisition service is internal and always present, so it is
        # never evidence of configuration. Only news servers are.
        servers = [s for s in (getattr(self.options, "servers", []) or [])
                   if s.enabled and str(s.host or "").strip()]
        if not servers:
            return {"state": "unconfigured", "configured": False, "servers": 0}
        counted = {"configured": True, "servers": len(servers)}
        if self.runtime is not None:
            # Started on demand (1.0.5 WS9): a failed start or apply is said as
            # such; a stopped service is Idle only once it was seen to start and
            # converge (the enable check) and while it can start again -- and
            # while it is being started for work, it is still Idle, not down.
            if not self.runtime.installed():
                return {**counted, "state": "unhealthy", "error": "the Usenet acquisition service is not installed"}
            if self._start_failed is not None:
                return {**counted, "state": "unhealthy", "error": self._start_failed}
            if not self._ready:
                return {**counted, "state": "idle" if self._checked else "checking"}
        try:
            version = await self.client.version()
        except (SabTransportError, SabApiError) as exc:
            return {"state": "unhealthy", "configured": True, "servers": len(servers),
                    "error": str(exc)}
        base = {"configured": True, "servers": len(servers), "version": version}
        report = await self.drift()
        if not report.reachable:
            return {**base, "state": "unhealthy", "error": report.error}
        if report.drifted:
            # The service is reachable but does not match what DownloadPulse
            # requires, so acquisition cannot be trusted to behave as configured.
            return {**base, "state": "unhealthy", "drifted": True,
                    "differences": list(report.differences)}
        if not await self._topology_matches():
            return {**base, "state": "unhealthy", "drifted": True, "differences": ["misc.download_dir"]}
        return {**base, "state": "healthy", "drifted": False}
