"""WS-A3: the application's Content-Security-Policy.

``main.request_id_middleware`` gives every response ``script-src 'self'``
(``APPLICATION_CONTENT_SECURITY_POLICY``) with ``setdefault``, so a page that
states its own policy keeps it; FastAPI's documentation pages have theirs. No
inline script or inline event-handler attribute runs under it, so the
interface declares its controls (``data-dp-click`` and its siblings) and each
renderer dispatches them from its own frozen table (``ui-actions.js``
``DPActions.delegate``). These checks keep the interface free of inline
handlers, keep every declared action in a table, and pin which pages state
their own policy and what the server-built scripts embed. The browser proof is
``frontend/browser/content-security-policy.spec.js``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi import Request, Response

import main
from main import (APPLICATION_CONTENT_SECURITY_POLICY, DOCUMENTATION_CONTENT_SECURITY_POLICY,
                  DOCUMENTATION_PAGE_PATHS, request_id_middleware)

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"
STATIC = FRONTEND / "static"
SPECS = FRONTEND / "browser"

# An inline handler attribute in any quoting -- "...", '...', `...`, ${...}
# or unquoted -- in markup or in a markup string, never a property such as
# ``overlay.onclick = ...``.
EVENT = (r"(?:abort|animation(?:cancel|end|iteration|start)|auxclick|before(?:input|print|toggle|unload)|blur|cancel"
         r"|canplay(?:through)?|change|click|close|contextmenu|copy|cuechange|cut|dblclick|drag(?:end|enter|leave|over"
         r"|start)?|drop|durationchange|emptied|ended|error|focus(?:in|out)?|formdata|hashchange|input|invalid"
         r"|key(?:down|press|up)|load(?:eddata|edmetadata|end|start)?|message|mouse(?:down|enter|leave|move|out|over|up)"
         r"|paste|pause|play(?:ing)?|pointer(?:cancel|down|enter|leave|move|out|over|up)|popstate|progress|ratechange"
         r"|reset|resize|scroll(?:end)?|securitypolicyviolation|seeked|seeking|select(?:ionchange|start)?|show"
         r"|slotchange|stalled|storage|submit|suspend|timeupdate|toggle|touch(?:cancel|end|move|start)"
         r"|transition(?:cancel|end|run|start)|unload|volumechange|waiting|wheel)")
INLINE_HANDLER = re.compile(r"""(?<![\w.$-])on""" + EVENT + r"""\s*=\s*(?:["'`]|\$\{|[^\s"'`=>]+)""", re.I)
ATTRIBUTE_DEPENDENT = re.compile(
    r"""\[\s*on""" + EVENT + r"""\s*(?:[~|^$*]?=|\])|(?:get|has|set|remove)Attribute(?:NS)?\(\s*["'`]on""" + EVENT,
    re.I)
ACTION_ATTRIBUTE = re.compile(r"""data-dp-(click|change|input|keydown)=["']([^"'$]*)["']""")
DELEGATION = re.compile(r"""DPActions\.delegate\([^,]+,\s*'(click|change|input|keydown)',\s*\{\n(.*?)\n\s*\}\);""",
                        re.S)
ACTION_KEY = re.compile(r"^\s*([A-Za-z]\w*):", re.M)
# The renderers that own a delegated table, and the markup each one renders.
ACTION_OWNERS = ("app.js", "ui-downloads.js", "ui-dashboard-transfer-presentation.js")


def _frontend_sources(*suffixes: str) -> list[Path]:
    return sorted(path for path in STATIC.rglob("*") if path.suffix in suffixes and "vendor" not in path.parts)


def _rel(path: Path) -> str:
    return str(path.relative_to(FRONTEND))


def _tables(source: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for kind, body in DELEGATION.findall(source):
        tables.setdefault(kind, set()).update(ACTION_KEY.findall(body))
    return tables


def test_no_inline_event_handler_in_markup_or_markup_strings() -> None:
    # The static copy of the OIDC completion page is not served by any route
    # (its inline script would be blocked, harmlessly); every other page and
    # script is the interface's.
    found = [f"{_rel(path)}:{source[:match.start()].count(chr(10)) + 1}: {match.group(0)}"
             for path in _frontend_sources(".html", ".js")
             for source in [path.read_text(encoding="utf-8")]
             for match in INLINE_HANDLER.finditer(source)]
    assert not found, f"inline event handlers cannot run under script-src 'self': {found}"


def test_inline_handler_pattern_catches_every_quoting() -> None:
    for markup in ('<div onclick="go()">', "<div onclick='go()'>", "`<b onclick=`", "<b onclick=${go}>",
                   "<img src=x onerror=alert(1)>", '<a ONCLICK = "x">'):
        assert INLINE_HANDLER.search(markup), markup
    for code in ("overlay.onclick = event => {}", "overlay.onclick = null;", "data-dp-click=\"nav\"",
                 "const one = transfers.length === 1;", "const only = consequences[0];"):
        assert not INLINE_HANDLER.search(code), code


def test_nothing_depends_on_handler_attributes() -> None:
    sources = _frontend_sources(".css", ".js", ".html") + sorted(SPECS.glob("*.js"))
    found = [f"{path.name}: {match.group(0)}" for path in sources
             for match in ATTRIBUTE_DEPENDENT.finditer(path.read_text(encoding="utf-8"))]
    assert not found, f"selectors and reads must use the declared action attributes: {found}"


def test_every_declared_action_has_an_entry_in_its_owners_table() -> None:
    tables = {name: _tables((STATIC / name).read_text(encoding="utf-8")) for name in ACTION_OWNERS}
    for name in ACTION_OWNERS:   # every table is read: one action per line, closed by "});"
        source = (STATIC / name).read_text(encoding="utf-8")
        assert source.count("DPActions.delegate(") == len(DELEGATION.findall(source)) > 0, name
    others = [path.name for path in _frontend_sources(".js")
              if "DPActions.delegate(" in path.read_text(encoding="utf-8") and path.name not in ACTION_OWNERS]
    assert not others, f"a new action owner belongs in ACTION_OWNERS: {others}"
    union: dict[str, set[str]] = {}
    for owned in tables.values():
        for kind, names in owned.items():
            union.setdefault(kind, set()).update(names)
    assert set(union) == {"click", "change", "input", "keydown"}
    # A renderer's own markup is dispatched by its own table; the shell's
    # (index.html) by app.js from the page body, except the Downloads page's
    # toolbar, which ui-downloads.js dispatches from #view-torrents.
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    downloads_at, downloads_end = index.index('<div class="view" id="view-torrents">'), index.index("<!-- Events -->")
    markup = [("index.html", index[:downloads_at] + index[downloads_end:], tables["app.js"]),
              ("index.html#view-torrents", index[downloads_at:downloads_end], tables["ui-downloads.js"])]
    markup += [(path.name, path.read_text(encoding="utf-8"), tables.get(path.name, {}))
               for path in _frontend_sources(".html", ".js") if path.name != "index.html"]
    missing = [f"{name}: data-dp-{kind}={action}" for name, source, known in markup
               for kind, action in ACTION_ATTRIBUTE.findall(source) if action not in known.get(kind, set())]
    assert not missing, f"declared actions without an entry in their owner's table: {missing}"
    declared = {(kind, name) for path in _frontend_sources(".html", ".js")
                for kind, name in ACTION_ATTRIBUTE.findall(path.read_text(encoding="utf-8"))}
    unused = sorted((kind, name) for kind, names in union.items() for name in names if (kind, name) not in declared)
    assert not unused, f"table entries no control declares: {unused}"


def test_the_delegation_helper_never_resolves_a_name_and_loads_before_its_users() -> None:
    helper = (STATIC / "ui-actions.js").read_text(encoding="utf-8")
    assert "window[" not in helper and "globalThis" not in helper and "eval(" not in helper
    assert "Object.freeze(Object.assign(Object.create(null), table))" in helper
    assert "window.DPActions = Object.freeze({delegate});" in helper
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    helper_at = index.index('<script src="/ui-actions.js?v=1" defer></script>')
    for user in ("/app.js?v=", "/ui-downloads.js?v=", "/ui-dashboard-transfer-presentation.js?v="):
        assert helper_at < index.index(user), user
    # No inline script element in the shell.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index)


def _request(path: str) -> Request:
    return Request({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                    "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                    "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 12345),
                    "server": ("localhost", 80)})


def test_the_application_policy_restricts_script_only() -> None:
    assert APPLICATION_CONTENT_SECURITY_POLICY == (
        "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    assert "unsafe" not in APPLICATION_CONTENT_SECURITY_POLICY
    assert DOCUMENTATION_PAGE_PATHS == {"/docs", "/docs/oauth2-redirect", "/redoc"}
    assert "'unsafe-inline' https://cdn.jsdelivr.net" in DOCUMENTATION_CONTENT_SECURITY_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/downloads", "/settings", "/app.js", "/ui-actions.js", "/style.css",
                                  "/i18n/en.js", "/api/health", "/api/torrents", "/openapi.json"])
async def test_every_application_response_carries_the_policy(path: str) -> None:
    async def call_next(_request):
        return Response(content="body")

    response = await request_id_middleware(_request(path), call_next)
    assert response.headers["Content-Security-Policy"] == APPLICATION_CONTENT_SECURITY_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc"])
async def test_documentation_pages_have_their_own_policy(path: str) -> None:
    async def call_next(_request):
        return Response(content="<html></html>", media_type="text/html")

    response = await request_id_middleware(_request(path), call_next)
    assert response.headers["Content-Security-Policy"] == DOCUMENTATION_CONTENT_SECURITY_POLICY


@pytest.mark.asyncio
async def test_a_page_with_its_own_policy_keeps_it() -> None:
    own = "default-src 'none'; frame-ancestors 'none'"

    async def call_next(_request):
        return Response(content="page", headers={"Content-Security-Policy": own})

    response = await request_id_middleware(_request("/login"), call_next)
    assert response.headers.getlist("Content-Security-Policy") == [own]


# The pages allowed to state their own policy: the one list. Each sets it in
# the function named here, and nowhere else does backend code set one.
OWN_POLICY_PAGES = {
    ("main.py", "request_id_middleware"),                      # the application's and the docs' policies
    ("api/auth_routes.py", "_login_page"),                     # sign-in
    ("api/auth_routes.py", "_state_free_auth_page"),           # sign-in (busy, errors)
    ("api/auth_config_routes.py", "_oidc_verification_failure_page"),  # OIDC verification complete
    ("auth/middleware.py", "_setup_from_internet_refused"),    # setup from the internet refused
}


def test_only_the_listed_pages_set_their_own_policy() -> None:
    found = set()
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND)
        if relative.parts[0] in {"tests", ".venv", "venv"}:
            continue
        source = path.read_text(encoding="utf-8")
        if "Content-Security-Policy" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(item, ast.Constant) and item.value == "Content-Security-Policy"
                       for item in ast.walk(node)):
                    found.add((relative.as_posix(), node.name))
    assert found == OWN_POLICY_PAGES


