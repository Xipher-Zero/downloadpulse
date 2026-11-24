"""DP 1.0.14 setup wizard, WS1: the installation's setup lifecycle and a new
Server installation's named Administrator (``services.setup_lifecycle``,
``api.setup_routes``).

Proven through the real database bootstrap (``db.migrations.v112``), the
real application and its middleware, the real sign-in form and the
application's own session store. Synthetic accounts and passwords only; every
installation is a scratch directory."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import auth.manager as manager
import auth.passwords as passwords
import auth.transitions as transitions
import core.config as config
import db.database as database
from auth import accounts
from auth.manager import PasswordAttemptAdmission
from auth.models import Principal
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from db.migrations import v112
from services import db_maintenance, setup_lifecycle

pytestmark = pytest.mark.asyncio

# Synthetic secrets: test values only.
ADMIN_PASSWORD = "synthetic-setup-admin-1"
OTHER_PASSWORD = "synthetic-setup-other-2"
USER_PASSWORD = "synthetic-setup-user-3"


@pytest.fixture(autouse=True)
def fresh_admission(monkeypatch):
    """This test's own password-work admission, Argon2 slots and locks:
    process-wide asyncio primitives bind to the loop they first wait in."""
    monkeypatch.setattr(manager, "password_attempt_admission", PasswordAttemptAdmission())
    monkeypatch.setattr(passwords, "_PASSWORD_VERIFY_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def own_sign_in_budget():
    """This test's own sign-in page budget and failure throttle, returned
    unused: both are process-wide."""
    from auth.throttle import login_challenge_rate_limiter, password_failure_throttle

    login_challenge_rate_limiter.clear()
    password_failure_throttle.clear()
    yield
    login_challenge_rate_limiter.clear()
    password_failure_throttle.clear()


def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "data" / "downloadpulse.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config" / "config.json")
    monkeypatch.setattr(config, "_settings", config.AppSettings())


def _row(path=None):
    with closing(sqlite3.connect(path or database.DB_PATH)) as conn:
        return conn.execute("SELECT initial_setup_complete, initial_phase, bootstrap_account_id, "
                            "bootstrap_configured_at IS NOT NULL FROM installation_setup").fetchall()


def _named_admins():
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        return conn.execute("SELECT id, username FROM accounts WHERE role='admin' AND protected IS NULL "
                            "AND deleted_at IS NULL").fetchall()


def _saved() -> dict:
    return json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) if config.CONFIG_PATH.exists() else {}


@pytest_asyncio.fixture
async def installation(tmp_path, monkeypatch):
    """A genuinely new installation: no database, no configuration file."""
    _paths(tmp_path, monkeypatch)
    await v112.migrate()
    return tmp_path


@pytest_asyncio.fixture
async def live(installation, monkeypatch):
    import main

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setitem(main.app.state._state, "application", SimpleNamespace(application_operation=operation))
    session_store.clear()

    def headers(token: str) -> dict:
        return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(client=client, headers=headers)
    session_store.clear()


async def _bootstrap(live, username="operator-one", password=ADMIN_PASSWORD, **kwargs):
    return await live.client.post("/api/setup/initial/administrator",
                                  json={"username": username, "password": password}, **kwargs)


def _cookie(response) -> str:
    match = re.search(rf"{HTTP_SESSION_COOKIE}=([^;]*)", response.headers.get("set-cookie", ""))
    return match.group(1) if match else ""


async def _sign_in(live, username, password):
    login = await live.client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    return await live.client.post("/login", data={"username": username, "password": password, "csrf_token": csrf})


# ── a new installation versus an existing one ──────────────────────────────

async def test_a_genuinely_new_installation_starts_its_setup_at_welcome(installation):
    assert _row() == [(0, "welcome", None, 0)]
    assert (await setup_lifecycle.initial_setup()).complete is False
    # Starting again changes nothing: the setup is never reset by a restart.
    await v112.migrate()
    assert _row() == [(0, "welcome", None, 0)]


async def test_a_fresh_database_beside_an_existing_configuration_is_not_a_new_installation(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    config.CONFIG_PATH.parent.mkdir(parents=True)
    config.CONFIG_PATH.write_text(json.dumps({"deployment_type": "server"}), encoding="utf-8")
    await v112.migrate()
    assert _row() == []
    assert (await setup_lifecycle.initial_setup()).complete is True


async def test_an_existing_database_without_the_record_is_complete(tmp_path, monkeypatch):
    # An installation from before the setup wizard: its database is already
    # current and holds no setup record.
    _paths(tmp_path, monkeypatch)
    await database.init_db()
    await v112._mark_current()
    await v112.migrate()
    assert _row() == []
    assert (await setup_lifecycle.state(accounts.DEFAULT_ADMIN_ID))["initial"] == {
        "complete": True, "phase": None, "administrator": "none"}


async def test_the_new_installation_decision_reads_the_setup_records_alone(installation, monkeypatch):
    """The pre-sign-in data boundary: what the wizard state reads is the two
    setup tables -- never an account, credential or setting."""
    statements: list[str] = []
    real = database.get_db

    @asynccontextmanager
    async def traced():
        async with real() as db:
            await db._raw.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(database, "get_db", traced)

    def no_settings():
        raise AssertionError("the setup state read a setting")

    monkeypatch.setattr(config, "get_settings", no_settings)
    assert (await setup_lifecycle.initial_setup()).complete is False
    await setup_lifecycle.state(accounts.DEFAULT_ADMIN_ID)
    tables = {name for sql in statements for name in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", sql, re.I)}
    assert statements and tables == {"installation_setup", "account_setup_rerun"}


async def test_no_sign_in_page_or_public_path_reads_the_setup_state_but_pairing_starts_eligibility(live, monkeypatch):
    """No sign-in page or public path reads the setup state -- with one
    intended exception (1.0.3, operator-approved review): a browser pairing
    start, which may not begin until setup is finished (api/browser_routes.py
    start_browser_pairing). It reads only whether setup is finished, once, after
    its rate limiter and before any pairing state exists; a refused (rate
    limited) start reads nothing."""
    reads = []
    real = setup_lifecycle._initial

    async def counted(db):
        reads.append(True)
        return await real(db)

    monkeypatch.setattr(setup_lifecycle, "_initial", counted)
    config._settings = config.AppSettings(auth_password_enabled=True)
    await accounts.create_local_account("someone", USER_PASSWORD, role="admin")
    for path in ("/login", "/api/auth/status", "/api/health", "/api/version", "/manifest.webmanifest"):
        await live.client.get(path)
    await _sign_in(live, "someone", "synthetic-wrong-password")
    assert reads == []
    # And the setup state is no public path: without a session it is refused.
    assert (await live.client.get("/api/setup/state")).status_code == 401
    assert reads == []
    # The one exception: a pairing start asks once whether setup is finished ...
    await live.client.post("/api/browser/v1/pairings", json={"client_name": "Browser"})
    assert reads == [True]
    # ... and only once past its rate limiter: a refused start reads nothing.
    from api import browser_routes
    monkeypatch.setattr(browser_routes.pairing_start_limiter, "allow", lambda _key: False)
    assert (await live.client.post("/api/browser/v1/pairings", json={"client_name": "Browser"})).status_code == 429
    assert reads == [True]


# ── classification ──────────────────────────────────────────────────────────

async def test_every_setup_route_is_an_admin_action():
    names = [name for name in ACTIONS if name.startswith("api.setup_routes.")]
    assert len(names) == 7 and all(ACTIONS[name] is Capability.ADMINISTER for name in names)


# ── Standalone ──────────────────────────────────────────────────────────────

STANDALONE_PASSWORD = "synthetic-standalone-4"


async def _set_password(live, password=STANDALONE_PASSWORD, **kwargs):
    return await live.client.post("/api/setup/initial/password", json={"password": password}, **kwargs)


async def _finished_standalone(live) -> dict:
    """A Standalone installation's setup, finished with its password set: the
    headers of the default Admin's session it leaves signed in."""
    config._settings = config.AppSettings(deployment_type="standalone")
    token = _cookie(await _set_password(live))
    finished = await live.client.post("/api/setup/initial/finish", headers=live.headers(token))
    assert finished.status_code == 200, finished.text
    return live.headers(token)


