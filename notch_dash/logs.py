"""
logs.py — pure readers for the lines notch_dash follows (Caddy's JSON access log, the
metrics JSONL, notch_api's server log, cloudflared's log), and `redact`, which every string
from a log, a header, a path or an error passes through before it is kept.

Log text, paths and user agents come from the internet (public probes): they are data only.
"""

import json
import re
import sys
from collections import namedtuple
from datetime import datetime, timezone

from notch_api.metrics import kind as call_kind

GATE_HOST, LOCAL_HOST, GATE = "notch-gate.localhost", "api.notch.localhost", "/<gate>"

Req = namedtuple("Req", "at kind method path route status ms ua host country ip")  # kind: passed | blocked | local
Call = namedtuple("Call", "at kind model tool status ok ms attempt job job_id usage")
Err = namedtuple("Err", "at level where message lines")  # `lines` grows while a traceback follows it

_KEY = re.compile(r"sk-or-[\w-]+")
_BEARER = re.compile(r"Bearer\s+\S+")
_HEX = re.compile(r"[0-9a-fA-F]{48,}")  # the gate secret's shape
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


def parse_caddy(line, now=None):
    """One access-log line -> Req, or None for other hosts, notch-dash's own requests and junk."""
    try:
        entry = json.loads(line)
        request = entry["request"]
        headers = {k.lower(): v[0] for k, v in (request.get("headers") or {}).items() if isinstance(v, list) and v}
        at, status, ms = float(entry["ts"]), int(entry["status"]), round(float(entry.get("duration") or 0) * 1000, 1)
        host, resp_headers = request.get("host"), {k.lower() for k in entry.get("resp_headers") or {}}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    ua = _text(headers.get("user-agent")) or ""
    if host not in (GATE_HOST, LOCAL_HOST) or ua.startswith("notch-dash/"):
        return None
    method, uri = _text(request.get("method"), 16) or "?", redact(request.get("uri") or "", None)
    if host == LOCAL_HOST:
        kind = "local"
    else:  # passed = Caddy proxied it: the server answered (Via), or wasn't there to
        proxied = "via" in resp_headers or (uri.startswith(GATE + "/") and status in (502, 503, 504))
        kind = "passed" if proxied else "blocked"
    return Req(at, kind, method, redact(uri, 200), route(method, uri), status, ms, sys.intern(ua),
               _text(headers.get("x-forwarded-host")), _text(headers.get("cf-ipcountry"), 8),
               _text(headers.get("cf-connecting-ip"), 64))


def parse_metric(line, now=None):
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
    errors; a traceback extends the error above it, and a line with no time of its own takes
    the last one seen. `started_at` is the last "resumed N unfinished job(s)".
    """

    def __init__(self):
        self.started_at = self._last_at = self._open = None

    def feed(self, line, now):
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
            return self._error(self._last_at or now, m[1], "uvicorn.error", m[2], line) if m[1] in LEVELS else None
        if self._open is not None and len(self._open.lines) < 400:
            self._open.lines.append(redact(line, 1000))
        return None

    def _error(self, at, level, where, message, line):
        self._open = Err(at, level, redact(where, 100), redact(message), [redact(line, 1000)])
        return self._open


_TUNNEL = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z (WRN|ERR) (.*)")
_URL = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)\b")
_FIELDS = re.compile(r'\s+[\w.-]+=(?:"(?:[^"\\]|\\.)*"|\S+)')


class TunnelLog:
    """cloudflared's log: `host` is the latest quick-tunnel address; feed() yields its WRN/ERR lines."""

    def __init__(self):
        self.host = None

    def feed(self, line, now):
        if " INF " in line and (m := _URL.search(line)) and m[1] != "api.trycloudflare.com":
            self.host = m[1]
        elif m := _TUNNEL.match(line):
            at = datetime.strptime(m[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            return Err(at, m[2], None, redact(_FIELDS.sub("", m[3]).strip()), [redact(line, 1000)])
        return None


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
        group = groups.setdefault(key, {"level": e.level, "where": e.where, "count": 0, "first_at": e.at})
        group["count"] += 1
        group["first_at"] = min(group["first_at"], e.at)
        if e.at >= group.get("last_at", e.at):
            group |= {"message": e.message, "exception": exception, "last_at": e.at, "sample": _sample(e.lines)}
    newest = sorted(groups.values(), key=lambda g: -g["last_at"])[:limit]
    return [g | {"first_at": round(g["first_at"], 3), "last_at": round(g["last_at"], 3)} for g in newest]
