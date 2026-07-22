"""The test suite must not reach the internet.

A test that does is flaky by construction — it depends on a third party being up
and unchanged. One was: a YouTube feed fetch during a full-suite run returned a
live 404 and failed a test that passed in isolation and on re-run, which is the
worst shape a failure can take. The autouse guard in conftest blocks outbound
sockets; these tests pin it so it can't be quietly removed.
"""
from __future__ import annotations

import socket

import pytest


def test_outbound_connection_is_blocked():
    with pytest.raises(RuntimeError, match="outbound network blocked"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_outbound_connection_via_socket_object_is_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="outbound network blocked"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_the_error_names_the_host():
    """So the offending call is obvious, rather than surfacing as an unrelated
    assertion failure minutes later."""
    with pytest.raises(RuntimeError, match="example.com"):
        socket.create_connection(("example.com", 443), timeout=1)


def test_an_http_client_is_blocked_too():
    """The guard sits at the socket layer specifically so it catches every
    client rather than trusting each test to mock its own."""
    import httpx

    with pytest.raises(Exception) as exc:
        httpx.get("https://example.com", timeout=1)
    assert "outbound network blocked" in str(exc.value) or isinstance(exc.value, httpx.ConnectError)


def test_loopback_is_still_allowed():
    """Anything genuinely local must keep working — the guard is about third
    parties, not about forbidding sockets."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        conn = socket.create_connection(server.getsockname(), timeout=2)
        conn.close()
    finally:
        server.close()