async def test_a_standalone_server_sets_its_password_before_it_can_finish_and_continues_signed_in(live):
    """1.0.4 (operator, 2026-11-24): a Standalone installation that is not the
    desktop application sets its one person's password in setup; without it,
    neither the pages after it nor Finish are admitted."""
    config._settings = config.AppSettings(deployment_type="standalone")
    for phase in ("deployment", "download_directory", "administrator"):
        moved = await live.client.put("/api/setup/initial/phase", json={"phase": phase})
        assert moved.status_code == 200 and moved.json()["initial"]["phase"] == phase
    state = (await live.client.get("/api/setup/state")).json()["initial"]
    assert state["administrator"] == "none"
    pending = await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"})
    assert pending.status_code == 409 and pending.json()["code"] == "setup.password_pending"
    assert (await live.client.post("/api/setup/initial/finish")).json()["code"] == "setup.password_pending"
    # The Server's Administrator is not this installation's.
    assert (await _bootstrap(live)).json()["code"] == "setup.bootstrap_unavailable"

    created = await _set_password(live)
    assert created.status_code == 200, created.text
    # Setup resumes after this page; the setup state reads no account, so the
    # wizard knows the password from its own new session.
    assert created.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "none"}
    assert STANDALONE_PASSWORD not in created.text
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert (await accounts.get_account(accounts.DEFAULT_ADMIN_ID)).has_password
    # No Server record is written for it: the fact is the password itself.
    assert _row() == [(0, "premium_services", None, 0)] and _named_admins() == []
    token = _cookie(created)
    record = session_store.resolve(token)
    assert record is not None and record.principal.account_id == accounts.DEFAULT_ADMIN_ID
    assert record.principal.authenticated
    # Signed in: the open principal is gone (a browser without the session is
    # asked to sign in), and the session continues setup.
    live.client.cookies.clear()
    assert (await live.client.get("/api/setup/state")).status_code == 401
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "getting_started"},
                                  headers=live.headers(token))).status_code == 200
    # The page that set it is not set twice.
    again = await _set_password(live, headers=live.headers(token))
    assert again.status_code == 403
    finished = await live.client.post("/api/setup/initial/finish", headers=live.headers(token))
    assert finished.status_code == 200 and finished.json() == {"complete": True, "sign_in_required": False}
    assert _row()[0][0] == 1

    # Afterwards the owner may leave it open on a network they trust: Clear
    # Password turns sign-in off, and nothing in setup prevents that.
    cleared = await live.client.post("/api/account/clear-password", json={"current_password": STANDALONE_PASSWORD},
                                     headers=live.headers(token))
    assert cleared.status_code == 200, cleared.text
    assert config.get_settings().auth_password_enabled is False
    assert (await live.client.get("/api/setup/state")).status_code == 200


