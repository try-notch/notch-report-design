"""
logs.py — pure readers for the lines notch_dash follows (Caddy's JSON access log, the
metrics JSONL, notch_api's server log, cloudflared's log), and `redact`, which every string
from a log, a header, a path or an error passes through before it is kept.

Log text, paths and user agents come from the internet (public probes): they are data only.
"""

import json
import re
import secrets
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone

from notch_api.metrics import kind as call_kind

GATE_HOST, LOCAL_HOST, GATE = "notch-gate.localhost", "api.notch.localhost", "/<gate>"
# The dashboard's public probes send this user agent and ask only these paths. The token is new each run, so a
# prober that copies "notch-dash/1" after this run started is still counted; before it, such a line is taken as
# an earlier run's probe.
OWN_UA, OWN_PATHS = f"notch-dash/1 ({secrets.token_hex(8)})", (GATE + "/healthz", "/healthz", "/docs")
STARTED = time.time()

Req = namedtuple("Req", "at kind method path route status ms ua host country ip")  # kind: passed | blocked | local
Call = namedtuple("Call", "at kind model tool status ok ms attempt job job_id usage")
Err = namedtuple("Err", "at level where message lines")  # `lines` grows while a traceback follows it

_KEY = re.compile(r"sk-or-[\w-]+")
_BEARER = re.compile(r"Bearer\s+\S+")
_HEX = re.compile(r"(?:[0-9a-fA-F]|%3[0-9]|%4[1-6]|%6[1-6]){48,}")  # the gate secret, any hex digit escaped or not
_ID = re.compile(r"\d+|[0-9A-Fa-f-]{16,}")
_VARYING = re.compile(r"[0-9A-Fa-f-]{16,}|\d+(?:\.\d+)?")


def redact(text, limit=300):
    """Hide OpenRouter keys, bearer tokens and anything shaped like the gate secret; cut to `limit` characters."""
    text = _HEX.sub("<gate>", _BEARER.sub("Bearer <redacted>", _KEY.sub("<key>", str(text))))
    return text if limit is None or len(text) <= limit else text[:limit - 1] + "…"


def _text(value, limit=200):
    return redact(value, limit) if isinstance(value, str) else None


def route(method, uri):
    """'GET /<gate>/v1/entries/<uuid>?x=1' -> 'GET /v1/entries/{id}'."""
    path = uri.split("?", 1)[0]
    path = path[len(GATE):] if path.startswith(GATE + "/") else path
    return redact(f"{method} " + "/".join("{id}" if _ID.fullmatch(part) else part for part in path.split("/")), 200)


def parse_caddy(line, *_):
    """One access-log line -> Req, or None for other hosts, this run's own probes and junk."""
    try:
        entry = json.loads(line)
        request = entry["request"]
        headers = {k.lower(): v[0] for k, v in (request.get("headers") or {}).items() if isinstance(v, list) and v}
        at, status, ms = float(entry["ts"]), int(entry["status"]), round(float(entry.get("duration") or 0) * 1000, 1)
        host, resp_headers = request.get("host"), {k.lower() for k in entry.get("resp_headers") or {}}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if host not in (GATE_HOST, LOCAL_HOST):
        return None
    ua = _text(headers.get("user-agent")) or ""
    method, uri = _text(request.get("method"), 16) or "?", redact(request.get("uri") or "", None)
    if host == LOCAL_HOST:
        kind = "local"
    else:  # passed = Caddy proxied it: the server answered (Via), or wasn't there to
        proxied = "via" in resp_headers or (uri.startswith(GATE + "/") and status in (502, 503, 504))
        kind = "passed" if proxied else "blocked"
    own = ua == OWN_UA or ua.startswith("notch-dash/") and at < STARTED  # this run's, or an earlier one's
    if own and uri in OWN_PATHS and (kind == "blocked" or uri.startswith(GATE + "/")):
        return None  # a probe of ours, unless it got through without the secret: that is a leak to count
    return Req(at, kind, method, redact(uri, 200), route(method, uri), status, ms, sys.intern(ua),
               _text(headers.get("x-forwarded-host")), _text(headers.get("cf-ipcountry"), 8),
               _text(headers.get("cf-connecting-ip"), 64))


def is_phone(req):
    """A request the phone made: it went through the gate, from the Notch app."""
    return req.kind == "passed" and req.ua.startswith("Notch/")


class CaddyLog:
    """
    parse_caddy, remembering the host the phone last came in by however long ago it was:
    the phone's build has that address baked in, so it can't come in by a newer one.
    """

    def __init__(self):
        self.phone_host = None

    def feed(self, line, *_):
        req = parse_caddy(line)
        if req and is_phone(req) and req.host:
            self.phone_host = req.host
        return req


def parse_metric(line, *_):
    """One NOTCH_METRICS line (notch_api/metrics.py) -> Call, or None."""
    try:
        e = json.loads(line)
        status, usage = e.get("status"), e.get("usage")
        return Call(float(e["ts"]), _text(e["kind"], 16), _text(e.get("model")), _text(e.get("tool")),
                    status if type(status) is int else _text(status, 16), e.get("ok") is True,
                    _number(e.get("latency_ms")), _number(e.get("attempt")), _text(e.get("job"), 16),
                    _text(e.get("job_id"), 64),
                    {k: v for k, v in usage.items() if _number(v) is not None} if isinstance(usage, dict) else None)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _number(value):
    return value if type(value) in (int, float) else None


