"""DP 1.0.3 WS25: the upload limits are exact decimal sizes, as every size is
shown (1 GB, 16 MB, 1 MB): exactly the limit is admitted, one byte more is
refused."""
from __future__ import annotations

import asyncio

import pytest
from starlette.responses import Response

import main
from providers.multimeta import metalink
from providers.multimeta.metalink import InvalidDescriptor, MAX_DESCRIPTOR_BYTES
from test_file_selection_api import api  # noqa: F401  (shared fixture)
from transfers.requests import MAX_LINK_FILE_BYTES, link_file_entries
from transfers.staged_input import MAX_STAGED_INPUT_BYTES, StagedInputError, StagedInputStore


def exactly(head: bytes, size: int, filler: bytes, tail: bytes = b"") -> bytes:
    body = head + filler * ((size - len(head) - len(tail)) // len(filler))
    body += b" " * (size - len(body) - len(tail)) + tail
    assert len(body) == size
    return body


def test_the_limits_are_decimal():
    assert MAX_STAGED_INPUT_BYTES == 1_000_000_000
    assert MAX_LINK_FILE_BYTES == 1_000_000
    assert MAX_DESCRIPTOR_BYTES == 16_000_000
    assert StagedInputStore("/nonexistent").max_bytes == MAX_STAGED_INPUT_BYTES


def test_the_metalink_ceiling_has_one_owner_in_core():
    """1.0.8 WS-A9: core owns the Metalink ceiling beside the torrent's; the
    Multimeta reader and the External API's name for it are that one value."""
    import external.intake as intake
    from transfers.requests import MAX_METALINK_BYTES, MAX_TORRENT_BYTES
    assert MAX_METALINK_BYTES == MAX_TORRENT_BYTES == 16_000_000
    assert MAX_DESCRIPTOR_BYTES == intake.MAX_METALINK_BYTES == MAX_METALINK_BYTES


def test_a_link_file_of_exactly_1_mb_is_read_and_one_byte_more_is_refused():
    data = exactly(b"https://a.example/release.bin\n", MAX_LINK_FILE_BYTES, b"#" * 99 + b"\n")
    assert len(link_file_entries(data)) == 1
    with pytest.raises(ValueError) as refused:
        link_file_entries(data + b"\n")
    assert refused.value.display_code == "linkfile.too_large"


def test_a_metalink_of_exactly_16_mb_is_read_and_one_byte_more_is_refused():
    head = (f'<?xml version="1.0" encoding="UTF-8"?>\n<metalink xmlns="{metalink.NAMESPACE}">'
            '<file name="a.bin"><url>https://a.example/a.bin</url></file>').encode()
    data = exactly(head, MAX_DESCRIPTOR_BYTES, b"<!--" + b"x" * 993 + b"-->", b"</metalink>")
    assert len(metalink.parse(data)) == 1
    with pytest.raises(InvalidDescriptor) as refused:
        metalink.parse(data + b" ")
    assert refused.value.reason == "too_large"


@pytest.mark.asyncio
async def test_a_torrent_of_exactly_16_mb_is_admitted_and_one_byte_more_is_refused(api, monkeypatch):
    submitted = []

    async def submit_torrent(data, filename, *, source="manual_file", selection_mode="all", demand=None):
        submitted.append(len(data))
        return {"id": 2, "status": "pending"}

    monkeypatch.setattr(api.application, "submit_torrent", submit_torrent)
    data = b"d" + b"0" * (16_000_000 - 2) + b"e"
    within = await api.client.post("/api/torrents/add-file", files={"file": ("t.torrent", data, "application/x-bittorrent")})
    assert within.status_code == 200 and submitted == [16_000_000]
    beyond = await api.client.post("/api/torrents/add-file", files={"file": ("t.torrent", data + b"0", "application/x-bittorrent")})
    assert beyond.status_code == 413 and submitted == [16_000_000]


def test_staged_input_admits_exactly_its_ceiling_and_refuses_one_byte_more(tmp_path):
    # The same writer at a small ceiling: a 1 GB write is not a unit test.
    store = StagedInputStore(str(tmp_path), max_bytes=1000)
    assert store.stage_bytes(b"x" * 1000).byte_length == 1000
    with pytest.raises(StagedInputError):
        store.stage_bytes(b"x" * 1001)


@pytest.mark.parametrize("framing, admitted", [(64 * 1024, True), (64 * 1024 + 1, False)])
def test_the_staged_upload_body_holds_a_1_gb_posting_and_its_framing(framing, admitted):
    sent = []
    reached = []

    async def downstream(scope, receive, send):
        reached.append(True)
        await Response(status_code=204)(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    length = str(MAX_STAGED_INPUT_BYTES + framing).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": main.STAGED_UPLOAD_PATH, "raw_path": main.STAGED_UPLOAD_PATH.encode(),
             "query_string": b"", "headers": [(b"content-length", length)], "client": ("127.0.0.1", 1)}
    asyncio.run(main.RequestBodyLimitMiddleware(downstream, max_bytes=1024 * 1024)(scope, receive, send))
    assert bool(reached) is admitted
    assert sent[0]["status"] == (204 if admitted else 413)
