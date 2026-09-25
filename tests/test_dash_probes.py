"""
notch_dash's probes: what each asks, what it keeps, and how it words a failure. Every
failure is worded here, never with an exception's own text: a probe URL holds the secret.
"""

import json
import subprocess

import httpx
import pytest

from notch_dash import probes
from notch_dash.live import Unreadable
from notch_dash.logs import OWN_UA

SECRET = "3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d"
HOST = "absolutely-innovations-candles-staff.trycloudflare.com"
METRICS = """\
# HELP build_info Build and version information
# TYPE build_info gauge
build_info{goversion="go1.27.1",revision="2026-09-24T15:31:10Z",type="",version="2026.9.3"} 1
cloudflared_tunnel_concurrent_requests_per_tunnel 0
cloudflared_tunnel_ha_connections 1
cloudflared_tunnel_request_errors 2
cloudflared_tunnel_response_by_code{status_code="200"} 46
cloudflared_tunnel_server_locations{connection_id="0",edge_location="ewr14"} 1
cloudflared_tunnel_total_requests 53
cloudflared_tunnel_user_hostnames_counts{userHostname="https://%s"} 1
go_gc_duration_seconds{quantile="0.5"} 5.2e-05
quic_client_latest_rtt{conn_index="0"} 22
quic_client_smoothed_rtt{conn_index="0"} 18
""" % HOST


def http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cloudflared_reads_ready_and_only_the_metrics_the_tunnel_check_shows():
    def handler(request):
        if request.url.path == "/ready":
            return httpx.Response(200, json={"status": 200, "readyConnections": 1, "connectorId": "72020cb3"})
        return httpx.Response(200, text=METRICS)

    assert probes.cloudflared(http(handler), "127.0.0.1:20241") == {
        "ready_connections": 1, "edge": "ewr14", "rtt_ms": 18.0, "version": "2026.9.3",
        "requests_total": 53, "request_errors": 2, "host": HOST}


def test_a_tunnel_with_no_connection_reads_as_zero_not_as_a_failure():
    def handler(request):
        if request.url.path == "/ready":
            return httpx.Response(503, json={"status": 503, "readyConnections": 0})
        return httpx.Response(200, text="cloudflared_tunnel_ha_connections 0\n")

    assert probes.cloudflared(http(handler), "127.0.0.1:20241")["ready_connections"] == 0


def test_the_public_probe_sends_the_secret_only_in_its_request(tmp_path):
    secret_file = tmp_path / "gate-secret"
    secret_file.write_text(SECRET + "\n")
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    result = probes.end_to_end(http(answer), HOST, str(secret_file))
    assert result["ok"] and result["http_status"] == 200 and result["error"] is None
    assert str(seen[0].url) == f"https://{HOST}/{SECRET}/healthz"
    assert seen[0].headers["User-Agent"] == OWN_UA  # the user agent the Caddy reader leaves out
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("failure, status", [
    (httpx.ConnectError(f"connect to https://{HOST}/{SECRET}/healthz failed"), None),
    (httpx.ReadTimeout(f"timed out reading https://{HOST}/{SECRET}/healthz"), None),
    (httpx.Response(502, text=f"bad gateway for /{SECRET}/healthz"), 502),
], ids=["refused", "timeout", "bad-gateway"])
def test_a_failed_public_probe_is_worded_without_the_exception_text(tmp_path, failure, status):
    secret_file = tmp_path / "gate-secret"
    secret_file.write_text(SECRET)

    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return failure

    result = probes.end_to_end(http(handler), HOST, str(secret_file))
    assert (result["ok"], result["http_status"]) == (False, status) and result["error"]
    assert SECRET not in json.dumps(result) and HOST not in json.dumps(result)


def test_a_secret_file_that_is_not_48_hex_is_refused_before_anything_is_sent(tmp_path):
    secret_file = tmp_path / "gate-secret"
    secret_file.write_text(SECRET[:-1] + "Z")

    def handler(request):
        raise AssertionError("sent a request")

    with pytest.raises(Unreadable) as exc:
        probes.end_to_end(http(handler), HOST, str(secret_file))
    assert SECRET[:40] not in str(exc.value)


def test_gate_integrity_reports_an_open_gate():
    def handler(request):
        return httpx.Response(200 if request.url.path == "/docs" else 404)

    assert probes.gate_integrity(http(handler), HOST) == {"healthz": 404, "docs": 200, "open": True, "error": None}


def test_openrouter_spend_keeps_only_the_numbers():
    data = {"label": "sk-or-v1-abc...xyz", "creator_user_id": "user_2abc", "workspace_id": "ws_9",
            "limit": 50, "limit_remaining": 49.76, "usage": 0.2435, "usage_daily": 0.0444, "usage_weekly": 0.2435,
            "usage_monthly": 0.2435, "is_free_tier": False}
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": data})

    spend = probes.openrouter_spend(http(handler), "sk-or-v1-real")
    assert seen[0].headers["Authorization"] == "Bearer sk-or-v1-real"
    assert spend == {"limit_usd": 50.0, "remaining_usd": 49.76, "today_usd": 0.0444, "week_usd": 0.2435,
                     "month_usd": 0.2435, "total_usd": 0.2435, "free_tier": False}


DEVICES = {"result": {"devices": [
    {"properties": {"hardware": {"udid": "OTHER", "marketingName": "iPad"}}},
    {"properties": {
        "hardware": {"udid": "00008150-000261540203401C", "marketingName": "iPhone 17 Pro", "serialNumber": "SERIAL",
                     "ecid": 123456},
        "state": {"name": "Chetan’s iPhone", "bootState": "booted"},
        "software": {"osVersionNumber": {"stringValue": "27.0", "components": [27, 0]}},
        "connection": {"lastConnectionDate": 812061180, "pairingState": "paired", "state": "disconnected",
                       "transportType": "localNetwork", "potentialHostnames": ["x.coredevice.local"]}}},
]}}


def fake_devicectl(document):
    def run(args, **kwargs):
        assert kwargs["timeout"]
        with open(args[args.index("--json-output") + 1], "w") as f:
            json.dump(document, f)
        return subprocess.CompletedProcess(args, 0)
    return run


def test_devicectl_finds_the_phone_by_udid_and_keeps_only_what_the_page_shows():
    phone = probes.device(fake_devicectl(DEVICES), "00008150-000261540203401C")
    assert phone == {"name": "Chetan’s iPhone", "model": "iPhone 17 Pro", "os": "27.0", "connection": "disconnected",
                     "pairing": "paired", "transport": "localNetwork", "last_connected_at": 1790368380.0}
    assert probes.device(fake_devicectl(DEVICES), "NOT-LISTED") is None


def test_a_devicectl_that_hangs_is_a_worded_failure():
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(Unreadable):
        probes.device(run, "00008150-000261540203401C")
