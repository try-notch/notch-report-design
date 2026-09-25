"""
snapshot.py — build(src, db, now) -> the api/snapshot document (DASHBOARD.md › Snapshot
contract), and the status rules with their thresholds. Pure: it reads the sources' cached
values and the DB rows it is handed, and writes every field itself, so nothing a source
returned reaches the page unread.
"""

import math
import time
from collections import Counter, defaultdict

from .logs import GATE, Call, Err, group_errors, is_phone
from .record import UNFINISHED

VERSION = 1
HOUR, DAY = 3600, 86400
STUCK_S = 120  # an unfinished job older than this is stuck
SERVER_MAX_AGE_S = 10  # a local /healthz older than this is no answer
SLOW_SERVER_MS = 500
SLOW_TUNNEL_MS = 2000
LOW_CREDIT_USD = 5
LEAK_WINDOW_S = 600
KINDS = ("stt", "classify", "chat")
SOURCES = ("db", "audio_dir", "metrics", "caddy_log", "server_log", "tunnel_log", "tunnel_metrics", "gate_secret",
           "openrouter", "device")
FAILURE_NOTES = {
    "model_unavailable": "The model service didn’t answer.",
    "model_refused": "The model service refused it.",
    "transcription_failed": "No words came back from the recording.",
    "audio_unreadable": "The recording couldn’t be read.",
}


# -- formats (the page's own, for the reasons the backend writes) ------------

def _r1(x):
    return None if x is None else round(x, 1)


def _r3(x):
    return None if x is None else round(x, 3)