async def test_the_desktop_application_finishes_without_a_password(live, monkeypatch, tmp_path):
    monkeypatch.setenv("DOWNLOADPULSE_DESKTOP", "1")
    folder = tmp_path / "Downloads"
    folder.mkdir()
    config._settings = config.AppSettings(deployment_type="standalone", download_folder=str(folder))
    for phase in ("download_directory", "premium_services", "getting_started"):
        moved = await live.client.put("/api/setup/initial/phase", json={"phase": phase})
        assert moved.status_code == 200, moved.text
    refused = await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})
    assert refused.status_code == 409 and refused.json()["code"] == "setup.phase_unavailable"
    assert (await _set_password(live)).json()["code"] == "setup.phase_unavailable"
    finished = await live.client.post("/api/setup/initial/finish")
    assert finished.status_code == 200 and finished.json() == {"complete": True, "sign_in_required": False}
    assert config.get_settings().auth_password_enabled is False


async def test_the_setup_password_is_refused_to_a_server_and_after_setup(live):
    assert (await _set_password(live)).json()["code"] == "setup.phase_unavailable"
    assert config.get_settings().auth_password_enabled is False
    config._settings = config.AppSettings(deployment_type="standalone")
    async with database.get_db() as db:
        await db.execute("UPDATE installation_setup SET initial_setup_complete=1 WHERE id=1")
        await db.commit()
    done = await _set_password(live)
    assert done.status_code == 409 and done.json()["code"] == "setup.initial_complete"
    assert config.get_settings().auth_password_enabled is False


async def test_a_password_set_in_settings_counts_and_one_cleared_mid_setup_is_asked_for_again(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    first = await live.client.post("/api/account/first-password", json={"new_password": STANDALONE_PASSWORD})
    assert first.status_code == 200, first.text
    login = await live.client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    signed = await live.client.post("/login", data={"password": STANDALONE_PASSWORD, "csrf_token": csrf})
    token = _cookie(signed)
    assert token, signed.text
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"},
                                  headers=live.headers(token))).status_code == 200
    cleared = await live.client.post("/api/account/clear-password", json={"current_password": STANDALONE_PASSWORD},
                                     headers=live.headers(token))
    assert cleared.status_code == 200, cleared.text
    # Open again, with no password: Finish asks for one once more.
    refused = await live.client.post("/api/setup/initial/finish")
    assert refused.status_code == 409 and refused.json()["code"] == "setup.password_pending"