@pytest.mark.asyncio
async def test_the_listed_pages_keep_their_policies_through_the_middleware() -> None:
    from api.auth_config_routes import _oidc_verification_failure_page
    from api.auth_routes import _state_free_auth_page
    from auth.middleware import _setup_from_internet_refused

    pages = {
        "/login": await _state_free_auth_page(message="login.error.busy", status_code=429, retry_after=60),
        "/api/auth/oidc/callback": _oidc_verification_failure_page(status_code=400),
        "/": _setup_from_internet_refused(_request("/")),
    }
    for path, page in pages.items():
        own = page.headers["Content-Security-Policy"]
        assert own != APPLICATION_CONTENT_SECURITY_POLICY
        assert "frame-ancestors 'none'" in own and "base-uri 'none'" in own

        async def call_next(_request, page=page):
            return page

        response = await request_id_middleware(_request(path), call_next)
        assert response.headers.getlist("Content-Security-Policy") == [own], path
    refusal = pages["/"].headers["Content-Security-Policy"]
    assert refusal == "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"


def _function(tree: ast.Module, name: str) -> ast.AST:
    return next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def _is_json_dumps(node: ast.AST) -> bool:
    # json.dumps(...), or json.dumps(...).replace(...) (escaping "<").
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
        node = node.func.value
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dumps"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "json")


