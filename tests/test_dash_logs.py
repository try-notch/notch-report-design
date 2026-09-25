"""
notch_dash's readers: redaction, the Caddy / server / tunnel log lines, and the tail that
follows a file through appends, rotation and truncation.
"""

import json
import os
from collections import namedtuple
from datetime import datetime

import pytest

from notch_dash import logs
from notch_dash.live import Poller, Tail, Unreadable

SECRET = "3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d"  # shaped like the gate secret
UUID = "3b9d2c1e-5f7a-4e8b-9c0d-1a2b3c4d5e6f"


def caddy(uri, *, host="notch-gate.localhost", status=200, via=True, ua="Notch/1 CFNetwork/3896 Darwin/27.0.0",
          ts=1790368684.76, method="GET", country="US", **headers):
    """One Caddy access-log line, shaped like the real ones."""
    return json.dumps({
        "ts": ts, "status": status, "duration": 0.0042,
        "request": {"host": host, "method": method, "uri": uri, "headers": {
            "User-Agent": [ua], "Cf-Ipcountry": [country], "Cf-Connecting-Ip": ["203.0.113.7"],
            "X-Forwarded-Host": ["a-b-c.trycloudflare.com"], **{k: [v] for k, v in headers.items()}}},
        "resp_headers": {"Via": ["1.1 Caddy"]} if via else {"Server": ["Caddy"]}})


def local(stamp):
    """A server-log timestamp read as this machine's local time, like the parser does."""
    day, ms = stamp.split(",")
    return datetime.strptime(day, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms) / 1000


def test_redact_hides_the_secret_keys_and_bearer_tokens_and_bounds_the_length():
    text = f"GET /{SECRET}/v1/me /{SECRET.upper()}/x Authorization: Bearer eyJhbGciOi.abc key=sk-or-v1-{SECRET}"
    out = logs.redact(text)
    assert SECRET not in out.lower() and "eyJhbGciOi" not in out and "sk-or-" not in out
    assert out.count("<gate>") == 2
    assert len(logs.redact("x" * 1000, limit=200)) <= 200


def test_redact_hides_a_secret_with_escaped_characters_which_caddy_unescapes_and_lets_through():
    escaped = "%33%66" + SECRET[2] + "%41" + SECRET[4:]  # 3, f and an upper-case A, each escaped
    assert logs.redact(f"GET /{escaped}/healthz") == "GET /<gate>/healthz"


@pytest.mark.parametrize("line, kind", [
    (caddy("/<gate>/v1/me"), "passed"),
    (caddy("/<gate>/v1/me", status=404, via=False), "blocked"),  # a wrong secret, which Caddy also logs as /<gate>/
    (caddy("/<gate>/v1/me", status=502, via=False), "passed"),  # proxied, and the server wasn't there
    (caddy("/wp-login.php", status=404, via=False, ua="Mozilla/5.0 zgrab/0.x"), "blocked"),
    (caddy("/healthz", host="api.notch.localhost"), "local"),
    (caddy("/<gate>/healthz", ua=logs.OWN_UA), None),  # the dashboard's own probe
    (caddy("/docs", status=404, via=False, ua="notch-dash/1"), "blocked"),  # anyone can send that user agent
    (caddy("/healthz", ua=logs.OWN_UA), "passed"),  # through without the secret: a leak, whoever sent it
    (caddy("/api/snapshot", host="dash.notch.localhost"), None),
    ("not json", None),
], ids=["passed", "wrong-secret", "proxied-502", "probe", "local", "own-probe", "forged-own-probe", "own-probe-leaked",
        "other-host", "junk"])
def test_caddy_lines_are_sorted_into_passed_blocked_and_local(line, kind):
    req = logs.parse_caddy(line)
    assert (req and req.kind) == kind


def test_routes_group_ids_and_drop_the_gate_and_the_query():
    route = logs.route
    assert route("GET", f"/<gate>/v1/entries/{UUID.upper()}?fields=all") == "GET /v1/entries/{id}"
    assert route("GET", f"/v1/jobs/{UUID}") == "GET /v1/jobs/{id}"
    assert route("DELETE", "/v1/projects/12345") == "DELETE /v1/projects/{id}"
    assert route("GET", "/v1/reports/0123456789abcdef") == "GET /v1/reports/{id}"
    assert route("GET", "/v1/entries?limit=100") == "GET /v1/entries"
    assert route("GET", "/v1/deadbeef") == "GET /v1/deadbeef"  # short hex is a word, not an id


def test_a_raw_secret_in_an_old_caddy_line_is_redacted_and_the_referer_is_never_kept():
    req = logs.parse_caddy(caddy(f"/{SECRET}/v1/me", Referer=f"https://x.trycloudflare.com/{SECRET}/"))
    assert req.kind == "passed" and req.path == "/<gate>/v1/me" and req.route == "GET /v1/me"
    assert SECRET not in repr(req)