async def test_two_concurrent_passwords_set_one(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    answers = await asyncio.gather(_set_password(live), _set_password(live, password=OTHER_PASSWORD))
    codes = sorted(answer.status_code for answer in answers)
    assert codes[0] == 200 and codes[1] in {403, 409}, [answer.text for answer in answers]
    loser = next(answer for answer in answers if answer.status_code != 200)
    assert loser.status_code == 403 or loser.json()["code"] in {"account.password_exists", "account.auth_changed"}
    assert (await accounts.get_account(accounts.DEFAULT_ADMIN_ID)).has_password
    assert config.get_settings().auth_password_enabled is True


async def test_an_interrupted_standalone_password_is_undone_at_the_next_start(installation):
    """Username & Password written to the file, the password's transaction never
    committed: at start it is turned off again, so the page is offered again."""
    config._settings = config.AppSettings(deployment_type="standalone", auth_password_enabled=True)
    config.save_settings(config._settings)
    await setup_lifecycle.settle_interrupted_bootstrap()
    assert config.get_settings().auth_password_enabled is False and _saved()["auth_password_enabled"] is False
    # A finished installation, or one whose password exists, is left as it is.
    config._settings = config.AppSettings(deployment_type="standalone", auth_password_enabled=True)
    async with database.get_db() as db:
        await db.execute("UPDATE installation_setup SET initial_setup_complete=1 WHERE id=1")
        await db.commit()
    await setup_lifecycle.settle_interrupted_bootstrap()
    assert config.get_settings().auth_password_enabled is True


async def test_the_deployment_type_cannot_strand_an_unfinished_setup(live):
    # A Server whose Administrator exists: the change would leave neither its page
    # nor Finish passable, so it is refused until setup is finished.
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})).status_code == 200
    token = _cookie(await _bootstrap(live))
    refused = await live.client.put("/api/auth/deployment-type", json={"deployment_type": "standalone", "confirm": True},
                                    headers=live.headers(token))
    assert refused.status_code == 409 and refused.json()["code"] == "setup.deployment_locked"
    assert config.get_settings().deployment_type == "server"


async def test_a_standalone_with_its_password_may_still_become_a_server_during_setup(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    token = _cookie(await _set_password(live))
    changed = await live.client.put("/api/auth/deployment-type", json={"deployment_type": "server", "confirm": True},
                                    headers=live.headers(token))
    assert changed.status_code == 200, changed.text
    # The change removed the password and turned sign-in off: the Server's own
    # Administrator page is open, so setup can still finish.
    assert config.get_settings().auth_password_enabled is False
    assert not (await accounts.get_account(accounts.DEFAULT_ADMIN_ID)).has_password
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})).status_code == 200
    assert (await _bootstrap(live)).status_code == 200


# ── setup is not offered to the internet ───────────────────────────────────

INTERNET_V4, INTERNET_V6 = "8.8.8.8", "2606:4700:4700::1111"


@asynccontextmanager
async def _from(address: str):
    import main

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False, client=(address, 4711))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield client


@pytest.mark.parametrize("address", [INTERNET_V4, INTERNET_V6, f"::ffff:{INTERNET_V4}"])
async def test_an_unfinished_setup_is_refused_to_the_internet(live, address):
    async with _from(address) as client:
        api = await client.get("/api/setup/state")
        assert api.status_code == 403 and api.json() == {
            "detail": "Finish setting up DownloadPulse from your own network first: setup is not available "
                      "from the internet.", "code": "setup.from_internet", "params": {}}
        page = await client.get("/")
        assert page.status_code == 403 and page.headers["content-type"].startswith("text/html")
        assert "Finish setting up from your own network" in page.text and "<script" not in page.text
        # Neither the reverse-proxy bootstrap nor a Basic header in open mode is a way round it.
        bootstrap = await client.put("/api/auth/config", json={"public_base_url": "https://dl.example.com"},
                                     headers={"Origin": "https://dl.example.com", "Host": "dl.example.com"})
        assert bootstrap.status_code == 403 and bootstrap.json()["code"] == "setup.from_internet"
        basic = await client.get("/api/setup/state", headers={"Authorization": "Basic YTpi"})
        assert basic.status_code == 403 and basic.json()["code"] == "setup.from_internet"
        # A public path is still a public path.
        assert (await client.get("/api/health")).status_code == 200
    assert config.get_settings().public_base_url == ""


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.20", "10.0.0.7", "172.20.0.3", "100.101.102.103",
                                     "fd12:3456::1", "fe80::1", "::1"])
async def test_the_home_network_a_vpn_and_this_computer_set_up_as_before(live, address):
    async with _from(address) as client:
        assert (await client.get("/api/setup/state")).status_code == 200


@pytest.mark.parametrize("headers", [
    {"X-Forwarded-For": INTERNET_V4}, {"X-Forwarded-For": f"192.168.1.5, {INTERNET_V4}"},
    {"Forwarded": f'for="[{INTERNET_V6}]:4711";proto=https'}, {"X-Real-IP": INTERNET_V4},
    {"CF-Connecting-IP": INTERNET_V4}, {"X-Forwarded-For": "unknown"}, {"Forwarded": "for=_hidden"},
])
async def test_a_reverse_proxy_on_the_private_network_passes_on_where_the_request_began(live, headers):
    async with _from("172.20.0.5") as client:
        refused = await client.get("/api/setup/state", headers=headers)
        assert refused.status_code == 403 and refused.json()["code"] == "setup.from_internet"
        # A forwarded private address is still the home network.
        assert (await client.get("/api/setup/state", headers={"X-Forwarded-For": "192.168.1.5"})).status_code == 200


@pytest.mark.parametrize("name, forged, real", [
    ("X-Forwarded-For", "10.0.0.1", INTERNET_V4), ("Forwarded", "for=10.0.0.1", f"for={INTERNET_V4}")])
