"""Scripts and stylesheets cached for good only when the request proves it asks
for the current bytes (core.static_assets). Expected versions are computed here
with hashlib from the files on disk, never asked of the owner."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

import main
from core.static_assets import IMMUTABLE, VersionedStaticFiles, routed_by
from main import request_id_middleware

REVALIDATE = "no-cache, must-revalidate"


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _site(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    root.mkdir()
    (root / "child.css").write_text("body { color: red; }\n", encoding="utf-8")
    (root / "a.js").write_text("console.log(1);\n", encoding="utf-8")
    (root / "app.js").write_text("// on disk, but a route answers /app.js\n", encoding="utf-8")
    (root / "style.css").write_text("@import url('/child.css?v=3');\n", encoding="utf-8")
    (root / "index.html").write_text(
        '<link rel="stylesheet" href="/style.css?v=53">\n<script src="/a.js?v=7"></script>\n'
        '<script src="/app.js?v=51"></script>\n<script src="/missing.js?v=2"></script>\n', encoding="utf-8")
    return root


def _client(root: Path) -> TestClient:
    app = FastAPI()
    app.middleware("http")(request_id_middleware)

    @app.get("/app.js")
    def built() -> Response:
        return Response("// per request", media_type="text/javascript", headers={"Cache-Control": "no-store"})
    app.mount("/", VersionedStaticFiles(directory=root, html=True, routed=routed_by(app)), name="static")
    return TestClient(app)


def _style_expected(root: Path) -> bytes:
    child = _h((root / "child.css").read_bytes())
    return (root / "style.css").read_bytes().replace(b"/child.css?v=3", f"/child.css?v={child}".encode())


def test_the_shell_names_each_served_file_by_the_hash_of_what_is_served(tmp_path):
    root = _site(tmp_path)
    client = _client(root)
    style = _style_expected(root)
    shell = client.get("/")
    assert shell.status_code == 200 and shell.headers["cache-control"] == REVALIDATE
    assert f"/style.css?v={_h(style)}" in shell.text and f"/a.js?v={_h((root / 'a.js').read_bytes())}" in shell.text
    # A route's address and a file it does not serve keep their hand versions.
    assert "/app.js?v=51" in shell.text and "/missing.js?v=2" in shell.text
    assert client.get("/style.css").content == style


def test_a_changed_child_changes_its_importer_and_the_shell_and_unpins_the_old_url(tmp_path):
    root = _site(tmp_path)
    client = _client(root)
    old_child = _h((root / "child.css").read_bytes())
    old_style = _h(_style_expected(root))
    old_shell = client.get("/")
    assert client.get(f"/child.css?v={old_child}").headers["cache-control"] == IMMUTABLE
    (root / "child.css").write_text("body { color: blue; }\n", encoding="utf-8")
    new_style = _h(_style_expected(root))
    assert new_style != old_style
    shell = client.get("/", headers={"If-None-Match": old_shell.headers["etag"]})
    assert shell.status_code == 200 and f"/style.css?v={new_style}" in shell.text
    # Neither the old child nor the old importer is ever cached for good again.
    assert client.get(f"/child.css?v={old_child}").headers["cache-control"] == REVALIDATE
    assert client.get(f"/style.css?v={old_style}").headers["cache-control"] == REVALIDATE
    assert client.get(f"/style.css?v={new_style}").headers["cache-control"] == IMMUTABLE


def test_only_the_proving_request_is_immutable(tmp_path):
    root = _site(tmp_path)
    client = _client(root)
    current = _h((root / "a.js").read_bytes())
    assert client.get(f"/a.js?v={current}").headers["cache-control"] == IMMUTABLE
    for asked in ("/a.js", "/a.js?v=7", "/a.js?v=0000000000000000"):
        assert client.get(asked).headers["cache-control"] == REVALIDATE, asked
    missing = client.get(f"/missing.js?v={current}")
    assert missing.status_code == 404 and missing.headers.get("cache-control") != IMMUTABLE
    first = client.get(f"/a.js?v={current}")
    again = client.get(f"/a.js?v={current}", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == IMMUTABLE
    head = client.head(f"/a.js?v={current}")
    assert head.headers["cache-control"] == IMMUTABLE and head.headers["etag"] == first.headers["etag"]
    # The route's own policy is untouched.
    assert client.get("/app.js?v=51").headers["cache-control"] == "no-store"


def test_a_rewritten_body_has_its_own_validators(tmp_path):
    root = _site(tmp_path)
    client = _client(root)
    body = _style_expected(root)
    response = client.get("/style.css")
    assert response.headers["etag"] == f'"{_h(body)}"' and int(response.headers["content-length"]) == len(body)
    assert "last-modified" not in response.headers and "accept-ranges" not in response.headers
    assert client.get("/style.css", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    # The file's own date never answers for it, and a range is answered whole.
    dated = client.get("/style.css", headers={"If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"})
    assert dated.status_code == 200 and dated.content == body
    ranged = client.get("/style.css", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 200 and ranged.content == body
    head = client.head("/style.css")
    assert head.headers["etag"] == response.headers["etag"]
    assert head.headers["content-length"] == response.headers["content-length"]


def test_the_rewritten_files_answer_only_get_and_head(tmp_path):
    client = _client(_site(tmp_path))
    for method, path in (("POST", "/"), ("PUT", "/index.html"), ("DELETE", "/style.css"), ("POST", "/a.js")):
        assert client.request(method, path).status_code == 405, (method, path)


def test_the_application_shell_versions_every_reference_it_serves():
    """The real shell and stylesheet, served by the application's own static
    owner: every local script/stylesheet reference carries the hash of the
    bytes served for it, or is answered by a route."""
    root = Path(main._static)
    served = FastAPI()
    served.mount("/", main._static_files)
    client = TestClient(served)
    reference = re.compile(r"(/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:js|css))\?v=([A-Za-z0-9_.-]*)")
    routed = routed_by(main.app)
    style = (root / "style.css").read_bytes()
    for path, _v in reference.findall(style.decode()):
        style = style.replace(f"{path}?v={_v}".encode(), f"{path}?v={_h((root / path.lstrip('/')).read_bytes())}".encode(), 1)
    assert client.get("/style.css").content == style
    shell = client.get("/").text
    found = reference.findall(shell)
    assert len(found) >= 40
    for path, version in found:
        if routed(path):
            continue
        expected = style if path == "/style.css" else (root / path.lstrip("/")).read_bytes()
        assert version == _h(expected), path


@pytest.mark.parametrize("declared", ["no-store", IMMUTABLE])
def test_a_declared_policy_wins_and_the_default_is_revalidation(declared):
    app = FastAPI()
    app.middleware("http")(request_id_middleware)

    @app.get("/x.js")
    def declared_policy() -> Response:
        return Response("x", headers={"Cache-Control": declared})

    @app.get("/y.js")
    def default_policy() -> Response:
        return Response("y")
    client = TestClient(app)
    assert client.get("/x.js").headers["cache-control"] == declared
    assert client.get("/y.js").headers["cache-control"] == REVALIDATE