SERVER_LOG = f"""\
INFO:     Started server process [22119]
2026-09-25 15:42:51,268 INFO notch_api.app: resumed 0 unfinished job(s)
INFO:     127.0.0.1:0 - "GET /healthz HTTP/1.1" 200 OK
2026-09-25 16:34:00,832 INFO httpx: HTTP Request: POST https://openrouter.ai/api/v1/audio/transcriptions "HTTP/1.1 200 OK"
2026-09-25 16:34:01,015 INFO httpx: HTTP Request: POST https://openrouter.ai/api/alpha/decisions "HTTP/1.1 502 Bad Gateway"
2026-09-25 16:35:00,000 WARNING notch_api.analysis: capture job {UUID} failed: model_unavailable (after 5 attempts)
2026-09-25 16:36:00,000 WARNING notch_api.analysis: capture job f8af4772-addd-4e68-bffe-f28d946e3dee failed: model_unavailable (after 3 attempts)
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "notch_api/app.py", line 152, in db
    yield conn
sqlite3.OperationalError: database is locked
INFO:     127.0.0.1:0 - "GET /v1/me HTTP/1.1" 500 Internal Server Error
2026-09-25 16:37:00,000 ERROR notch_api.worker: job {UUID} could not record its outcome
Traceback (most recent call last):
  File "notch_api/worker.py", line 81, in run
    job(self.db_path, job_id, **kwargs)
sqlite3.OperationalError: database is locked
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "notch_api/app.py", line 160, in db
    yield conn
sqlite3.OperationalError: database is locked
"""


def _feed(parser, text, now=0.0):
    return [item for line in text.splitlines() if (item := parser.feed(line, now)) is not None]


def test_the_server_log_gives_model_calls_the_last_start_and_errors_grouped_with_their_tracebacks():
    parser = logs.ServerLog()
    items = _feed(parser, SERVER_LOG)

    calls = [i for i in items if isinstance(i, logs.Call)]
    assert [(c.kind, c.status, c.ok, c.at) for c in calls] == [
        ("stt", 200, True, local("2026-09-25 16:34:00,832")),
        ("classify", 502, False, local("2026-09-25 16:34:01,015"))]
    assert parser.started_at == local("2026-09-25 15:42:51,268")

    groups = logs.group_errors([i for i in items if isinstance(i, logs.Err)])
    by_where = {(g["level"], g["where"]): g for g in groups}
    assert len(groups) == 3
    asgi = by_where["ERROR", "uvicorn.error"]
    assert asgi["count"] == 2 and asgi["exception"] == "sqlite3.OperationalError: database is locked"
    # uvicorn's lines carry no time: each takes the last timestamped line's.
    assert (asgi["first_at"], asgi["last_at"]) == (local("2026-09-25 16:36:00,000"), local("2026-09-25 16:37:00,000"))
    assert asgi["sample"].splitlines()[-1] == asgi["exception"] and "line 160" in asgi["sample"]
    assert "GET /v1/me" not in asgi["sample"]  # the access line after a traceback ends it
    assert by_where["WARNING", "notch_api.analysis"]["count"] == 2  # ids and numbers don't split a group
    assert by_where["WARNING", "notch_api.analysis"]["exception"] is None
    assert groups[0]["last_at"] >= groups[1]["last_at"] >= groups[2]["last_at"]


def test_the_tunnel_log_gives_the_public_host_and_its_warnings_without_their_fields():
    parser = logs.TunnelLog()
    items = _feed(parser, """\
2026-09-25T20:31:24Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-09-25T20:31:28Z INF |  https://absolutely-innovations-candles-staff.trycloudflare.com                            |
2026-09-25T20:32:40Z WRN Failed to refresh DNS local resolver error="lookup region1.v2.argotunnel.com: no such host"
2026-09-25T20:33:40Z WRN Failed to refresh DNS local resolver error="lookup region2.v2.argotunnel.com: no such host"
2026-09-25T20:34:00Z ERR Failed to request quick Tunnel error="Post \\"https://api.trycloudflare.com/tunnel\\": EOF"
""")
    assert parser.host == "absolutely-innovations-candles-staff.trycloudflare.com"
    groups = logs.group_errors(items)
    assert [(g["level"], g["message"], g["count"]) for g in groups] == [
        ("ERR", "Failed to request quick Tunnel", 1), ("WRN", "Failed to refresh DNS local resolver", 2)]
    assert groups[1]["first_at"] == datetime.fromisoformat("2026-09-25T20:32:40Z").timestamp()


Line = namedtuple("Line", "at text")


def test_tail_follows_appends_rotation_and_truncation_and_keeps_a_day(tmp_path):
    path, now = tmp_path / "access.log", [1000.0]
    path.write_text("a1\na2\npart")
    tail = Tail(str(path), lambda text, at: Line(at, text), interval=2, clock=lambda: now[0])
    texts = lambda: [line.text for line in tail.items()]

    tail.tick()
    assert texts() == ["a1", "a2"]  # a line still being written waits for its newline
    with open(path, "a") as f:
        f.write("ial\na3\n")
    tail.tick()
    assert texts() == ["a1", "a2", "partial", "a3"]

    path.write_text("b1\n")  # truncated in place
    tail.tick()
    assert texts()[-1] == "b1"

    os.rename(path, tmp_path / "access.log.1")  # rolled: a new file takes the name
    path.write_text("c1\n")
    tail.tick()
    assert texts()[-2:] == ["b1", "c1"]

    now[0] += 86400 + 1
    with open(path, "a") as f:
        f.write("d1\n")
    tail.tick()
    assert texts() == ["d1"]

    path.unlink()
    tail.tick()
    assert tail.error and texts() == ["d1"]


def test_a_poller_keeps_its_last_good_value_until_it_is_too_old():
    now, answers = [0.0], iter([{"n": 1}, Unreadable("couldn’t connect")])

    def probe():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    poller = Poller(probe, interval=5, max_age=30, clock=lambda: now[0])
    poller.refresh()
    now[0] = 20.0
    poller.refresh()
    assert (poller.current(now[0]), poller.read_at, poller.error) == ({"n": 1}, 0.0, "couldn’t connect")
    assert poller.current(31.0) is None