async def test_every_line_of_a_forwarding_header_is_read(live, name, forged, real):
    # A proxy that adds its own line after the client's (HAProxy): the client's forged
    # private line comes first, the proxy's line naming the real address second.
    async with _from("172.20.0.5") as client:
        refused = await client.get("/api/setup/state", headers=[(name, forged), (name, real)])
        assert refused.status_code == 403 and refused.json()["code"] == "setup.from_internet"


async def test_once_setup_is_finished_an_open_installation_is_open_to_whoever_reaches_it(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    token = _cookie(await _set_password(live))
    assert (await live.client.post("/api/setup/initial/finish", headers=live.headers(token))).status_code == 200
    await live.client.post("/api/account/clear-password", json={"current_password": STANDALONE_PASSWORD},
                           headers=live.headers(token))
    assert config.get_settings().auth_password_enabled is False
    async with _from(INTERNET_V4) as client:
        assert (await client.get("/api/setup/state")).status_code == 200


# ── Server: the named Administrator ─────────────────────────────────────────

async def test_a_server_setup_creates_a_distinct_administrator_and_continues_as_it(live):
    default_before = await accounts.get_account(accounts.DEFAULT_ADMIN_ID)
    pending = await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"})
    assert pending.status_code == 409 and pending.json()["code"] == "setup.administrator_pending"
    assert (await live.client.post("/api/setup/initial/finish")).json()["code"] == "setup.administrator_pending"
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})).status_code == 200

    created = await _bootstrap(live)

    assert created.status_code == 200, created.text
    assert created.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}
    assert ADMIN_PASSWORD not in created.text
    [(admin_id, username)] = _named_admins()
    assert username == "operator-one" and admin_id != accounts.DEFAULT_ADMIN_ID
    # Username & Password is in force, in memory and on disk.
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)]
    # The protected default Admin is exactly as it was: no password, same name.
    default_after = await accounts.get_account(accounts.DEFAULT_ADMIN_ID)
    assert (default_after.username, default_after.has_password, default_after.protected, default_after.role) == \
        (default_before.username, False, "default_admin", accounts.Role.ADMIN)

    # The browser holds an ordinary password session for the new Administrator.
    token = _cookie(created)
    record = session_store.resolve(token)
    assert record is not None and record.principal.account_id == admin_id
    assert record.principal.authenticated and record.principal.role == "admin"
    # Without the CSRF token its session cannot change anything.
    no_csrf = await live.client.put("/api/setup/initial/phase", json={"phase": "getting_started"},
                                    headers={"Cookie": f"{HTTP_SESSION_COOKIE}={token}"})
    assert no_csrf.status_code == 403
    # The pages that created it are not where setup resumes again.
    back = await live.client.put("/api/setup/initial/phase", json={"phase": "deployment"}, headers=live.headers(token))
    assert back.status_code == 409 and back.json()["code"] == "setup.phase_unavailable"
    onward = await live.client.put("/api/setup/initial/phase", json={"phase": "getting_started"},
                                   headers=live.headers(token))
    assert onward.status_code == 200 and onward.json()["initial"]["phase"] == "getting_started"

    finished = await live.client.post("/api/setup/initial/finish", headers=live.headers(token))

    assert finished.status_code == 200 and finished.json() == {"complete": True, "sign_in_required": True}
    assert "Max-Age=0" in finished.headers.get("set-cookie", "") or 'expires=' in finished.headers.get("set-cookie", "").lower()
    assert _row() == [(1, None, admin_id, 1)]
    # The wizard's session is over: it no longer admits anything.
    assert session_store.resolve(token) is None
    assert (await live.client.get("/api/setup/state", headers=live.headers(token))).status_code == 401
    # Only the new Administrator's real credentials sign in again.
    assert (await _sign_in(live, "operator-one", OTHER_PASSWORD)).status_code == 401
    signed = await _sign_in(live, "operator-one", ADMIN_PASSWORD)
    assert signed.status_code == 303
    assert session_store.resolve(_cookie(signed)).principal.account_id == admin_id


async def test_an_invalid_administrator_is_refused_and_changes_nothing(live):
    for username, password, code in (("operator-one", "short7!", "account.password_too_short"),
                                     ("operator-one", "", "account.password_required"),
                                     ("   ", ADMIN_PASSWORD, "account.username_required"),
                                     ("Administrator", ADMIN_PASSWORD, "account.username_taken")):
        refused = await _bootstrap(live, username, password)
        assert refused.status_code == 409 and refused.json()["code"] == code, (username, refused.text)
        assert password not in refused.text or not password
    typed = await live.client.post("/api/setup/initial/administrator", json={"username": "x", "password": 12345678})
    assert typed.status_code == 422
    assert _named_admins() == [] and _row() == [(0, "welcome", None, 0)]
    assert config.get_settings().auth_password_enabled is False and _saved() == {}