def _ms(ms):
    """3.8 ms · 212 ms · 1.8 s · 3 min · 2 h 5 min."""
    if ms < 10:
        return f"{ms:.1f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    minutes = int(ms // 60_000)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _ago(seconds):
    """10 s · 3 min · 2 h · 1 day."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < HOUR:
        return f"{s // 60} min"
    return f"{s // HOUR} h" if s < DAY else f"{s // DAY} day" + ("s" if s >= 2 * DAY else "")


def _usd(x):
    return "<$0.01" if 0 < x < 0.01 else f"${x:.0f}" if x == int(x) else f"${x:.2f}"


def _plural(n, noun):
    return noun if n == 1 else noun + ("es" if noun.endswith("ch") else "s")


def _pct(values, p):
    """Nearest rank."""
    return round(sorted(values)[max(0, math.ceil(p / 100 * len(values)) - 1)], 1) if values else None


def _check(status, word, reason, **fields):
    return {"status": status, "word": word, "reason": reason, **fields}


# -- the checks: rules top to bottom, first match wins -----------------------

def phone_check(phone, *, source, device, now):
    """`phone`: passed gate requests with a Notch/ user agent in 24 h, or None when the Caddy log is unavailable."""
    last = max(phone, key=lambda r: r.at) if phone else None
    fields = {"last_seen_at": last and _r3(last.at), "last_route": last and last.route,
              "requests_1h": None if phone is None else sum(r.at >= now - HOUR for r in phone), "device": device}
    if phone is None:
        word = "Not set up" if source["state"] == "off" else "Can’t reach it"
        return _check("unknown", word, source["reason"], **fields)
    if last is None:
        return _check("unknown", "Not seen", "No sign of it through the gate in the last 24 hours.", **fields)
    return _check("ok", "Fine", f"Seen {_ago(now - last.at)} ago through the gate.", **fields)


def tunnel_check(cf, *, cf_state="ok", where=None, read_at=None, probe=None, host=None, phone_host=None,
                 server_status="ok"):
    fields = {k: cf and cf[k] for k in ("ready_connections", "edge", "rtt_ms", "version", "requests_total",
                                         "request_errors")}
    fields |= {"host": host, "phone_host": phone_host, "probe": probe, "read_at": _r3(read_at)}
    answered = probe is not None and probe["ok"]
    if cf and cf["ready_connections"] == 0:
        return _check("critical", "Down", "cloudflared is running but has no connection to Cloudflare.", **fields)
    if probe and not probe["ok"] and server_status != "critical":
        return _check("critical", "Down", f"Your phone can’t get through: {probe['error']}.", **fields)
    if host and phone_host and host != phone_host:
        return _check("warn", "Moved", "The tunnel has a new address. Rebuild the app so your phone can find it.",
                      **fields)
    if answered and probe["latency_ms"] > SLOW_TUNNEL_MS:
        return _check("warn", "Slow", f"Answering end to end, but slowly: {_ms(probe['latency_ms'])}.", **fields)
    if cf is None and probe is None:
        if cf_state == "off":
            return _check("unknown", "Not set up", "Not set up. Set NOTCH_DASH_TUNNEL_METRICS to watch the tunnel.",
                          **fields)
        return _check("unknown", "Can’t reach it", f"Couldn’t read cloudflared at {where}. Is the tunnel running?",
                      **fields)
    clauses = []
    if cf:
        n = cf["ready_connections"]
        clauses.append(f"{n} {_plural(n, 'connection')}" + (f" via {cf['edge']}" if cf["edge"] else ""))
    if answered:
        clauses.append(f"answered end to end in {_ms(probe['latency_ms'])}")
    reason = " · ".join(clauses) or "reached the gate; the server behind it didn’t answer"
    return _check("ok", "Fine", reason[0].upper() + reason[1:] + ".", **fields)


def gate_check(integrity, *, host, reqs, now):
    """`reqs`: the Caddy requests in 24 h, or None when the log is unavailable."""
    gate = None if reqs is None else [r for r in reqs if r.kind != "local"]
    blocked = None if gate is None else [r for r in gate if r.kind == "blocked"]
    leaks = None if gate is None else sum(
        r.kind == "passed" and not r.path.startswith(GATE + "/") and r.at >= now - LEAK_WINDOW_S for r in gate)
    fields = {"integrity": integrity,
              "blocked_1h": None if blocked is None else sum(r.at >= now - HOUR for r in blocked),
              "blocked_24h": None if blocked is None else len(blocked),
              "last_blocked_at": _r3(max(r.at for r in blocked)) if blocked else None,
              "passed_without_secret_10m": leaks}
    if integrity and integrity["open"]:
        which = " and ".join(f"/{n}" for n in ("healthz", "docs") if 200 <= (integrity[n] or 0) < 300)
        return _check("critical", "Open", f"The gate is open: {which} answered without the secret.", **fields)
    if leaks:
        return _check("critical", "Open", f"The gate let {leaks} {_plural(leaks, 'request')} through without the "
                      "secret in the last 10 min.", **fields)
    if not host:
        return _check("unknown", "Not set up", "No tunnel address to check from outside.", **fields)
    if integrity is None or not integrity["healthz"] == integrity["docs"] == 404:
        why = "not checked yet" if integrity is None else integrity["error"] or ", ".join(
            f"/{n} answered {integrity[n]}" for n in ("healthz", "docs") if integrity[n] != 404)
        return _check("unknown", "Can’t reach it", f"Couldn’t check the gate from outside: {why}.", **fields)
    return _check("ok", "Fine", "/healthz and /docs stay closed without the secret.", **fields)


def server_check(health, at, now, *, port, started_at=None):
    """`health`: the latest local /healthz result (made at `at`), failures included, or None before the first."""
    fields = {"port": port, "at": _r3(at), **{k: health and health[k] for k in ("http_status", "latency_ms", "error")},
              "started_at": _r3(started_at)}
    if health is None or now - at > SERVER_MAX_AGE_S:
        return _check("unknown", "Can’t reach it", "Not checked yet.", **fields)
    if health["error"]:
        return _check("critical", "Down", f"The server didn’t answer on 127.0.0.1:{port}.", **fields)
    if health["latency_ms"] > SLOW_SERVER_MS:
        return _check("warn", "Slow", f"Answering, but slowly: {_ms(health['latency_ms'])}.", **fields)
    return _check("ok", "Fine", f"Answering in {_ms(health['latency_ms'])}.", **fields)


def _noun(jobs):
    kinds = {j["kind"] for j in jobs}
    return "notch" if kinds == {"capture"} else "report" if kinds == {"report"} else "job"


def worker_check(db, *, where, now):
    if db is None:
        return _check("unknown", "Can’t reach it", f"Couldn’t read {where or 'the record'}.", busy=False,
                      **dict.fromkeys(("captures", "reports", "oldest_pending_ms", "stuck", "done_24h", "failed_24h",
                                       "failed_1h")))
    pending, finished = db["pending"], db["finished"]
    counts = {kind: dict.fromkeys(states, 0) for kind, states in UNFINISHED.items()}
    for j in pending:
        counts[j["kind"]][j["state"]] += 1

    def age(j):  # waiting since submitted; working since started (report jobs keep no start)
        return now - (j["submitted_at"] if j["state"] == "queued" else j["started_at"] or j["submitted_at"])

    waiting = [j for j in pending if j["state"] == "queued" and age(j) > STUCK_S]
    working = [j for j in pending if j["state"] != "queued" and age(j) > STUCK_S]
    oldest = max((now - j["submitted_at"] for j in pending), default=None)
    done, failed, failed_1h = (sum(f[key] for f in finished.values()) for key in ("complete", "failed", "failed_1h"))
    fields = {"busy": bool(pending), "captures": counts["capture"], "reports": counts["report"],
              "oldest_pending_ms": _r1(oldest and oldest * 1000), "stuck": len(waiting) + len(working),
              "done_24h": done, "failed_24h": failed, "failed_1h": failed_1h}
    for stuck, one, many in (
            (waiting, "One {noun} has waited {age}. It’s saved; the worker hasn’t picked it up.",
             "{n} {nouns} have waited up to {age}. They’re saved; the worker hasn’t picked them up."),
            (working, "One {noun} has been working for {age}. It’s saved.",
             "{n} {nouns} have been working for up to {age}. They’re saved.")):
        if stuck:
            n, noun = len(stuck), _noun(stuck)
            words = {"n": n, "noun": noun, "nouns": _plural(n, noun), "age": _ms(max(map(age, stuck)) * 1000)}
            return _check("warn", "Stuck", (one if n == 1 else many).format(**words), **fields)
    if failed_1h:
        lost = [f"{n} {_plural(n, noun)}" for noun, n in (("notch", finished["capture"]["failed_1h"]),
                                                          ("report", finished["report"]["failed_1h"])) if n]
        return _check("warn", "Failed", f"{' and '.join(lost)} couldn’t be written up in the last hour.", **fields)
    if pending:
        return _check("ok", "Working", f"{len(pending)} working now.", **fields)
    return _check("ok", "Fine", f"Nothing waiting · {done} done in 24 h.", **fields)


def openrouter_check(spend, *, key_set, read_at=None, error=None):
    """`spend`: the last good /key read while it is under 10 min old, else None."""
    fields = {k: spend and spend[k] for k in ("limit_usd", "remaining_usd", "today_usd", "week_usd", "month_usd",
                                              "total_usd", "free_tier")} | {"read_at": _r3(read_at) if spend else None}
    if not key_set:
        return _check("unknown", "Not set up", "Not set up. Add OPENROUTER_API_KEY to .env to see spend.", **fields)
    if spend is None:
        why = error or ("not read yet" if read_at is None else "not read in the last 10 min")
        return _check("unknown", "Can’t reach it", f"Couldn’t read spend from OpenRouter: {why}.", **fields)
    limit, left = spend["limit_usd"], spend["remaining_usd"]
    capped = limit is not None and left is not None
    if capped and left <= 0:
        return _check("critical", "Out", "No credit left. Model calls will be refused until it’s topped up.", **fields)
    if capped and left < LOW_CREDIT_USD:
        return _check("warn", "Low", f"{_usd(left)} left of {_usd(limit)}.", **fields)
    tail = f"{_usd(left)} left of {_usd(limit)}" if capped else "no limit"
    return _check("ok", "Fine", f"{_usd(spend['today_usd'] or 0)} today · {_usd(spend['month_usd'] or 0)} this month"
                  f" · {tail}.", **fields)


def overall(checks):
    """`checks` in strip order -> {status, word, reason, problems}."""
    problems = [{"check": name, **{k: c[k] for k in ("status", "word", "reason")}}
                for name, c in checks.items() if c["status"] in ("warn", "critical")]
    problems.sort(key=lambda p: p["status"] != "critical")  # stable: strip order within each
    if problems:
        status = problems[0]["status"]
        word = "Needs you now" if status == "critical" else "Worth a look"
        return {"status": status, "word": word, "reason": problems[0]["reason"], "problems": problems}
    core = [checks[name] for name in ("server", "worker") if checks[name]["status"] == "unknown"]
    if core:
        return {"status": "unknown", "word": "Can’t tell yet", "reason": core[0]["reason"], "problems": []}
    return {"status": "ok", "word": "All fine", "reason": "Nothing needs you right now.", "problems": []}


# -- the panels ---------------------------------------------------------------

def _phase(name, state, start_ms, ms, calls=None, failed_calls=None):
    return {"name": name, "state": state, "start_ms": start_ms, "ms": ms, "calls": calls, "failed_calls": failed_calls}


def _phases(row, events, now):
    """(phase_source, phases) for one notch: from its metrics events, else from the job row's own times."""
    if row["job_id"] is None:
        return "db", []
    sub, started, state = row["submitted_at"], row["started_at"], row["state"]
    end = row["finished_at"] if row["finished_at"] is not None else now
    in_flight = state in UNFINISHED["capture"]

    def ms(a, b):
        return _r1((b - a) * 1000)

    if started is None:
        return "db", [_phase("wait", "running" if in_flight else "done", 0.0, ms(sub, end))]
    phases = [_phase("wait", "done", 0.0, ms(sub, started))]
    if not events:
        run = "running" if in_flight else "failed" if state == "failed" else "done"
        return "db", phases + [_phase("run", run, ms(sub, started), ms(started, end))]
    timed, stt_end = [], started
    for kind in KINDS:
        mine = [e for e in events if e.kind == kind]
        running = in_flight and (state == "transcribing" if kind == "stt" else
                                 state == "analyzing" and not any(e.ok for e in mine))
        if not mine and not running:
            continue
        first = mine[0].at if mine else stt_end  # classify and chat both start when stt ends: they run side by side
        stop = now if running else max(e.at + (e.ms or 0) / 1000 for e in mine)
        status = "running" if running else "failed" if state == "failed" and not mine[-1].ok else "done"
        timed.append(_phase(kind, status, ms(sub, first), ms(first, stop), len(mine), sum(not e.ok for e in mine)))
        stt_end = stop if kind == "stt" else stt_end
    return "metrics", phases + sorted(timed, key=lambda p: p["start_ms"])


