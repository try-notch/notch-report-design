"""
The api/snapshot document: its shape against tests/dash_sample_snapshot.json, the secret
kept out of it, every source failing softly, the waterfall, and the status rules.
"""

import json
import pathlib
import subprocess
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from notch_api.config import DEV_USER_ID
from notch_dash import snapshot
from notch_dash.app import create_app
from notch_dash.logs import OWN_UA, Req
from notch_dash.settings import Settings
from tests.test_dash_logs import SECRET, caddy
from tests.test_dash_probes import DEVICES, METRICS, fake_devicectl

NOW = 1790368700.0
HOST = "absolutely-innovations-candles-staff.trycloudflare.com"
UDID = "00008150-000261540203401C"
FLIGHT, DONE, FAILED, OLD = ("C7E4A1B9-2F3D-4B8E-9A61-3D5E7F9A1B2C", "5B2F0285-4E38-4DA8-926A-28B4184D7D79",
                             "9C1D7E3A-6B2F-4A8C-B5D4-0E9F8A7B6C5D", "1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D")
SAMPLE = json.loads((pathlib.Path(__file__).parent / "dash_sample_snapshot.json").read_text())

# DASHBOARD.md › Nullability, as paths ("[]" is any list item, "*" any key). The marked
# additions are fields the table leaves out that can be null for real.
NULLABLE = {
    "pipeline", "record", "traffic", "blocked", "errors.server", "errors.tunnel",
    *(f"checks.phone.{k}" for k in ("last_seen_at", "last_route", "requests_1h", "device")),
    *(f"checks.tunnel.{k}" for k in ("ready_connections", "edge", "rtt_ms", "version", "requests_total",
                                     "request_errors", "read_at", "host", "phone_host", "probe")),
    "checks.tunnel.probe.http_status", "checks.tunnel.probe.latency_ms", "checks.tunnel.probe.error",
    *(f"checks.gate.integrity{k}" for k in ("", ".healthz", ".docs", ".error")),
    *(f"checks.gate.{k}" for k in ("blocked_1h", "blocked_24h", "last_blocked_at", "passed_without_secret_10m")),
    *(f"checks.server.{k}" for k in ("at", "http_status", "latency_ms", "error", "started_at")),
    *(f"checks.worker.{k}" for k in ("captures", "reports", "stuck", "done_24h", "failed_24h", "failed_1h",
                                     "oldest_pending_ms")),
    *(f"checks.openrouter.{k}" for k in ("limit_usd", "remaining_usd", "today_usd", "week_usd", "month_usd",
                                         "total_usd", "free_tier", "read_at")),
    *(f"pipeline.rows[].{k}" for k in ("job_id", "finished_at", "failure_code", "note", "words", "mood", "summary",
                                       "audio_on_disk")),
    "pipeline.rows[].phases[].calls", "pipeline.rows[].phases[].failed_calls",
    "models.source", "models.note",
    *(f"models.kinds[].{k}" for k in ("model", "p50_ms", "p95_ms", "cost_usd", "prompt_tokens", "completion_tokens",
                                      "total_tokens")),
    "models.kinds[].last_at",  # addition: a kind with no calls in the window
    *(f"models.recent[].{k}" for k in ("model", "tool", "latency_ms", "attempt", "job", "entry_id", "total_tokens",
                                       "cost_usd")),
    "record.audio.disk_bytes", "record.audio.disk_files", "record.reports.last_generated_at",
    "record.profile.name",  # addition: users.display_name is nullable
    "sources.*.where", "sources.*.read_at", "sources.*.reason",
    # additions: a group with no traceback has no exception, and cloudflared's lines name no logger
    "errors.server[].exception", "errors.tunnel[].exception", "errors.tunnel[].where",
}


def assert_shape(live, sample, path=""):
    """Same keys as the sample, recursively; list items like the sample's first; null only where allowed."""
    if live is None:
        field = path.rsplit(".", 1)[-1]
        assert path in NULLABLE or path.startswith("sources.") and f"sources.*.{field}" in NULLABLE, path
    elif isinstance(sample, dict):
        assert isinstance(live, dict) and set(live) == set(sample), (path, set(live) ^ set(sample))
        for key in sample:
            assert_shape(live[key], sample[key], f"{path}.{key}" if path else key)
    elif isinstance(sample, list):
        assert isinstance(live, list), path
        if path.startswith("traffic.hour.by_class"):
            assert len(live) == 60, path
        for item in live if sample else ():
            assert_shape(item, sample[0], path + "[]")


