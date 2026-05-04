from __future__ import annotations

import socket

from services import url_guard


def _no_debug(monkeypatch):
    monkeypatch.delenv("LECTIO_DEBUG", raising=False)
    # Also stub the env-read in case .env or shell sets it
    monkeypatch.setattr(url_guard, "_debug_bypass_enabled", lambda: False)


def _patch_resolve(monkeypatch, ips: list[str]):
    """Make socket.getaddrinfo return the given IP literals for any host."""
    def fake_getaddrinfo(host, _port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)


def test_blocks_private_ip_literal_in_url(monkeypatch):
    _no_debug(monkeypatch)
    assert not url_guard.is_safe_outbound_url("http://192.168.1.10/og.html")
    assert not url_guard.is_safe_outbound_url("http://10.0.0.5/")
    assert not url_guard.is_safe_outbound_url("http://172.16.0.1/")


def test_blocks_loopback_and_link_local(monkeypatch):
    _no_debug(monkeypatch)
    assert not url_guard.is_safe_outbound_url("http://127.0.0.1/")
    assert not url_guard.is_safe_outbound_url("http://169.254.169.254/")  # EC2 metadata
    assert not url_guard.is_safe_outbound_url("http://[::1]/")


def test_blocks_hostname_resolving_to_private_ip(monkeypatch):
    _no_debug(monkeypatch)
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    assert not url_guard.is_safe_outbound_url("https://internal.example.org/")


def test_blocks_when_any_resolved_ip_is_private_dns_rebinding(monkeypatch):
    """DNS rebinding mitigation: if a hostname resolves to multiple IPs and
    any one is internal, refuse the whole URL even if others are public."""
    _no_debug(monkeypatch)
    _patch_resolve(monkeypatch, ["8.8.8.8", "192.168.0.1"])
    assert not url_guard.is_safe_outbound_url("https://attacker.example.com/")


def test_allows_public_ip(monkeypatch):
    _no_debug(monkeypatch)
    _patch_resolve(monkeypatch, ["93.184.216.34"])  # example.com
    assert url_guard.is_safe_outbound_url("https://example.com/article")


def test_blocks_non_http_schemes(monkeypatch):
    _no_debug(monkeypatch)
    assert not url_guard.is_safe_outbound_url("file:///etc/passwd")
    assert not url_guard.is_safe_outbound_url("gopher://example.com/")
    assert not url_guard.is_safe_outbound_url("")


def test_blocks_when_dns_lookup_fails(monkeypatch):
    _no_debug(monkeypatch)

    def boom(*_a, **_k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", boom)
    assert not url_guard.is_safe_outbound_url("https://nonexistent.invalid/")


def test_debug_mode_bypasses_all_checks(monkeypatch):
    monkeypatch.setenv("LECTIO_DEBUG", "1")
    # Even a clearly-private URL must pass when LECTIO_DEBUG=1.
    assert url_guard.is_safe_outbound_url("http://192.168.1.10/")
    assert url_guard.is_safe_outbound_url("http://127.0.0.1:8000/")
