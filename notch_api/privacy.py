"""
privacy.py — nothing a person said reaches a log line, stdout or a stack trace.

ONE ALLOWLISTED JSON LINE PER REQUEST. Guard, the outermost ASGI layer, gives every
request an id (echoed as X-Request-Id) and, when it ends, logs one line holding only the
keys in ALLOWED: the route's template (never the raw path), kind, status, error code,
latency, sizes, model, provider, attempt and versions. A value is written only if it is
a number, a bool or a short string of plain characters; anything else is dropped, so
even a provider name the server did not expect cannot carry text through.

EVERY OTHER RECORD IS SCRUBBED. ScrubbedFormatter, installed on the root, `uvicorn` and
`uvicorn.error` loggers, writes a record's level, logger and its message TEMPLATE (the
format string, never its arguments) when the logger is this server's or uvicorn's, and
only "log" for anyone else's. An exception is its type and file:line:function frames:
no message, no locals, no chained exception's message. httpx and httpcore are held at
WARNING (at INFO they log URLs, and a Supabase admin URL names a user), and uvicorn's
access log is off.

A CRASH IS ANSWERED HERE. Starlette's ServerErrorMiddleware sends the 500 and then
re-raises, and uvicorn would print that traceback with the exception's message. Guard
catches whatever comes up, logs it scrubbed, answers the 500 envelope itself if no
response has started, and never lets it reach uvicorn.
"""

import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

from .wire_v2 import Refusal

ALLOWED = ("event", "request_id", "route", "method", "kind", "status", "error_code", "latency_ms", "request_bytes",
           "response_bytes", "audio_seconds", "chunks", "entry_count", "input_chars", "model", "provider", "attempt",
           "config_version", "prompt_version", "app_version", "platform", "alert", "zdr", "count")
_PLAIN = re.compile(r"[A-Za-z0-9_./:@+{}, -]{0,160}")
_TEMPLATE = re.compile(r"[\x20-\x7e]{0,200}")   # a message template from code: printable ASCII
_TRUSTED = ("notch_api", "notch_dash", "uvicorn")

request_log = logging.getLogger("notch_api.request")
alert_log = logging.getLogger("notch_api.alert")


def _plain(value):
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and _PLAIN.fullmatch(value) is not None


def fields(values):
    """The allowlisted, plain part of `values`."""
    return {k: v for k, v in values.items() if k in ALLOWED and _plain(v)}


def frames(tb):
    """A traceback as file:line:function, with no source text, no locals and no message."""
    return [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_tb(tb)][-12:]


class ScrubbedFormatter(logging.Formatter):
    def format(self, record):
        stamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
        line = {"ts": stamp.replace("+00:00", "Z"), "level": record.levelname, "logger": record.name}
        notch = getattr(record, "notch", None)
        if isinstance(notch, dict):
            line.update(fields(notch))
        trusted = record.name.split(".")[0] in _TRUSTED
        template = record.msg if trusted and isinstance(record.msg, str) and _TEMPLATE.fullmatch(record.msg) else None
        line.setdefault("event", template or "log")
        if record.exc_info and record.exc_info[0] is not None:
            line["exc_type"] = record.exc_info[0].__name__
            line["frames"] = frames(record.exc_info[2])
        return json.dumps(line, ensure_ascii=True)


def install_logging(level=logging.INFO, stream=None):
    """Scrubbed JSON lines on stderr (journald on the VPS) for this server, uvicorn, and everything else."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(ScrubbedFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level)
    access = logging.getLogger("uvicorn.access")
    access.handlers, access.propagate, access.disabled = [], False, True
    for name in ("httpx", "httpx2", "httpcore"):   # at INFO they log every URL
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def alert(name, **values):
    """An alert line (ZDR miss, config switched off): an ERROR on notch_api.alert, allowlisted like the rest."""
    alert_log.error(name, extra={"notch": {"event": "alert", "alert": name, **values}})


def request_entry(scope):
    """The request's log fields, which a route fills in (kind, attempt, sizes, versions...)."""
    return scope.setdefault("state", {}).setdefault("notch_log", {})


class Guard:
    """
    The outermost ASGI layer: request ids, the one log line per request, and the last word on
    any exception. `on_request`, when given, is handed each finished request's entry (the log
    line's fields, before the allowlist): refusals.py counts the refused ones from it.
    """

    def __init__(self, app, on_request=None):
        self.app, self.on_request = app, on_request

    def __getattr__(self, name):
        return getattr(self.app, name)   # .state, .routes... for whoever holds the app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        began, request_id = time.perf_counter(), str(uuid.uuid4())
        entry = request_entry(scope)
        entry.update(request_id=request_id, method=scope.get("method"))
        response = {"status": None, "bytes": 0}

        async def tracked(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                message = {**message, "headers": [*message.get("headers", []),
                                                   (b"x-request-id", request_id.encode())]}
            elif message["type"] == "http.response.body":
                response["bytes"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception as exc:  # noqa: BLE001 — nothing may reach uvicorn, which would print the message
            logging.getLogger("notch_api.guard").error(
                "unhandled_exception", exc_info=(type(exc), exc, exc.__traceback__),
                extra={"notch": {"event": "unhandled_exception", "request_id": request_id}})
            if response["status"] is None:
                refusal = Refusal("internal_error")
                entry.setdefault("error_code", refusal.code)
                body = json.dumps(refusal.body()).encode()
                await tracked({"type": "http.response.start", "status": 500,
                               "headers": [(b"content-type", b"application/json"),
                                           (b"content-length", str(len(body)).encode())]})
                await tracked({"type": "http.response.body", "body": body})
        finally:
            route = scope.get("route")
            entry.setdefault("route", getattr(route, "path", None) or "unmatched")
            entry.update(status=response["status"], response_bytes=response["bytes"],
                         latency_ms=round((time.perf_counter() - began) * 1000, 1))
            request_log.info("request", extra={"notch": {"event": "request", **entry}})
            if self.on_request is not None:
                self.on_request(entry)