def _is_static_asset(node: ast.AST) -> bool:
    # _static_asset("name.js").read_text(...): the interface's own file.
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_text"
            and isinstance(node.func.value, ast.Call) and getattr(node.func.value.func, "id", "") == "_static_asset")


@pytest.mark.parametrize("name", ["application_javascript_bundle", "theme_bootstrap_script"])
def test_server_built_scripts_embed_only_json_values(name: str) -> None:
    tree = ast.parse((BACKEND / "api" / "auth_routes.py").read_text(encoding="utf-8"))
    function = _function(tree, name)
    bound = {target.id: node.value for node in ast.walk(function) if isinstance(node, ast.Assign)
             for target in node.targets if isinstance(target, ast.Name)}
    values = [value.value for node in ast.walk(function) if isinstance(node, ast.JoinedStr)
              for value in node.values if isinstance(value, ast.FormattedValue)]
    assert values
    for value in values:
        if isinstance(value, ast.IfExp):
            assert isinstance(value.body, ast.Constant) and isinstance(value.orelse, ast.Constant)
            continue
        assert isinstance(value, ast.Name), ast.unparse(value)
        assert _is_json_dumps(bound[value.id]) or _is_static_asset(bound[value.id]), ast.unparse(bound[value.id])


def test_the_locale_catalog_script_embeds_only_json_values() -> None:
    tree = ast.parse((BACKEND / "core" / "localization.py").read_text(encoding="utf-8"))
    function = _function(tree, "catalog_script")
    parts = [node.value for node in ast.walk(function) if isinstance(node, ast.Assign)
             and any(getattr(target, "id", "") == "parts" for target in node.targets)]
    assert len(parts) == 1 and isinstance(parts[0], ast.List)
    pieces = list(parts[0].elts) + [node.args[0] for node in ast.walk(function) if isinstance(node, ast.Call)
                                    and isinstance(node.func, ast.Attribute) and node.func.attr == "append"]
    assert pieces and all(_is_json_dumps(piece) for piece in pieces)
    formatted = [value.value for node in ast.walk(function) if isinstance(node, ast.JoinedStr)
                 for value in node.values if isinstance(value, ast.FormattedValue)]
    assert [ast.unparse(value) for value in formatted] == ["','.join(parts)"]


def test_main_wires_the_policy_in_the_outermost_middleware() -> None:
    source = (BACKEND / "main.py").read_text(encoding="utf-8")
    middleware = source[source.index("async def request_id_middleware"):]
    middleware = middleware[:middleware.index("\n    return response")]
    assert 'response.headers.setdefault(\n        "Content-Security-Policy",' in middleware
    assert main.app.docs_url in DOCUMENTATION_PAGE_PATHS and main.app.redoc_url in DOCUMENTATION_PAGE_PATHS
