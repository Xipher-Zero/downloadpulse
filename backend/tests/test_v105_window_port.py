"""The desktop window keeps its address across launches while its port is free,
so the browser cache of the page's scripts and styles (core.static_assets)
survives a relaunch; a taken or unreadable record is a free port."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.supervisor import WINDOW_PORT_FILE, window_port  # noqa: E402


def test_the_window_reuses_its_port_while_it_is_free(tmp_path):
    runtime = tmp_path / "desktop-runtime"
    first = window_port(runtime)
    assert (runtime / WINDOW_PORT_FILE).read_text(encoding="ascii").strip() == str(first)
    assert window_port(runtime) == first


def test_a_taken_port_moves_the_window_and_is_remembered(tmp_path):
    runtime = tmp_path / "desktop-runtime"
    first = window_port(runtime)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", first))
        holder.listen(1)
        moved = window_port(runtime)
    assert moved != first
    assert (runtime / WINDOW_PORT_FILE).read_text(encoding="ascii").strip() == str(moved)


def test_an_unreadable_or_out_of_range_record_is_a_free_port(tmp_path):
    runtime = tmp_path / "desktop-runtime"
    runtime.mkdir()
    for record in ("not a port", "80", "70000", ""):
        (runtime / WINDOW_PORT_FILE).write_text(record, encoding="ascii")
        port = window_port(runtime)
        assert 1024 <= port <= 65535 and (runtime / WINDOW_PORT_FILE).read_text(encoding="ascii").strip() == str(port)
