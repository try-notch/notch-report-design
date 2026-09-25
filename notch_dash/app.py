"""
app.py — the dashboard's HTTP edge and the sources behind it.

GET only, from an allowed Host on this Mac only, every response uncached under a strict CSP. `/` is the
page, `static/` its two other files (an allow-list), `api/snapshot` the document. Slow
sources are kept current by background threads (Sources.start); a snapshot request reads
their caches and runs one bounded read of the record.
"""

import ipaddress
import logging
import os
import pathlib
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import logs, probes, record, snapshot
from .live import Poller, Tail, run

STATIC = pathlib.Path(__file__).with_name("static")
PAGE_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
              "style.css": "text/css; charset=utf-8"}
HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}
WATCHED_S = 60  # a snapshot served this recently means someone is watching
CADDY_KEEP = 20_000  # Caddy requests kept of each kind, so the internet's probes never push out the phone's
LOG_KEEP = 5_000  # server and tunnel log items kept of each type: a prober can set how fast cloudflared errs
ENV = {"db": "NOTCH_DB", "audio_dir": "NOTCH_AUDIO_DIR", "metrics": "NOTCH_METRICS",
       "caddy_log": "NOTCH_DASH_CADDY_LOG", "server_log": "NOTCH_DASH_SERVER_LOG", "tunnel_log": "NOTCH_DASH_TUNNEL_LOG",
       "tunnel_metrics": "NOTCH_DASH_TUNNEL_METRICS", "gate_secret": "NOTCH_DASH_GATE_SECRET_FILE",
       "openrouter": "OPENROUTER_API_KEY", "device": "NOTCH_DASH_DEVICE"}  # what turns each source on


class Sources:
    """Every source the dashboard reads: a Tail or a Poller each, plus the record, read once per snapshot."""

    def __init__(self, settings, *, http, run, clock):
        s, self.settings, self.clock = settings, settings, clock
        self.watched_at = self.db_read_at = self.db_error = None
        self.server_parser, self.tunnel_parser = logs.ServerLog(), logs.TunnelLog()

        def tail(path, parse, interval, maxlen=None, part=lambda item: None):
            return Tail(path, parse, interval=interval, maxlen=maxlen, part=part, clock=clock)

        def poll(fn, interval, max_age, watched_only=False):
            return Poller(fn, interval=interval, max_age=max_age, watched_only=watched_only, clock=clock)

        self.caddy = tail(s.caddy_log, logs.parse_caddy, 2, CADDY_KEEP, lambda req: req.kind)
        self.metrics = tail(s.metrics, logs.parse_metric, 2, 20_000)
        self.server_log = tail(s.server_log, self.server_parser.feed, 2, LOG_KEEP, type)
        self.tunnel_log = tail(s.tunnel_log, self.tunnel_parser.feed, 5, LOG_KEEP)
        self.health = poll(lambda: probes.local_health(http, s.notch_port), 2, 10)
        self.cloudflared = poll(lambda: probes.cloudflared(http, s.tunnel_metrics), 5, 30)
        self.e2e = poll(lambda: probes.end_to_end(http, self.host(clock()), s.gate_secret_file), 15, 60, True)
        self.integrity = poll(lambda: probes.gate_integrity(http, self.host(clock())), 60, 300, True)
        self.openrouter = poll(lambda: probes.openrouter_spend(http, s.openrouter_key), 60, 600, True)
        self.device = poll(lambda: probes.device(run, s.device), 60, 600, True)
        self.audio = poll(lambda: probes.audio_size(s.audio_dir), 60, 600)
        name = _basename
        self.named = {  # sources.* key -> (tail or poller, configured, where)
            "db": (None, s.db, name(s.db)), "audio_dir": (self.audio, s.audio_dir, name(s.audio_dir)),
            "metrics": (self.metrics, s.metrics, name(s.metrics)),
            "caddy_log": (self.caddy, s.caddy_log, name(s.caddy_log)),
            "server_log": (self.server_log, s.server_log, name(s.server_log)),
            "tunnel_log": (self.tunnel_log, s.tunnel_log, name(s.tunnel_log)),
            "tunnel_metrics": (self.cloudflared, s.tunnel_metrics, s.tunnel_metrics),
            "gate_secret": (self.e2e, s.gate_secret_file, name(s.gate_secret_file)),
            "openrouter": (self.openrouter, s.openrouter_key, "openrouter.ai/api/v1/key"),
            "device": (self.device, s.device, "xcrun devicectl"),
        }
        # (step, interval, watched_only): the local server, every configured source, and last the gate,
        # which needs the host the tunnel sources give
        self.jobs = [(self.health.refresh, self.health.interval, False)]
        for source, configured, _ in self.named.values():
            if isinstance(source, Tail) and configured:
                self.jobs.append((source.tick, source.interval, False))
            elif isinstance(source, Poller) and configured:
                self.jobs.append((source.refresh, source.interval, source.watched_only))
        self.jobs.append((self.integrity.refresh, self.integrity.interval, True))
        self._stop, self._building = threading.Event(), threading.Lock()

    def watching(self):
        return self.watched_at is not None and self.clock() - self.watched_at < WATCHED_S

    def start(self):
        for step, interval, watched_only in self.jobs:
            threading.Thread(target=run, args=(step, interval, self._stop, self.watching if watched_only else None),
                             daemon=True).start()

    def stop(self):
        self._stop.set()

    def host(self, now):
        """The tunnel's public host: cloudflared's own word for it, else the tunnel log's."""
        cf = self.cloudflared.current(now)
        if cf and cf["host"]:
            return cf["host"]
        return self.tunnel_parser.host if self.tunnel_log.read_at and self.tunnel_log.error is None else None

    def read_db(self, now):
        """The record, or None (and why, for sources.db) when it can't be read."""
        if not self.settings.db:
            return None
        audio_dir = self.settings.audio_dir if self.audio.fresh(now) else None
        try:
            db = record.read(self.settings.db, now, audio_dir)
        except sqlite3.Error:
            self.db_error = "it couldn’t be opened read-only"
            return None
        self.db_read_at, self.db_error = now, None
        return db

    def source(self, name, now):
        """One `sources` entry: {state, where, read_at, reason}."""
        obj, configured, where = self.named[name]
        if not configured:
            return {"state": "off", "where": None, "read_at": None,
                    "reason": f"Not set up. Set {ENV[name]} to read it."}
        if obj is None:  # the record: read by this snapshot
            read_at, error, ok = self.db_read_at, self.db_error, self.db_read_at == now and self.db_error is None
        else:  # its last read worked; a value too old to use (a probe paused while nobody watched) is the checks' call
            read_at, error, ok = obj.read_at, obj.error, obj.read_at is not None and obj.error is None
        reason = None if ok else f"Couldn’t read {where}: {error or 'not read yet'}."
        return {"state": "ok" if ok else "unreachable", "where": where, "read_at": snapshot._r3(read_at),
                "reason": reason}

    def snapshot(self, now):
        with self._building:  # one at a time: sources.db reads what this snapshot's read_db left
            self.watched_at, started = now, time.perf_counter()
            return snapshot.build(self, self.read_db(now), now, started)