def iso(ago):
    return None if ago is None else datetime.fromtimestamp(NOW - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(ago):
    """A server-log timestamp: the machine's local time."""
    return datetime.fromtimestamp(NOW - ago).strftime("%Y-%m-%d %H:%M:%S,000")


def refuse(request):
    raise httpx.ConnectError("refused")


def serve(*, handler=refuse, run=None, clock=lambda: NOW, **settings):
    """A TestClient over a dashboard whose sources have each been read once, at NOW."""
    def no_devicectl(args, **kwargs):
        raise FileNotFoundError("xcrun")

    app = create_app(Settings(allowed_hosts=("testserver",), **settings),
                     http=httpx.Client(transport=httpx.MockTransport(handler)), run=run or no_devicectl,
                     clock=clock, start=False)
    for step, _, _ in app.state.sources.jobs:
        step()
    return TestClient(app)


@pytest.fixture
def jobs(conn, capture):
    """add(entry_id, state, submitted, started=None, finished=None) -> job_id; times in seconds before NOW."""
    def add(entry_id, state, submitted, started=None, finished=None):
        code = "model_unavailable" if state == "failed" else None
        job_id = capture(entry_id, b"audio", state="failed" if code else "queued")
        done = state == "complete"
        with conn:
            conn.execute("UPDATE capture_jobs SET state = ?, failure_code = ?, submitted_at = ?, started_at = ?,"
                         " finished_at = ?, attempts = ? WHERE id = ?",
                         (state, code, iso(submitted), iso(started), iso(finished), int(started is not None), job_id))
            conn.execute("UPDATE entries SET analysis_state = ?, raw_text = ?, word_count = ?, summary = ?, mood = ?,"
                         " tags = ? WHERE id = ?",
                         ("pending" if state == "queued" else state, "Ran a quick test." if done else None,
                          4 if done else 0, "You ran a quick test." if done else None, "flat" if done else None,
                          '["testing"]' if done else "[]", entry_id))
        return job_id
    return add


def event(ago, kind, job_id, *, status=200, ms=180.0, job="capture", **usage):
    return json.dumps({"ts": NOW - ago, "kind": kind, "model": f"{kind}-model", "tool": "label_entry" if kind == "chat"
                       else None, "status": status, "ok": status == 200, "latency_ms": ms, "attempt": 1, "job": job,
                       "job_id": job_id, "usage": usage or None})


def handler(request):
    """Every endpoint the dashboard asks, answering as a healthy stack does."""
    url = str(request.url)
    if url == "http://127.0.0.1:4131/healthz" or url == f"https://{HOST}/{SECRET}/healthz":
        return httpx.Response(200, json={"ok": True})
    if url == "http://127.0.0.1:20241/ready":
        return httpx.Response(200, json={"status": 200, "readyConnections": 1})
    if url == "http://127.0.0.1:20241/metrics":
        return httpx.Response(200, text=METRICS)
    if url == "https://openrouter.ai/api/v1/key":
        return httpx.Response(200, json={"data": {"label": "sk-or-v1-abc...xyz", "limit": 50, "limit_remaining": 49.76,
                                                  "usage": 0.2435, "usage_daily": 0.0444, "usage_weekly": 0.2435,
                                                  "usage_monthly": 0.2435, "is_free_tier": False}})
    return httpx.Response(404)


@pytest.fixture
def stack(tmp_path, db_path, audio_dir, conn, jobs):
    """Every source available: a notch in flight, one written, one failed, one from before metrics, and a report."""
    flight, done, failed = jobs(FLIGHT, "analyzing", 10, 10), jobs(DONE, "complete", 260, 260, 258), \
        jobs(FAILED, "failed", 2140, 2140, 2122)
    jobs(OLD, "complete", 5000, 4990, 4980)
    with conn:
        conn.execute("INSERT INTO reports (id, user_id, range_start, range_end, range_label, generated_at)"
                     " VALUES ('R1', ?, '2026-09-01', '2026-09-25', 'September 2026', ?)", (DEV_USER_ID, iso(37)))
        conn.execute("INSERT INTO report_jobs (id, user_id, report_id, state, submitted_at, finished_at)"
                     " VALUES ('RJ1', ?, 'R1', 'complete', ?, ?)", (DEV_USER_ID, iso(40), iso(37)))

    files = {
        "metrics": [event(9.6, "stt", flight, ms=1830.0), event(7.65, "classify", flight, ms=190.0),
                    event(259.35, "stt", done), event(259.16, "classify", done, ms=175.0),
                    event(259.16, "chat", done, ms=706.0, total_tokens=2524, cost=0.0041),
                    *(event(2139.6 - 4 * i, "stt", failed, status=502, ms=400.0) for i in range(5)),
                    event(38, "chat", "RJ1", job="report", ms=1416.0, total_tokens=3522, cost=0.0062)],
        "caddy": [caddy("/<gate>/v1/entries", method="POST", ts=NOW - 10, **{"X-Forwarded-Host": HOST}),
                  caddy(f"/<gate>/v1/entries/{DONE}", ts=NOW - 257, **{"X-Forwarded-Host": HOST}),
                  caddy(f"/{SECRET}/v1/me", ts=NOW - 30, Referer=f"https://{HOST}/{SECRET}/",
                        **{"X-Forwarded-Host": HOST}),
                  caddy("/healthz", host="api.notch.localhost", ua="curl/8.7.1", ts=NOW - 15),
                  caddy("/<gate>/healthz", ua=OWN_UA, ts=NOW - 5),
                  caddy("/wp-login.php", status=404, via=False, ua="curl/8.7.1", ts=NOW - 15.7),
                  caddy("/.env", status=404, via=False, ua="Mozilla/5.0 zgrab/0.x", country="DE", ts=NOW - 27500)],
        "server": [f"{stamp(3000)} INFO notch_api.app: resumed 0 unfinished job(s)",
                   f'{stamp(259)} INFO httpx: HTTP Request: POST https://openrouter.ai/api/v1/audio/transcriptions'
                   ' "HTTP/1.1 200 OK"',
                   f"{stamp(600)} ERROR notch_api.worker: job {failed} could not record its outcome",
                   "Traceback (most recent call last):",
                   '  File "notch_api/worker.py", line 81, in run',
                   f"ValueError: bad path /{SECRET}/v1/me"],
        "tunnel": ["2026-09-25T20:31:28Z INF |  https://%s  |" % HOST,
                   f'2026-09-25T20:32:40Z WRN Request failed path=/{SECRET}/healthz error="EOF"'],
    }
    paths = {}
    for name, lines in files.items():
        paths[name] = tmp_path / f"{name}.log"
        paths[name].write_text("\n".join(lines) + "\n")
    (tmp_path / "gate-secret").write_text(SECRET + "\n")
    return serve(handler=handler, run=fake_devicectl(DEVICES), db=db_path, audio_dir=audio_dir,
                 metrics=str(paths["metrics"]), caddy_log=str(paths["caddy"]), server_log=str(paths["server"]),
                 tunnel_log=str(paths["tunnel"]), tunnel_metrics="127.0.0.1:20241",
                 gate_secret_file=str(tmp_path / "gate-secret"), device=UDID, openrouter_key="sk-or-v1-test")


def test_a_live_snapshot_has_the_sample_shape(stack):
    snap = stack.get("api/snapshot").json()
    assert {name: s["state"] for name, s in snap["sources"].items()} == {name: "ok" for name in SAMPLE["sources"]}
    assert list(snap["sources"]) == list(SAMPLE["sources"]) and list(snap["checks"]) == list(SAMPLE["checks"])
    assert_shape(snap, SAMPLE)


def test_the_secret_never_leaves_even_when_a_log_line_or_a_referer_holds_it(stack):
    body = stack.get("api/snapshot").text
    assert SECRET not in body.lower() and "sk-or-v1-test" not in body and "sk-or-v1-abc" not in body
    snap = json.loads(body)
    assert "GET /v1/me" in [r["route"] for r in snap["traffic"]["routes"]]  # the old line still counts, redacted


def test_what_the_stack_shows(stack):
    snap = stack.get("api/snapshot").json()
    checks = snap["checks"]
    assert [p["check"] for p in snap["overall"]["problems"]] == ["worker"]  # the notch that failed 35 min ago
    assert {k: c["status"] for k, c in checks.items()} == {
        "phone": "ok", "tunnel": "ok", "gate": "ok", "server": "ok", "worker": "warn", "openrouter": "ok"}
    assert (checks["phone"]["last_route"], checks["phone"]["requests_1h"]) == ("POST /v1/entries", 3)
    assert checks["phone"]["device"]["read_at"] == NOW
    assert checks["tunnel"]["host"] == checks["tunnel"]["phone_host"] == HOST
    assert (checks["gate"]["blocked_1h"], checks["gate"]["blocked_24h"]) == (1, 2)
    assert checks["worker"]["captures"] == {"queued": 0, "transcribing": 0, "analyzing": 1}
    assert (checks["worker"]["done_24h"], checks["worker"]["failed_24h"], checks["worker"]["failed_1h"]) == (3, 1, 1)
    assert snap["traffic"]["by_source_1h"] == {"gate": 3, "local": 1}  # never the probes, never our own
    assert [b["path"] for b in snap["blocked"]["recent"]] == ["/wp-login.php", "/.env"]
    assert snap["models"]["source"] == "metrics"
    kinds = {k["kind"]: k for k in snap["models"]["kinds"]}
    assert (kinds["stt"]["calls"], kinds["stt"]["failed"]) == (7, 5)
    assert (kinds["chat"]["total_tokens"], kinds["chat"]["cost_usd"]) == (6046, 0.0103)
    assert snap["models"]["recent"][0]["entry_id"] == FLIGHT
    assert next(r for r in snap["models"]["recent"] if r["job"] == "report")["entry_id"] is None
    (error,) = snap["errors"]["server"]
    assert error["exception"].startswith("ValueError") and error["count"] == 1
    assert [e["level"] for e in snap["errors"]["tunnel"]] == ["WRN"]
    assert snap["record"]["entries"]["total"] == 4 and snap["record"]["audio"]["disk_files"] == 4


def phases(snap, entry_id):
    row = next(r for r in snap["pipeline"]["rows"] if r["entry_id"] == entry_id)
    return row, [(p["name"], p["state"], p["start_ms"], p["ms"], p["calls"], p["failed_calls"]) for p in row["phases"]]


def test_the_waterfall_places_each_phase_from_the_metrics_and_runs_the_one_in_flight(stack):
    snap = stack.get("api/snapshot").json()

    row, steps = phases(snap, FLIGHT)
    assert (row["phase_source"], row["elapsed_ms"], row["finished_at"]) == ("metrics", 10000.0, None)
    assert steps == [("wait", "done", 0.0, 0.0, None, None),
                     ("stt", "done", 400.0, 1830.0, 1, 0),
                     ("chat", "running", 2230.0, 7770.0, 0, 0),  # nothing back from chat yet: runs from stt's end
                     ("classify", "done", 2350.0, 190.0, 1, 0)]

    row, steps = phases(snap, DONE)
    assert [s[:2] for s in steps] == [("wait", "done"), ("stt", "done"), ("classify", "done"), ("chat", "done")]
    assert steps[2][2] == steps[3][2]  # Jev and the chat model run side by side

    row, steps = phases(snap, FAILED)
    assert steps[-1] == ("stt", "failed", 400.0, 16400.0, 5, 5)
    assert row["failure_code"] == "model_unavailable" and "502" in row["note"]
    assert phases(snap, DONE)[0]["note"] is None


def test_without_metrics_a_notch_shows_its_wait_and_its_run_from_the_record(stack):
    row, steps = phases(stack.get("api/snapshot").json(), OLD)
    assert row["phase_source"] == "db"
    assert steps == [("wait", "done", 0.0, 10000.0, None, None), ("run", "done", 10000.0, 10000.0, None, None)]


def test_models_fall_back_to_the_server_log_until_the_metrics_file_appears(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("\n".join(f'{stamp(ago)} INFO httpx: HTTP Request: POST https://openrouter.ai/api/{path}'
                             f' "HTTP/1.1 {code} X"' for ago, path, code in [
                                 (100, "v1/audio/transcriptions", 200), (90, "alpha/decisions", 502),
                                 (80, "v1/chat/completions", 200), (90000, "v1/chat/completions", 200)]) + "\n")
    web = serve(server_log=str(log), metrics=str(tmp_path / "phone-metrics.jsonl"))
    models = web.get("api/snapshot").json()["models"]
    assert models["source"] == "server_log" and models["note"]
    assert [(k["kind"], k["calls"], k["failed"], k["model"], k["p50_ms"]) for k in models["kinds"]] == [
        ("stt", 1, 0, None, None), ("classify", 1, 1, None, None), ("chat", 1, 0, None, None)]


@pytest.mark.parametrize("configured", [False, True], ids=["off", "unreachable"])
def test_every_optional_source_missing_still_answers_200_with_the_same_shape(tmp_path, configured):
    missing = str(tmp_path / "nothing-here")
    settings = dict(tunnel_metrics="127.0.0.1:20241", device=UDID, openrouter_key="sk-or-v1-test",
                    **{k: missing for k in ("audio_dir", "metrics", "caddy_log", "server_log", "tunnel_log",
                                            "gate_secret_file")}) if configured else {}
    response = serve(db=str(tmp_path / "phone.db"), **settings).get("api/snapshot")
    assert response.status_code == 200
    snap = response.json()
    assert_shape(snap, SAMPLE)
    assert {s["state"] for name, s in snap["sources"].items() if name != "db"} == {
        "unreachable" if configured else "off"}
    assert snap["sources"]["db"]["state"] == "unreachable" and not (tmp_path / "phone.db").exists()
    assert snap["checks"]["server"]["status"] == "critical"  # nothing answered on 127.0.0.1:4131
    assert snap["models"]["source"] is None and snap["pipeline"] is None


def test_a_probe_paused_while_nobody_watched_leaves_its_source_readable_and_drops_only_its_value(tmp_path):
    (tmp_path / "gate-secret").write_text(SECRET + "\n")
    now = [NOW]
    web = serve(handler=handler, clock=lambda: now[0], tunnel_metrics="127.0.0.1:20241",
                gate_secret_file=str(tmp_path / "gate-secret"), openrouter_key="sk-or-v1-test")
    now[0] += 601  # past every watched-only max age: the probes paused, nothing failed
    snap = web.get("api/snapshot").json()
    assert [snap["sources"][name]["state"] for name in ("gate_secret", "openrouter")] == ["ok", "ok"]
    assert snap["checks"]["tunnel"]["probe"] is None and snap["checks"]["openrouter"]["status"] == "unknown"


@pytest.mark.parametrize("age, status, stuck", [(120, "ok", 0), (121, "warn", 1)])
def test_a_job_is_stuck_only_once_it_has_waited_more_than_two_minutes(db_path, jobs, age, status, stuck):
    jobs(FLIGHT, "queued", age)
    worker = serve(db=db_path).get("api/snapshot").json()["checks"]["worker"]
    assert (worker["status"], worker["stuck"], worker["busy"]) == (status, stuck, True)


# -- status rules ------------------------------------------------------------

CF = {"ready_connections": 1, "edge": "ewr14", "rtt_ms": 18.0, "version": "2026.9.3", "requests_total": 53,
      "request_errors": 0, "host": HOST}
PROBE = {"at": NOW, "ok": True, "http_status": 200, "latency_ms": 212.4, "error": None}
FAILED_PROBE = {"at": NOW, "ok": False, "http_status": 502, "latency_ms": 90.0, "error": "HTTP 502"}


@pytest.mark.parametrize("health, at, status", [
    (None, None, "unknown"),
    ({"http_status": 200, "latency_ms": 3.8, "error": None}, NOW - 11, "unknown"),
    ({"http_status": None, "latency_ms": None, "error": "couldn’t connect"}, NOW, "critical"),
    ({"http_status": 200, "latency_ms": 501.0, "error": None}, NOW, "warn"),
    ({"http_status": 200, "latency_ms": 3.8, "error": None}, NOW, "ok"),
], ids=["never", "stale", "down", "slow", "fine"])
def test_server_rules(health, at, status):
    assert snapshot.server_check(health, at, NOW, port=4131)["status"] == status


@pytest.mark.parametrize("kwargs, status", [
    ({"cf": CF | {"ready_connections": 0}, "probe": PROBE}, "critical"),
    ({"cf": CF, "probe": FAILED_PROBE}, "critical"),
    ({"cf": CF, "probe": FAILED_PROBE, "server_status": "critical"}, "ok"),  # the server's problem, not the tunnel's
    ({"cf": CF, "probe": PROBE, "phone_host": "old-name.trycloudflare.com"}, "warn"),
    ({"cf": CF, "probe": PROBE | {"latency_ms": 2001.0}}, "warn"),
    ({"cf": None, "cf_state": "off"}, "unknown"),
    ({"cf": None, "cf_state": "unreachable"}, "unknown"),
    ({"cf": CF, "probe": PROBE}, "ok"),
], ids=["no-connection", "phone-cut-off", "server-down", "moved", "slow", "off", "unreachable", "fine"])
def test_tunnel_rules(kwargs, status):
    kwargs = {"host": HOST, "phone_host": HOST, "server_status": "ok"} | kwargs
    assert snapshot.tunnel_check(**kwargs)["status"] == status


INTEGRITY = {"at": NOW, "healthz": 404, "docs": 404, "open": False, "error": None}


@pytest.mark.parametrize("integrity, host, leaked, status", [
    (INTEGRITY | {"docs": 200, "open": True}, HOST, 0, "critical"),
    (INTEGRITY, HOST, 1, "critical"),
    (None, None, 0, "unknown"),
    (None, HOST, 0, "unknown"),
    (INTEGRITY | {"docs": None, "error": "/docs timed out after 10 s"}, HOST, 0, "unknown"),
    (INTEGRITY, HOST, 0, "ok"),
], ids=["open", "leaked", "no-host", "not-checked", "half-checked", "closed"])
def test_gate_rules(integrity, host, leaked, status):
    reqs = [Req(NOW - 60, "passed", "GET", "/docs", "GET /docs", 200, 1.0, "curl/8", HOST, "US", "192.0.2.1")] * leaked
    assert snapshot.gate_check(integrity, host=host, reqs=reqs, now=NOW)["status"] == status


SPEND = {"limit_usd": 50.0, "remaining_usd": 49.76, "today_usd": 0.04, "week_usd": 0.2, "month_usd": 0.2,
         "total_usd": 0.24, "free_tier": False}


@pytest.mark.parametrize("spend, key_set, status", [
    (None, False, "unknown"),
    (None, True, "unknown"),
    (SPEND | {"remaining_usd": 0.0}, True, "critical"),
    (SPEND | {"remaining_usd": 4.99}, True, "warn"),
    (SPEND | {"limit_usd": None, "remaining_usd": None}, True, "ok"),
    (SPEND, True, "ok"),
], ids=["no-key", "no-read", "out", "low", "no-limit", "fine"])
def test_openrouter_rules(spend, key_set, status):
    assert snapshot.openrouter_check(spend, key_set=key_set, read_at=NOW, error="HTTP 401")["status"] == status


def check(status):
    return {"status": status, "word": status, "reason": f"{status} reason"}


@pytest.mark.parametrize("statuses, overall, first", [
    ({"tunnel": "warn", "worker": "critical"}, "critical", "worker"),
    ({"phone": "unknown", "gate": "unknown", "openrouter": "unknown"}, "ok", None),
    ({"server": "unknown"}, "unknown", None),
    ({"worker": "warn", "tunnel": "warn"}, "warn", "tunnel"),
], ids=["critical-first", "edges-unknown", "core-unknown", "strip-order"])
def test_overall_rules(statuses, overall, first):
    checks = {name: check(statuses.get(name, "ok")) for name in SAMPLE["checks"]}
    result = snapshot.overall(checks)
    assert result["status"] == overall
    assert (result["problems"][0]["check"] if result["problems"] else None) == first