async def test_two_concurrent_submissions_create_one_administrator(live):
    first, second = await asyncio.gather(_bootstrap(live, "operator-one", ADMIN_PASSWORD),
                                         _bootstrap(live, "operator-two", OTHER_PASSWORD))
    statuses = sorted((first.status_code, second.status_code))
    assert statuses == [200, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] in {"setup.administrator_exists", "setup.bootstrap_unavailable"}
    assert len(_named_admins()) == 1 and _cookie(loser) == ""


async def test_after_the_administrator_exists_no_open_or_signed_in_caller_can_create_another(live):
    created = await _bootstrap(live)
    token = _cookie(created)
    live.client.cookies.clear()
    # A stale browser with no session: sign-in is enabled now, so it is refused.
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD)).status_code == 401
    # A lost response replayed: the same submission is not a second account.
    assert (await _bootstrap(live)).status_code == 401
    # The signed-in Administrator is not the open principal.
    signed = await _bootstrap(live, "operator-two", OTHER_PASSWORD, headers=live.headers(token))
    assert signed.status_code == 403
    assert len(_named_admins()) == 1
    # A lost response is recovered by ordinary sign-in, and setup resumes after the Administrator.
    signed_in = await _sign_in(live, "operator-one", ADMIN_PASSWORD)
    state = await live.client.get("/api/setup/state", headers=live.headers(_cookie(signed_in)))
    assert state.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}


async def test_a_failed_setting_write_after_the_account_commits_is_finished_by_the_same_submission(live, monkeypatch):
    real = config.save_settings
    failures = iter([True])

    def failing(settings):
        if next(failures, False):
            raise OSError("synthetic write failure")
        return real(settings)

    monkeypatch.setattr(config, "save_settings", failing)

    failed = await _bootstrap(live)

    assert failed.status_code == 500 and failed.json()["code"] == "setup.administrator_not_enabled"
    assert _cookie(failed) == "" and ADMIN_PASSWORD not in failed.text
    [(admin_id, _name)] = _named_admins()
    # Never enabled without its Administrator, and still open: recoverable.
    assert config.get_settings().auth_password_enabled is False and _saved() == {}
    assert _row() == [(0, "welcome", admin_id, 0)]
    assert (await live.client.get("/api/setup/state")).json()["initial"]["administrator"] == "pending"
    # Another submission is refused, never a second Administrator.
    other = await _bootstrap(live, "operator-two", OTHER_PASSWORD)
    assert other.status_code == 409 and other.json()["code"] == "setup.administrator_exists"
    wrong = await _bootstrap(live, "operator-one", OTHER_PASSWORD)
    assert wrong.status_code == 409 and wrong.json()["code"] == "setup.administrator_exists"

    finished = await _bootstrap(live)

    assert finished.status_code == 200, finished.text
    assert _named_admins() == [(admin_id, "operator-one")]
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)]
    assert session_store.resolve(_cookie(finished)).principal.account_id == admin_id


async def test_an_interrupted_administrator_is_finished_at_the_next_start(installation, monkeypatch):
    # The crash window: the account committed, the process stopped before the setting.
    monkeypatch.setattr(config, "save_settings", lambda settings: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(setup_lifecycle.SetupRefused):
        await setup_lifecycle.establish_administrator(
            SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), "operator-one", ADMIN_PASSWORD)
    [(admin_id, _name)] = _named_admins()
    monkeypatch.undo()
    _paths(installation, monkeypatch)  # the restarted process: settings as stored
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    assert config.get_settings().auth_password_enabled is False

    await setup_lifecycle.settle_interrupted_bootstrap()

    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)] and len(_named_admins()) == 1
    # Settling again changes nothing.
    await setup_lifecycle.settle_interrupted_bootstrap()
    assert _row() == [(0, "premium_services", admin_id, 1)]


async def test_a_failed_record_after_sign_in_is_enabled_answers_truthfully_and_creates_no_second_admin(live, monkeypatch):
    """The failure is injected into the ``_settle`` transaction that runs after
    Username & Password was written: the answer is a failure with no session,
    never a 200 carrying a stale state."""
    real = setup_lifecycle._settle
    calls = []

    async def failing(db):
        calls.append(_saved().get("auth_password_enabled"))
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic settle failure")
        return await real(db)

    monkeypatch.setattr(setup_lifecycle, "_settle", failing)

    failed = await _bootstrap(live)

    assert calls == [True]  # it ran after the setting was on disk
    assert failed.status_code == 500 and failed.json()["code"] == "setup.administrator_not_recorded"
    assert _cookie(failed) == "" and ADMIN_PASSWORD not in failed.text
    [(admin_id, _name)] = _named_admins()
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "welcome", admin_id, 0)]
    # Sign-in is enabled now: no further submission is admitted, so none can create another.
    live.client.cookies.clear()
    assert (await _bootstrap(live)).status_code == 401
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD)).status_code == 401
    # Ordinary sign-in is the way on; the next setup step records the Administrator.
    token = _cookie(await _sign_in(live, "operator-one", ADMIN_PASSWORD))
    state = await live.client.get("/api/setup/state", headers=live.headers(token))
    assert state.json()["initial"] == {"complete": False, "phase": "welcome", "administrator": "pending"}
    onward = await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"},
                                   headers=live.headers(token))
    assert onward.status_code == 200, onward.text
    assert onward.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}
    assert _row() == [(0, "premium_services", admin_id, 1)] and _named_admins() == [(admin_id, "operator-one")]