_STAMPED = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ([A-Z]+) ([\w.]+): (.*)")
_BARE = re.compile(r"([A-Z]+):\s+(.*)")  # uvicorn's own format, with no time
_HTTPX = re.compile(r'HTTP Request: POST (\S+) "HTTP/[\d.]+ (\d{3})')
LEVELS = ("WARNING", "ERROR", "CRITICAL")


class ServerLog:
    """
    notch_api's stdout and stderr. feed() yields model calls (httpx's request lines) and
    errors; a traceback extends the error above it. A line with no time of its own (uvicorn's)
    is dated `now` when it was read while following the file, else it takes the last time seen:
    a stamped line can be hours old, since only model calls and startup write one.
    `started_at` is the last "resumed N unfinished job(s)".
    """

    def __init__(self):
        self.started_at = self._last_at = self._open = None

    def feed(self, line, now, following=False):
        if line.startswith("{") and (parsed := _json_line(line)) is not None:
            # notch_api's scrubbed JSON lines (privacy.py): the event is a template, never content.
            at, level, where, message = parsed
            self._last_at, self._open = at, None
            if where == "notch_api.app" and message.startswith("resumed "):
                self.started_at = at
            return self._error(at, level, where, message, line) if level in LEVELS else None
        if m := _STAMPED.match(line):
            at = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").timestamp() + int(m[2]) / 1000  # local time
            level, where, message = m[3], m[4], m[5]
            self._last_at, self._open = at, None
            if where == "httpx" and (call := _HTTPX.search(message)):
                status = int(call[2])
                return Call(at, call_kind(call[1]), None, None, status, 200 <= status < 300, *[None] * 5)
            if where == "notch_api.app" and message.startswith("resumed "):
                self.started_at = at
            return self._error(at, level, where, message, line) if level in LEVELS else None
        if m := _BARE.match(line):
            self._open = None
            at = now if following else self._last_at or now
            return self._error(at, m[1], "uvicorn.error", m[2], line) if m[1] in LEVELS else None
        if self._open is not None and len(self._open.lines) < 400:
            self._open.lines.append(redact(line, 1000))
        return None

    def _error(self, at, level, where, message, line):
        self._open = Err(at, level, redact(where, 100), redact(message), [redact(line, 1000)])
        return self._open


_TUNNEL = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z (WRN|ERR) (.*)")
QUICK_HOST = re.compile(r"[a-z0-9-]+\.trycloudflare\.com")  # a quick tunnel's address: the probes send the secret there
_URL = re.compile(rf"https://({QUICK_HOST.pattern})\b")
_FIELDS = re.compile(r'\s+[\w.-]+=(?:"(?:[^"\\]|\\.)*"|\S+)')


class TunnelLog:
    """cloudflared's log: `host` is the latest quick-tunnel address; feed() yields its WRN/ERR lines."""

    def __init__(self):
        self.host = None

    def feed(self, line, *_):
        if " INF " in line and (m := _URL.search(line)) and m[1] != "api.trycloudflare.com":
            self.host = m[1]
        elif m := _TUNNEL.match(line):
            at = datetime.strptime(m[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            return Err(at, m[2], None, redact(_FIELDS.sub("", m[3]).strip()), [redact(line, 1000)])
        return None


def _json_line(line):
    """A scrubbed notch_api line -> (at, level, logger, event [exception type]), or None if it is not one."""
    try:
        data = json.loads(line)
        at = datetime.strptime(data["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, KeyError, TypeError):
        return None
    level, where, event = data.get("level"), data.get("logger"), data.get("event")
    if not all(isinstance(v, str) for v in (level, where, event)):
        return None
    kind = data.get("exc_type") if isinstance(data.get("exc_type"), str) else None
    return at, level, where, f"{event} [{kind}]" if kind else event


def _exception(lines):
    """A traceback's last line, or None when the entry has no traceback."""
    if not any(line.startswith("Traceback") for line in lines[1:]):
        return None
    return redact(next(line for line in reversed(lines) if line.strip()).strip())


def _sample(lines):
    lines = lines if len(lines) <= 40 else lines[:1] + lines[-39:]
    return redact("\n".join(lines), 4096)


def group_errors(errors, limit=20):
    """
    Errors -> groups keyed by level, logger, message (ids and numbers aside) and exception
    type: {level, where, message, exception, count, first_at, last_at, sample}, where the
    message, exception and sample are the newest occurrence's. Newest first.
    """
    groups = {}
    for e in errors:
        exception = _exception(e.lines)
        key = (e.level, e.where, _VARYING.sub("#", e.message), exception and exception.split(":", 1)[0])
        group = groups.setdefault(key, {"count": 0, "first_at": e.at, "newest": e, "exception": exception})
        group["count"] += 1
        group["first_at"] = min(group["first_at"], e.at)
        if e.at >= group["newest"].at:
            group |= {"newest": e, "exception": exception}
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["newest"].at)[:limit]:
        e = g["newest"]  # its sample is built once here, however many times the error came
        out.append({"level": e.level, "where": e.where, "message": e.message, "exception": g["exception"],
                    "count": g["count"], "first_at": round(g["first_at"], 3), "last_at": round(e.at, 3),
                    "sample": _sample(e.lines)})
    return out