def _note(row, phases, events):
    """One sentence for a failed notch: what the calls said, else what its failure code means."""
    broken = next((p for p in phases if p["state"] == "failed" and p["calls"]), None)
    if broken:
        status = [e for e in events if e.kind == broken["name"]][-1].status
        how = (f"answered {status}" if type(status) is int else "timed out" if status == "timeout"
               else "couldn’t connect")
        tries = f"{broken['calls']} {_plural(broken['calls'], 'try')}"
        note = f"Couldn’t write this one. {broken['name']} {how} after {tries}."
    else:
        note = FAILURE_NOTES.get(row["failure_code"], "It couldn’t be written up.")
    return note + (" The audio is still here." if row["audio_on_disk"] else "")


def pipeline(db, calls, now):
    if db is None:
        return None
    by_job = defaultdict(list)
    for c in sorted(calls or (), key=lambda c: c.at):
        if c.job_id and c.kind in KINDS:
            by_job[c.job_id].append(c)
    rows = []
    for r in db["rows"]:
        events = by_job.get(r["job_id"], [])
        source, phases = _phases(r, events, now)
        failed = r["state"] == "failed"
        end = r["finished_at"] if r["finished_at"] is not None else now
        rows.append({
            "entry_id": r["entry_id"], "job_id": r["job_id"], "state": r["state"], "recorded_at": r["recorded_at"],
            "submitted_at": r["submitted_at"], "finished_at": r["finished_at"],
            "elapsed_ms": _r1((end - r["submitted_at"]) * 1000), "attempts": r["attempts"],
            "failure_code": r["failure_code"] if failed else None,
            "note": _note(r, phases, events) if failed else None,
            "recording_ms": _r1((r["duration_seconds"] or 0) * 1000), "words": r["words"],
            "audio_on_disk": r["audio_on_disk"], "phase_source": source, "phases": phases})
    return {"counts_24h": db["counts_24h"], "rows": rows}