async def test_a_server_finish_is_refused_once_sign_in_by_password_is_disabled(live):
    created = await _bootstrap(live)
    [(admin_id, _name)] = _named_admins()
    # Username & Password disabled afterwards (its ordinary authority's effect):
    # the installation is open again, and its caller the open principal.
    config._settings = config.get_settings().model_copy(update={"auth_password_enabled": False})
    live.client.cookies.clear()

    refused = await live.client.post("/api/setup/initial/finish")

    assert refused.status_code == 409 and refused.json()["code"] == "setup.finish_needs_password"
    assert _row() == [(0, "premium_services", admin_id, 1)]
    assert (await setup_lifecycle.initial_setup()).complete is False
    assert _cookie(created)


async def test_a_server_finish_is_refused_to_a_caller_without_a_browser_session(live):
    import base64

    await _bootstrap(live)
    [(admin_id, _name)] = _named_admins()
    live.client.cookies.clear()
    basic = base64.b64encode(f"operator-one:{ADMIN_PASSWORD}".encode()).decode()

    refused = await live.client.post("/api/setup/initial/finish", headers={"Authorization": f"Basic {basic}"})

    assert refused.status_code == 403 and refused.json()["code"] == "auth.forbidden"
    assert _row() == [(0, "premium_services", admin_id, 1)]
    # No credential at all: the boundary refuses it before Finish is reached.
    assert (await live.client.post("/api/setup/initial/finish")).status_code == 401
    assert (await setup_lifecycle.initial_setup()).complete is False


async def test_a_finished_installation_never_reopens_the_administrator(live):
    await _finished_standalone(live)
    config._settings = config.AppSettings()
    refused = await _bootstrap(live)
    assert refused.status_code == 409 and refused.json()["code"] == "setup.initial_complete"
    assert _named_admins() == []


# ── an Admin's rerun ────────────────────────────────────────────────────────

async def test_a_rerun_is_its_own_admins_and_never_reopens_the_initial_setup(live):
    early = await live.client.put("/api/setup/rerun", json={"phase": "welcome"})
    assert early.status_code == 409 and early.json()["code"] == "setup.initial_incomplete"
    created = await _bootstrap(live)
    ann_token = _cookie(created)
    await live.client.post("/api/setup/initial/finish", headers=live.headers(ann_token))
    ann = await accounts.local_credential("operator-one")
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    una = await accounts.create_local_account("una", USER_PASSWORD, role="user")

    def session(account):
        principal = Principal.password_session(account)
        token, _ = session_store.create(principal, lifetime_seconds=3600, credential_version=principal.credential_version)
        return token

    ann_token, bea_token, una_token = session(ann[0]), session(bea), session(una)
    started = await live.client.put("/api/setup/rerun", json={"phase": "administrator"}, headers=live.headers(ann_token))
    assert started.status_code == 200 and started.json()["rerun"] == {"phase": "administrator"}
    assert started.json()["initial"]["complete"] is True
    # Admin B does not inherit Admin A's rerun.
    assert (await live.client.get("/api/setup/state", headers=live.headers(bea_token))).json()["rerun"] is None
    # A User may neither read nor change a setup.
    for method, path, body in (("GET", "/api/setup/state", None), ("PUT", "/api/setup/rerun", {"phase": "welcome"}),
                               ("DELETE", "/api/setup/rerun", None), ("POST", "/api/setup/initial/finish", None)):
        refused = await live.client.request(method, path, json=body, headers=live.headers(una_token))
        assert refused.status_code == 403, (method, path)
    # A rerun never creates an Administrator, even mid-rerun.
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD, headers=live.headers(ann_token))).status_code == 403
    # Dismissed: nothing reverts and the initial setup stays complete.
    ended = await live.client.delete("/api/setup/rerun", headers=live.headers(ann_token))
    assert ended.status_code == 200 and ended.json()["rerun"] is None and ended.json()["initial"]["complete"] is True
    assert _row()[0][0] == 1


