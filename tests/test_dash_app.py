"""
notch_dash's HTTP edge: read-only in every sense, the security headers on every response,
the Host allow-list, and the page's files served from an allow-list.
"""

import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from notch_dash import app as dash_app
from notch_dash import record
from notch_dash.settings import Settings


def client(**settings):
    def refuse(request):
        raise httpx.ConnectError("refused")

    return TestClient(dash_app.create_app(
        Settings(**({"allowed_hosts": ("testserver",)} | settings)),
        http=httpx.Client(transport=httpx.MockTransport(refuse)), start=False))


def test_every_response_is_uncached_unreferred_and_under_a_strict_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(dash_app, "STATIC", tmp_path)
    web = client()
    for response in (web.get("/"), web.get("api/snapshot"), web.get("/static/app.js"), web.get("/docs"),
                     web.post("api/snapshot"), web.get("/", headers={"Host": "evil.example"})):
        headers = response.headers
        assert (headers["cache-control"], headers["referrer-policy"], headers["x-content-type-options"]) == (
            "no-store", "no-referrer", "nosniff")
        policy = [d.strip() for d in headers["content-security-policy"].split(";")]
        assert "default-src 'none'" in policy and "script-src 'self'" in policy
        assert "unsafe" not in headers["content-security-policy"]


def test_only_gets_from_an_allowed_host_are_answered():
    web = client(allowed_hosts=("dash.notch.localhost", "127.0.0.1:4130"))
    assert web.get("api/snapshot", headers={"Host": "dash.notch.localhost"}).status_code == 200
    assert web.head("api/snapshot", headers={"Host": "127.0.0.1:4130"}).status_code == 200
    assert web.get("api/snapshot", headers={"Host": "rebound.attacker.example"}).status_code == 421
    assert web.post("api/snapshot", headers={"Host": "dash.notch.localhost"}).status_code == 405
    assert web.get("/openapi.json", headers={"Host": "dash.notch.localhost"}).status_code == 404


@pytest.mark.parametrize("forwarded_for, status", [
    (None, 200),  # straight to 127.0.0.1:4130
    ("127.0.0.1", 200),  # through Caddy from this Mac
    ("192.168.1.20", 403),  # through Caddy's *:80 from another device on the network
    ("127.0.0.1, 192.168.1.20", 403),  # a forged loopback ahead of the address Caddy saw
    ("not-an-address", 403),
], ids=["direct", "caddy-local", "caddy-lan", "forged", "unreadable"])
def test_only_this_mac_is_answered_even_through_caddy(forwarded_for, status):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    assert client().get("api/snapshot", headers=headers).status_code == status


def test_the_page_is_served_from_an_allow_list_and_tolerates_not_being_built(tmp_path, monkeypatch):
    monkeypatch.setattr(dash_app, "STATIC", tmp_path)
    web = client()
    assert web.get("/").status_code == 404 and web.get("/static/app.js").status_code == 404

    (tmp_path / "index.html").write_text("<!doctype html><title>Notch</title>")
    (tmp_path / "app.js").write_text("poll();")
    (tmp_path / "secret.txt").write_text("not for the page")
    assert web.get("/").headers["content-type"].startswith("text/html")
    assert web.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert web.get("/static/secret.txt").status_code == 404
    assert web.get("/static/..%2F..%2Fsettings.py").status_code == 404


def test_the_record_is_opened_read_only(db_path):
    conn = record.connect_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO projects (id, user_id, name) VALUES ('P1', 'u', 'x')")
    finally:
        conn.close()


def test_a_snapshot_counts_as_someone_watching():
    web = client()
    sources = web.app.state.sources
    assert not sources.watching()
    web.get("api/snapshot")
    assert sources.watching()