def traffic(reqs, now):
    if reqs is None:
        return None
    seen = [r for r in reqs if r.kind != "blocked"]
    hour = [r for r in seen if r.at >= now - HOUR]
    start = now // 60 * 60 - 59 * 60
    by_class = {c: [0] * 60 for c in ("2xx", "3xx", "4xx", "5xx")}
    routes = defaultdict(list)
    for r in seen:
        i, cls = int((r.at - start) // 60), f"{r.status // 100}xx"
        if 0 <= i < 60 and cls in by_class:
            by_class[cls][i] += 1
        routes[r.route].append(r)
    table = sorted(({"route": route, "count": len(rs), "errors": sum(r.status >= 400 for r in rs),
                     "p50_ms": _pct([r.ms for r in rs], 50), "p95_ms": _pct([r.ms for r in rs], 95),
                     "last_at": _r3(max(r.at for r in rs))} for route, rs in routes.items()),
                   key=lambda t: (-t["count"], t["route"]))
    return {"requests_1h": len(hour), "errors_1h": sum(r.status >= 400 for r in hour),
            "by_source_1h": {"gate": sum(r.kind == "passed" for r in hour),
                             "local": sum(r.kind == "local" for r in hour)},
            "hour": {"start_at": start, "step_s": 60, "by_class": by_class}, "routes": table[:12]}


def _usage(calls, key):
    values = [c.usage[key] for c in calls if c.usage and key in c.usage]
    return round(sum(values), 6) if values else None


def models(calls, logged, job_entries):
    """Model calls in 24 h from the metrics file, else httpx's lines in the server log."""
    if calls is not None:
        source, note, items = "metrics", None, calls
    elif logged is not None:
        source, note = "server_log", "Counting from the server log until the metrics file appears."
        items = [c for c in logged if isinstance(c, Call)]
    else:
        return {"source": None, "note": "Not set up. Restart the server so it writes NOTCH_METRICS, or set "
                "NOTCH_DASH_SERVER_LOG.", "kinds": [], "recent": []}
    items = sorted(items, key=lambda c: c.at)
    kinds = []
    for kind in (*KINDS, "tts"):
        mine = [c for c in items if c.kind == kind]
        if kind == "tts" and not mine:
            continue
        latencies = [c.ms for c in mine if c.ms is not None]
        kinds.append({"kind": kind, "model": mine[-1].model if mine else None, "calls": len(mine),
                      "failed": sum(not c.ok for c in mine),
                      "p50_ms": _pct(latencies, 50), "p95_ms": _pct(latencies, 95),
                      **{k: _usage(mine, k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                      "cost_usd": _usage(mine, "cost"), "last_at": _r3(mine[-1].at) if mine else None})
    recent = [{"at": _r3(c.at), "kind": c.kind, "model": c.model, "tool": c.tool, "status": c.status, "ok": c.ok,
               "latency_ms": c.ms, "attempt": c.attempt, "job": c.job,
               "entry_id": job_entries.get(c.job_id) if c.job == "capture" else None,
               "total_tokens": (c.usage or {}).get("total_tokens"), "cost_usd": (c.usage or {}).get("cost")}
              for c in reversed(items[-20:])]
    return {"source": source, "note": note, "kinds": kinds, "recent": recent}


def blocked(reqs, now):
    if reqs is None:
        return None
    refused = sorted((r for r in reqs if r.kind == "blocked"), key=lambda r: r.at, reverse=True)
    top = sorted(Counter(r.path for r in refused).items(), key=lambda pc: (-pc[1], pc[0]))[:5]
    return {"count_1h": sum(r.at >= now - HOUR for r in refused), "count_24h": len(refused),
            "top_paths": [{"path": path, "count": n} for path, n in top],
            "recent": [{"at": _r3(r.at), "method": r.method, "path": r.path, "status": r.status, "country": r.country,
                        "user_agent": r.ua, "client_ip": r.ip} for r in refused[:50]]}


def record(db, audio):
    if db is None:
        return None
    rec, stored = db["record"], db["record"]["audio"]
    return {**rec, "audio": {"objects": stored["objects"], "db_bytes": stored["db_bytes"],
                             "disk_bytes": audio and audio["bytes"], "disk_files": audio and audio["files"],
                             "past_retention": stored["past_retention"]}}


# -- the document ---------------------------------------------------------------

def build(src, db, now, started=None):
    """
    `src`: app.Sources; `db`: record.read()'s result, or None when the record couldn't be
    read; `started`: the perf_counter() the request began at, for took_ms.
    """
    started = started or time.perf_counter()
    sources = {name: src.source(name, now) for name in SOURCES}

    def window(name, tail):
        return [i for i in tail.items() if i.at >= now - DAY] if sources[name]["state"] == "ok" else None

    reqs, calls = window("caddy_log", src.caddy), window("metrics", src.metrics)
    logged, tunnel_errors = window("server_log", src.server_log), window("tunnel_log", src.tunnel_log)
    phone = None if reqs is None else [r for r in reqs if is_phone(r)]
    host = src.host(now)
    probe, integrity, device = src.e2e.current(now), src.integrity.current(now), src.device.current(now)
    server = server_check(src.health.value, src.health.read_at, now, port=src.settings.notch_port,
                          started_at=src.server_parser.started_at if logged is not None else None)
    checks = {
        "phone": phone_check(phone, source=sources["caddy_log"], now=now,
                             device=device and {**device, "read_at": _r3(src.device.read_at)}),
        "tunnel": tunnel_check(src.cloudflared.current(now), cf_state=sources["tunnel_metrics"]["state"],
                               where=src.settings.tunnel_metrics, read_at=src.cloudflared.read_at,
                               probe=probe and {"at": _r3(src.e2e.read_at), **probe}, host=host,
                               phone_host=src.caddy_parser.phone_host if reqs is not None else None,
                               server_status=server["status"]),
        "gate": gate_check(integrity and {"at": _r3(src.integrity.read_at), **integrity}, host=host, reqs=reqs,
                           now=now),
        "server": server,
        "worker": worker_check(db, where=sources["db"]["where"], now=now),
        "openrouter": openrouter_check(src.openrouter.current(now), key_set=bool(src.settings.openrouter_key),
                                       read_at=src.openrouter.read_at, error=src.openrouter.error),
    }
    snap = {
        "v": VERSION, "generated_at": _r3(now), "took_ms": None, "overall": overall(checks), "checks": checks,
        "pipeline": pipeline(db, calls, now), "traffic": traffic(reqs, now),
        "models": models(calls, logged, db["job_entries"] if db else {}), "blocked": blocked(reqs, now),
        "record": record(db, src.audio.current(now)),
        "errors": {"server": None if logged is None else group_errors([e for e in logged if isinstance(e, Err)]),
                   "tunnel": None if tunnel_errors is None else group_errors(tunnel_errors)},
        "sources": sources,
    }
    snap["took_ms"] = _r1((time.perf_counter() - started) * 1000)
    return snap