async def test_a_standalone_rerun_has_no_administrator_page(live):
    signed_in = await _finished_standalone(live)
    refused = await live.client.put("/api/setup/rerun", json={"phase": "administrator"}, headers=signed_in)
    assert refused.status_code == 409 and refused.json()["code"] == "setup.phase_unavailable"
    kept = await live.client.put("/api/setup/rerun", json={"phase": "premium_services"}, headers=signed_in)
    assert kept.status_code == 200 and kept.json()["rerun"] == {"phase": "premium_services"}


async def test_a_removed_admins_rerun_is_retired_with_it(installation):
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        conn.execute("UPDATE installation_setup SET initial_setup_complete=1")
        conn.commit()
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    await setup_lifecycle.set_rerun(bea.id, "welcome")
    await accounts.delete_account(accounts.DEFAULT_ADMIN_ID, bea.id)
    assert (await setup_lifecycle.state(bea.id))["rerun"] is None


# ── wipe ────────────────────────────────────────────────────────────────────

async def test_a_wipe_keeps_the_setup_and_every_rerun(installation):
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        conn.execute("UPDATE installation_setup SET initial_setup_complete=1, initial_phase=NULL")
        conn.commit()
    await setup_lifecycle.set_rerun(bea.id, "premium_services")
    before = _row()
    result = await db_maintenance.wipe_database(verified_quiesced=True)
    assert not {"installation_setup", "account_setup_rerun"} & set(result["wiped_tables"])
    assert _row() == before
    assert (await setup_lifecycle.state(bea.id))["rerun"] == {"phase": "premium_services"}


# ── WS3b: the TEMPORARY development-only preview's build fact ───────────────

async def test_a_development_build_is_named_by_its_own_changelog_entry_title():
    from core.version import development_entry_title

    changelog = ("# Changelog\n\n## [2.0.1] — Development\n\n2.0.1 once said `## [2.0.0] — Development` in passing.\n\n"
                 "## [2.0.0] — A release\n\n### [2.0.0] notes\n- the one change\n")
    assert development_entry_title(changelog, "2.0.1") is True
    # a released entry, a heading quoted in another entry, a prefix of a version and an unknown one are not
    assert development_entry_title(changelog, "2.0.0") is False
    assert development_entry_title(changelog, "2.0") is False
    assert development_entry_title(changelog, "9.9.9") is False


async def test_the_running_build_fails_closed_without_a_readable_development_entry(monkeypatch, tmp_path):
    import core.version as version

    def running(text, number="3.1.0"):
        path = tmp_path / "CHANGELOG.md"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(version, "_changelog_candidates", lambda: (path,))
        monkeypatch.setattr(version, "read_version", lambda: number)
        version.is_development_build.cache_clear()
        return version.is_development_build()

    try:
        assert running("## [3.1.0] — Development\n") is True
        assert running("## [3.1.0] — The release\n\n## [3.0.0] — Development\n") is False
        assert running(None) is False
        assert running("## [unknown] — Development\n", number="unknown") is False
    finally:
        version.is_development_build.cache_clear()


async def test_the_setup_state_says_whether_this_is_a_development_build(live, monkeypatch):
    import api.setup_routes as routes

    for development in (True, False):
        monkeypatch.setattr(routes, "is_development_build", lambda value=development: value)
        state = (await live.client.get("/api/setup/state")).json()
        assert state["development_build"] is development and "initial" in state


async def test_the_preview_is_offered_only_to_a_development_build_that_opts_in(live, monkeypatch):
    # Test First-Run Wizard: hidden by default even in development; shown only
    # where a development build runs with DOWNLOADPULSE_SETUP_PREVIEW=1; never
    # in a released build, whatever its environment.
    import api.setup_routes as routes

    for development, env, offered in ((True, None, False), (True, "0", False), (True, "true", False),
                                      (True, "1", True), (True, " 1 ", True),
                                      (False, "1", False), (False, None, False)):
        monkeypatch.setattr(routes, "is_development_build", lambda value=development: value)
        if env is None:
            monkeypatch.delenv(routes.SETUP_PREVIEW_ENV, raising=False)
        else:
            monkeypatch.setenv(routes.SETUP_PREVIEW_ENV, env)
        state = (await live.client.get("/api/setup/state")).json()
        assert state["setup_preview"] is offered, (development, env)
        assert state["development_build"] is development


@pytest.mark.parametrize(("value", "internet"), [
    ("8.8.8.8", True), ("8.8.8.8:443", True), ("[2606:4700:4700::1111]:4711", True), ('"[2606:4700::1111]"', True),
    ("::ffff:8.8.8.8", True), ("192.168.1.5", False), ("::ffff:192.168.1.5", False), ("fe80::1%eth0", False),
    ("100.64.0.1", False), ("127.0.0.1", False), ("203.0.113.9", False), ("unknown", None), ("_hidden", None),
])
async def test_an_address_is_the_internet_only_when_globally_routable(value, internet):
    from auth.policy import _internet_address

    assert _internet_address(value) is internet