def _basename(path):
    return path and os.path.basename(path.rstrip("/"))


def _from_this_mac(request):
    """
    Caddy listens on *:80 and sets X-Forwarded-For to the address it was reached from,
    replacing whatever the client sent: anything but loopback there came from another machine.
    """
    try:
        return all(ipaddress.ip_address(ip.strip()).is_loopback
                   for ip in ",".join(request.headers.getlist("x-forwarded-for") or ["127.0.0.1"]).split(","))
    except ValueError:
        return False


def _page_file(name):
    path = STATIC / name
    if name not in PAGE_FILES or not path.is_file():
        return PlainTextResponse("Not here.", 404)
    return FileResponse(path, media_type=PAGE_FILES[name])


def create_app(settings, *, http=None, run=subprocess.run, clock=time.time, start=True):
    # At INFO httpx logs each request's URL, and the public probe's URL holds the gate secret.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    sources = Sources(settings, http=http or httpx.Client(follow_redirects=False), run=run, clock=clock)

    @asynccontextmanager
    async def lifespan(app):
        if start:
            sources.start()
        yield
        sources.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.sources = sources

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in settings.allowed_hosts:
            response = PlainTextResponse("Misdirected request.", 421)
        elif not _from_this_mac(request):
            response = PlainTextResponse("Only this Mac can see it.", 403)
        elif request.method not in ("GET", "HEAD"):
            response = PlainTextResponse("Read only.", 405, headers={"Allow": "GET, HEAD"})
        else:
            response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    @app.api_route("/", methods=["GET", "HEAD"])
    def page():
        return _page_file("index.html")

    @app.api_route("/static/{name}", methods=["GET", "HEAD"])
    def static(name: str):
        return _page_file(name)

    @app.api_route("/api/snapshot", methods=["GET", "HEAD"])
    def api_snapshot():
        return JSONResponse(sources.snapshot(clock()))

    return app
