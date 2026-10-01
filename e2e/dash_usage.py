"""
dash_usage.py — the usage page end to end: phones -> the /v2 API -> the meter -> notch_dash -> a browser.

    .venv/bin/python e2e/dash_usage.py            # offline, ~20 s; writes e2e/runs/dash-usage-<stamp>/report.html
    .venv/bin/python e2e/dash_usage.py --serve    # the same, then leaves the page up to look at

It serves the real API and the real dashboard on two local ports, sharing one meter file and one
clock the run moves by hand, so five days of use fit in seconds. Phones are plain HTTP calls with
tokens signed for the run; the models are notch_api.fakes. The dashboard is built from compose.yaml's
own `dash` environment, so what is checked is what the VPS runs. Then a headless Chrome loads the
page through the dashboard's own server (so its Content-Security-Policy applies) and the run reads
the DOM and takes screenshots. A second, shorter pass starts `python -m notch_api` and
`python -m notch_dash` as the container does, to prove the entry points and the shutdown write.
tests/test_dash_usage_e2e.py runs the first pass, without the screenshots, as part of the suite.

The story mirrors the first week of real use: one person on seven builds in five days, five
notches, two reports, one rewrite, a second person who only opened the app, a config push that
turned the writing check on, and then everything that can turn a phone away.

EVERY WAY THIS CAN GO WRONG, written before the code. Each is a check below, by its number.

 The API and the meter
  F1  An answer refused before check-and-start leaves no trace, so the page says nothing failed.
  F2  A refusal the meter already wrote a row for is counted a second time.
  F3  Probes at paths that do not exist fill the problems table, or grow the database without bound.
  F4  Counting a refusal changes the answer the phone gets.
  F5  The new table or the row-level view carries content or an identifier: a transcript, a path,
      a whole account id, an Idempotency-Key.
  F6  Counts held in memory are lost when the server stops.
  F7  The dashboard cannot read a meter from before this change (no `refusals` table, no index).
  F8  A meter from before this change cannot be opened by the new server.

 The numbers
  F9  An account is counted under every build it ran, so versions add up to more than the accounts.
  F10 Spend is rounded to cents, or the 7-day total comes out above the all-time one.
  F11 The cost of a notch includes reports and rewrites.
  F12 Just after UTC midnight "the last 24 hours" is empty though a notch was made an hour ago.
  F13 A call from 25 hours ago is counted in the last 24 hours.
  F14 One model is split into a row per provider list.
  F15 A p95 is shown for a handful of calls.
  F16 Calls are not split by prompt version, or the versions are mixed.
  F17 Recent calls are out of order, or a call that never finished breaks the list.
  F18 A failed or refused call is missing from the problems table or the recent list.
  F19 Zero-retention verdicts of `unknown` or `miss` are shown as an ordinary row.
  F20 The accounts table shows a whole id, or the wrong build, notches or spend.
  F21 A deleted account leaves the totals inconsistent.
  F22 The day table shows two weeks of zeros, hides a gap, or drops rewrites.
  F23 A call past its deadline and still in flight goes unmentioned.
  F24 The server card never refreshes, because only the health page counted as someone watching.
  F25 The link to the health page is shown where that page has nothing to show.
  F26 Watching the usage page makes the dashboard knock on /healthz every two seconds.

 Found by a second reader, after the first version
  F35 A 404 that is a real route's own answer (Notch Cloud's keycheck before a key is set up) is
      shown as a probe, or as a problem.
  F36 The meter's file cannot be opened when the counter writes: the batch is lost, or stopping
      the server raises and the rest of shutdown never runs.

 The page
  F27 An empty meter breaks the page.
  F28 A provider's name reaches the DOM as markup.
  F29 The page needs an inline style or script the Content-Security-Policy refuses.
  F30 Times are shown in UTC instead of the viewer's zone, or the cap's reset time is wrong.
  F31 Dark mode is not what the screenshot shows.
  F32 Narrow tables fall into the phone layout on a desktop, or a phone has to scroll sideways.
      (Looked at in the screenshots; the run cannot measure it.)
  F33 The page and the server disagree on the document's version after a deploy.
      (One comparison in usage.js; read, not run.)

 The container's way of starting
  F34 `python -m notch_api` or `python -m notch_dash` does not start with compose.yaml's environment.
"""

import argparse
import contextlib
import datetime as dt
import glob
import html
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from notch_api import privacy  # noqa: E402
from notch_api.app import create_app  # noqa: E402
from notch_api.auth import Verifier  # noqa: E402
from notch_api.fakes import (  # noqa: E402
    FakeApple, FakeAudio, FakeClient, FakeSupabaseAdmin, _BoundFake, fake_recording)
from notch_api.meter import Meter  # noqa: E402
from notch_api.openrouter import ModelUnavailable  # noqa: E402
from notch_api.refusals import RefusalCounter  # noqa: E402
from notch_api.services import Services  # noqa: E402
from notch_api.zdr import ZdrAuditor  # noqa: E402
from notch_dash import app as dash_app, usage as dash_usage  # noqa: E402
from notch_dash.settings import Settings  # noqa: E402
from tests.v2kit import SUPABASE_URL, JWKSStub, SigningKey, WallClock, token  # noqa: E402

UTC = dt.timezone.utc
VIEWER_ZONE = "America/New_York"          # the browser's zone: UTC-4 on these dates
CANARY = "saffron walrus retrospective"   # said in every notch; must never reach the dashboard
A = "aaaaaaaa-1111-4111-8111-111111111111"   # five notches on seven builds
B = "bbbbbbbb-2222-4222-8222-222222222222"   # opened the app, never notched
C = "cccccccc-3333-4333-8333-333333333333"   # twenty notches in a day (part two)
D = "dddddddd-4444-4444-8444-444444444444"   # one notch, then deleted (part two)
WHISPER, CHAT = "openai/whisper-large-v3", "deepseek/deepseek-v4-pro-0813"
OPENROUTER_KEY = {"limit": 50, "limit_remaining": 49.5, "usage": 0.5, "usage_daily": 0.01, "usage_weekly": 0.2,
                  "usage_monthly": 0.5, "is_free_tier": False}
PROJECTS = ["Checkout Rewrite", CANARY.title()]


def at(month, day, hour, minute=0, second=0):
    return dt.datetime(2026, month, day, hour, minute, second, tzinfo=UTC).timestamp()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def usd(value):
    """The page's money format (DASHBOARD.md › Formats): four decimals under a dollar."""
    if value is None:
        return "—"
    if value == 0:
        return "$0"
    if value < 0.0001:
        return "<$0.0001"
    return f"${value:,.2f}" if value >= 1 else f"${value:.4f}"


# ---------------------------------------------------------------------------
# The models: notch_api.fakes, told how long each call takes and who serves it.
# ---------------------------------------------------------------------------

class _Bound(_BoundFake):
    def tool_call(self, **kwargs):
        if kwargs.get("tool_name") != "check_notch":
            return super().tool_call(**kwargs)
        # The writing check, which FakeClient has no answer for: nothing to fix.
        return self._call("tool_call", "chat", self._models.get("chat", CHAT), self._fake.chat_provider,
                          lambda: kwargs["parse"]({"fixes": []}),
                          lambda _: {"prompt_tokens": 100, "completion_tokens": 50})

    def within(self, seconds):
        return self


class ScriptedFake(FakeClient):
    """
    FakeClient on the run's clock. `took` seconds pass during the next request's first
    model call, so the meter sees that duration; `serving` names the chat providers of
    the next calls, in order, then it is "Wafer" again.
    """

    def __init__(self, clock):
        self._lock, self._serving, self.clock, self.took = threading.Lock(), [], clock, 0.0
        super().__init__(stt_provider=None)   # OpenRouter does not say who transcribed: ZDR `unknown`

    @property
    def chat_provider(self):
        with self._lock:
            return self._serving.pop(0) if self._serving else "Wafer"

    @chat_provider.setter
    def chat_provider(self, value):
        pass

    def serving(self, *providers):
        with self._lock:
            self._serving = list(providers)

    def bound(self, *, deadline=None, usage=None, models=None, provider=None):
        return _Bound(self, deadline, usage, models or {}, provider)

    def _record(self, method, **kwargs):
        with self._lock:
            took, self.took = self.took, 0.0
        self.clock.advance(took)
        super()._record(method, **kwargs)


class Counted:
    """An ASGI wrapper that counts requests by path, to see how often the dashboard asks /healthz."""

    def __init__(self, app):
        self.app, self.paths = app, {}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.paths[scope["path"]] = self.paths.get(scope["path"], 0) + 1
        await self.app(scope, receive, send)


class Served:
    """An ASGI app on 127.0.0.1:`port`, on a thread, until the with block ends."""

    def __init__(self, app, port):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                                    access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise SystemExit("a server did not start")
            time.sleep(0.02)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)

    def __exit__(self, *exc):
        self.stop()


def compose_dash_environment():
    """compose.yaml's `dash` service environment, with each ${VAR:-default} as its default."""
    env, inside = {}, None
    for line in open(os.path.join(HERE, "compose.yaml"), encoding="utf-8"):
        indent, text = len(line) - len(line.lstrip(" ")), line.strip()
        if indent == 2 and text.endswith(":"):
            inside = "service" if text == "dash:" else None
        elif inside and indent == 4:
            inside = "environment" if text == "environment:" else "service"
        elif inside == "environment" and indent == 6 and text and not text.startswith("#"):
            name, _, value = text.partition(":")
            value = re.sub(r"\s+#.*$", "", value).strip().strip('"')
            env[name.strip()] = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", value)
    if "NOTCH_DASH_HOME" not in env:
        raise SystemExit("compose.yaml's dash environment could not be read")
    return env


# ---------------------------------------------------------------------------
# The stack and the phones.
# ---------------------------------------------------------------------------

class Stack:
    def __init__(self, work):
        self.work, self.clock = work, WallClock(at(9, 27, 14, 0))
        self.fake = ScriptedFake(self.clock)
        self.signing = SigningKey("e2e")
        self.meter_db = os.path.join(work, "meter.db")
        self.services = Services.build(
            meter_db=self.meter_db, tmp_root=os.path.join(work, "tmpfs"), client=self.fake, audio=FakeAudio(),
            verifier=Verifier(SUPABASE_URL, http=JWKSStub(self.signing).http, cooldown=0), clock=self.clock,
            apple=FakeApple(), supabase_admin=FakeSupabaseAdmin())
        self.services.zdr = ZdrAuditor(self.fake, self.services.meter, self.services.remote, inline=True, waits=(0,),
                                       sleep=lambda seconds: None)
        self.api_port, self.dash_port = free_port(), free_port()
        self.api_app = Counted(create_app(services=self.services, v1=False))
        self.dash_app = self.dashboard(self.dash_port)
        self.settings = self.dash_app.state.sources.settings
        self.api = f"http://127.0.0.1:{self.api_port}"
        self.dash = f"http://127.0.0.1:{self.dash_port}"
        self.http = httpx.Client(timeout=30)
        self.ledger, self.keys = [], []
        self._real = httpx.HTTPTransport()

    def dashboard(self, port, start=True):
        """notch_dash for `port`, from compose.yaml's environment; `start=False` leaves its probes asleep."""
        settings = Settings.from_env(compose_dash_environment() | {
            "NOTCH_DASH_BIND": "127.0.0.1", "NOTCH_DASH_PORT": str(port), "NOTCH_PORT": str(self.api_port),
            "NOTCH_DASH_METER_DB": self.meter_db, "NOTCH_METRICS": os.path.join(self.work, "metrics.jsonl"),
            "OPENROUTER_API_KEY": "e2e-not-a-key"})
        return dash_app.create_app(settings, http=httpx.Client(transport=httpx.MockTransport(self._outbound)),
                                   clock=self.clock, start=start)

    def _outbound(self, request):
        """The dashboard's own HTTP: OpenRouter's key endpoint is canned, the API's /healthz is real."""
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json={"data": OPENROUTER_KEY})
        return self._real.handle_request(request)

    # -- one phone call ---------------------------------------------------------

    def call(self, method, path, *, user=A, build="1.0.6+6", json_body=None, content=None, headers=None,
             expect=200, took=0.0, cost=0.0, serving=(), request_key=None, auth=True, kind=None):
        """One request -> its response. A processing call is also written to the ledger, as the phone saw it."""
        sent = {"X-Client": f"ios/{build}"}
        if auth:
            sent["Authorization"] = f"Bearer {token(self.signing, sub=user)}"
        if method != "GET":
            request_key = request_key or str(uuid.uuid4())
            sent["Idempotency-Key"] = request_key
            self.keys.append(request_key)
        sent.update(headers or {})
        self.fake.took, self.fake.cost = took, cost
        self.fake.serving(*serving)
        answered_before, started_at = len(self.fake.generations), self.clock()
        response = self.http.request(method, self.api + path, json=json_body, content=content, headers=sent)
        if response.status_code != expect:
            raise SystemExit(f"{method} {path} answered {response.status_code}, not {expect}: {response.text[:300]}")
        if kind:
            body = response.json() if response.content else {}
            self.ledger.append({
                "at": started_at, "user": user, "kind": kind, "build": build.split("+")[0],
                "status": "ok" if expect == 200 else "refused" if expect in (409, 429) else "failed",
                "code": None if expect == 200 else body["error"]["code"], "took": took,
                "cost": (len(self.fake.generations) - answered_before) * cost,
                "prompt": body.get("prompt_version"), "key": request_key})
        return response

    def config(self, user, build):
        return self.call("GET", "/v2/config", user=user, build=build)

    def notch(self, user, build, *, words, heard_in, written_in, serving=()):
        """One notch of `words` words: the fake hears 0.4 s a word, so 38 words are a 15.2 s recording."""
        said = f"Shipped the form. Then the {CANARY}."
        text = " ".join([said, *["and"] * (words - len(said.split()))])
        recording = fake_recording(text, seconds=round(0.4 * words, 2))
        self.call("POST", "/v2/transcribe", user=user, build=build, content=recording, took=heard_in, cost=0.00016,
                  kind="transcribe", headers={"Content-Type": "audio/mp4", "X-Notch-Mode": "daily",
                                              "X-Notch-Duration": str(int(0.4 * words))})
        self.clock.advance(1)
        return self.call("POST", "/v2/analyze", user=user, build=build, took=written_in, cost=0.0012, kind="analyze",
                         serving=serving, json_body={"transcript": text, "project_names": PROJECTS, "vocabulary": []})

    def report(self, user, build, *, took, entries):
        body = {"type": "week", "range_start": "2026-09-21", "range_end": "2026-09-27", "range_label": "Sep 21–27",
                "scope": {}, "author": {"display_name": CANARY}, "project_names": PROJECTS,
                "entries": [{"id": f"e{n}", "date": "2026-09-27", "project_name": PROJECTS[0], "tags": ["shipped"],
                             "categories": ["wins"], "is_milestone": False, "summary": f"About the {CANARY}.",
                             "takeaways": [CANARY], "impact_note": None, "acknowledged_by": None,
                             "transcript": CANARY} for n in range(entries)]}
        return self.call("POST", "/v2/reports", user=user, build=build, json_body=body, took=took, cost=0.0027,
                         kind="reports")

    def push(self, overrides, note):
        self.services.remote.push(overrides, note=note, created_by="e2e")

    # -- reading the dashboard --------------------------------------------------

    def usage(self, dash=None):
        """GET /api/usage, past the dashboard's ten-second cache."""
        self.clock.advance(11)
        response = self.http.get((dash or self.dash) + "/api/usage")
        response.raise_for_status()
        return response.json(), response.text


def story(s):
    """Sep 27 to Sep 30, as the first week went."""
    c, v5 = s.clock, {"prompts": {"analyze": "v5", "takeaways": "v5", "check": "c1"}}
    c.now = at(9, 27, 14, 0)
    s.config(A, "1.0.0+1")
    c.now = at(9, 27, 14, 5)
    s.notch(A, "1.0.0+1", words=35, heard_in=2.4, written_in=1.6)
    c.now = at(9, 27, 16, 30)
    s.config(A, "1.0.1+2")
    s.notch(A, "1.0.1+2", words=38, heard_in=2.7, written_in=1.7)
    c.now = at(9, 27, 21, 10)
    s.notch(A, "1.0.2+3", words=40, heard_in=3.1, written_in=1.5)
    c.now = at(9, 27, 21, 20)
    s.report(A, "1.0.2+3", took=5.2, entries=3)

    c.now = at(9, 28, 15, 0)
    s.config(A, "1.0.3+4")
    s.notch(A, "1.0.3+4", words=25, heard_in=2.2, written_in=10.2)
    c.now = at(9, 28, 15, 10)
    s.report(A, "1.0.4+5", took=3.2, entries=4)

    c.now = at(9, 29, 19, 30)
    s.push(v5, "writing v5, with the check")

    c.now = at(9, 30, 22, 20)
    s.config(B, "1.0.6+6")
    c.now = at(9, 30, 22, 30)
    s.config(A, "1.0.6+6")
    analyzed = s.notch(A, "1.0.6+6", words=38, heard_in=3.7, written_in=13.8,
                       serving=("Ionstream", "Wafer", "Ionstream"))
    c.now = at(9, 30, 22, 40)
    transcript = f"Shipped the form. Then the {CANARY}."
    s.call("POST", "/v2/takeaways", json_body={"transcript": transcript, "project_names": PROJECTS, "vocabulary": []},
           took=3.5, cost=0.0003, serving=("Ionstream", "Ionstream"), kind="takeaways")
    return analyzed


def trouble(s):
    """Sep 30, 22:50 UTC: every way a phone is turned away, one of each kind the page tells apart."""
    c = s.clock
    c.now = at(9, 30, 22, 50)
    body = {"transcript": f"A second look at the {CANARY}.", "project_names": PROJECTS, "vocabulary": []}
    audio = {"Content-Type": "audio/mp4", "X-Notch-Mode": "daily", "X-Notch-Duration": "5"}
    # It started and failed: the model service was down.
    s.fake.fail_with = ModelUnavailable("down")
    s.call("POST", "/v2/analyze", json_body=body, expect=502, kind="analyze")
    s.fake.fail_with = None
    # The meter refused it: the last write-up's key, with another body.
    reused = next(row["key"] for row in reversed(s.ledger) if row["kind"] == "analyze" and row["status"] == "ok")
    s.call("POST", "/v2/analyze", json_body=body, expect=409, request_key=reused, kind="analyze")
    # Turned away before the meter: no token (twice), an old build, not audio, unreadable audio, a switch.
    answers = [s.call("POST", "/v2/analyze", json_body=body, expect=401, auth=False) for _ in range(2)]
    answers.append(s.call("POST", "/v2/transcribe", build="0.9.0+1", content=fake_recording("Hello."), expect=426,
                          headers=audio))
    answers.append(s.call("POST", "/v2/transcribe", content=b"plain words", expect=415,
                          headers=audio | {"Content-Type": "text/plain"}))
    answers.append(s.call("POST", "/v2/transcribe", content=CANARY.encode() * 20, expect=422, headers=audio))
    s.push({"prompts": {"analyze": "v5", "takeaways": "v5", "check": "c1"}, "features": {"takeaways": False}},
           "rewrites off")
    answers.append(s.call("POST", "/v2/takeaways", json_body=body, expect=503))
    s.push({"prompts": {"analyze": "v5", "takeaways": "v5", "check": "c1"}}, "rewrites back on")
    # Not a refusal at all: Notch Cloud's keycheck answers 404 until the account sets a key up.
    answers.append(s.call("GET", "/v2/cloud/keycheck", expect=404))
    # Probes: paths that do not exist, and a method a real path does not take.
    for method, path, status in (("GET", "/wp-login.php", 404), ("GET", "/.env", 404),
                                 ("POST", f"/v2/{CANARY.replace(' ', '-')}", 404), ("GET", "/v2/transcribe", 405)):
        answers.append(s.call(method, path, expect=status, auth=False))
    # A call the process never settled: its row stays in flight past its deadline.
    c.now = at(9, 30, 22, 55)
    s.services.meter.start(user_id=A, kind="reports", key=str(uuid.uuid4()), body_hmac=b"x" * 32,
                           deadline_seconds=241, config=s.services.remote.current(),
                           versions={"app_version": "1.0.6", "platform": "ios", "prompt_version": "r1",
                                     "config_version": s.services.remote.current().version})
    return answers


# ---------------------------------------------------------------------------
# The browser.
# ---------------------------------------------------------------------------

def find_chrome():
    found = os.environ.get("CHROME")
    shells = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell")) + glob.glob(
        os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell")))
    candidates = [found, *reversed(shells), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome")]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def chrome(binary, url, *, width, height, dom=False, shot=None, dark=False):
    """Load `url` -> (the DOM once the page has rendered, what the browser logged)."""
    args = [binary, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--enable-logging=stderr",
            "--v=0", f"--window-size={width},{height}", "--virtual-time-budget=5000"]
    # --force-dark-mode leaves prefers-color-scheme light; this is what flips it.
    args += ["--blink-settings=preferredColorScheme=0"] if dark else []
    args += ["--force-device-scale-factor=2", f"--screenshot={shot}"] if shot else []
    args += ["--dump-dom"] if dom else []
    done = subprocess.run(args + [url], capture_output=True, text=True, timeout=90,
                          env=dict(os.environ, TZ=VIEWER_ZONE))
    return done.stdout, done.stderr


def png_pixel(path, x, y):
    """(r, g, b) at (x, y) of an 8-bit RGB or RGBA PNG, with only the standard library."""
    data = open(path, "rb").read()
    offset, chunks, width, channels = 8, [], None, None
    while offset < len(data):
        length, name = struct.unpack(">I4s", data[offset:offset + 8])
        body = data[offset + 8:offset + 8 + length]
        if name == b"IHDR":
            width, _, depth, colour = struct.unpack(">IIBB", body[:10])
            channels = {2: 3, 6: 4}[colour]
            assert depth == 8
        elif name == b"IDAT":
            chunks.append(body)
        offset += 12 + length
    raw, stride = zlib.decompress(b"".join(chunks)), width * channels
    previous = bytearray(stride)
    for row in range(y + 1):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up, corner = previous[i], previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                p = left + up - corner
                nearest = min((abs(p - left), 0, left), (abs(p - up), 1, up), (abs(p - corner), 2, corner))[2]
                line[i] = (line[i] + nearest) & 255
        previous = line
    return tuple(previous[x * channels:x * channels + 3])


def part(dom, element_id):
    """The text of the element with this id, tags stripped: enough to say what a section shows."""
    match = re.search(rf'<(\w+)[^>]*\bid="{re.escape(element_id)}"[^>]*>', dom)
    if not match:
        return ""
    tag, depth, position = match.group(1), 1, match.end()
    for step in re.finditer(rf"<(/?){tag}\b[^>]*>", dom[position:]):
        depth += -1 if step.group(1) else 1
        if depth == 0:
            inner = dom[position:position + step.start()]
            return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner))).strip()
    return ""


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.rows = []

    def __call__(self, number, what, passed, detail=""):
        self.rows.append((number, what, "PASS" if passed else "FAIL", str(detail)))
        found = f"  [{detail}]" if detail and not passed else ""
        print(f"  {'ok  ' if passed else 'FAIL'} {number:<4} {what}{found}")

    def skip(self, number, what, why):
        self.rows.append((number, what, "SKIP", why))
        print(f"  skip {number:<4} {what}  [{why}]")

    @property
    def failed(self):
        return [row for row in self.rows if row[2] == "FAIL"]


@contextlib.contextmanager
def server_logs(path):
    """The servers' log lines go to `path`, scrubbed as in production, instead of the terminal."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(privacy.ScrubbedFormatter())
    loggers = [logging.getLogger(name) for name in ("notch_api", "notch_dash")]
    saved = [(logger.handlers[:], logger.propagate, logger.level) for logger in loggers]
    for logger in loggers:
        logger.handlers, logger.propagate = [handler], False
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, (handlers, propagate, level) in zip(loggers, saved):
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)
        handler.close()


def close(a, b, tolerance=1e-6):
    return a is not None and b is not None and abs(a - b) <= tolerance


def median(values):
    return statistics.median(values) if values else None


def run(out, *, browser=None, serve=False, quick=False):
    """
    The whole run -> the Checks. `out` gets report.html, the JSON documents, the DOM and the
    screenshots. `quick` leaves out the screenshots and the pass through the entry points.
    """
    os.makedirs(out, exist_ok=True)
    check, work = Checks(), os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)
    browser = browser or find_chrome()
    s = Stack(work)
    shots = []
    with server_logs(os.path.join(work, "server.log")), Served(s.api_app, s.api_port) as api, \
            Served(s.dash_app, s.dash_port):
        # -- an empty meter ------------------------------------------------------
        # On a second dashboard whose probes never start. The one under test must first be watched on
        # Oct 1: a probe that ran now, on Sep 27's clock, would not be due again for a real minute.
        print("an empty meter")
        port = free_port()
        with Served(s.dashboard(port, start=False), port):
            quiet = f"http://127.0.0.1:{port}"
            empty, _ = s.usage(quiet)
            check("F27", "an empty meter answers a whole document", empty.get("v") == 2 and empty["recent"] == []
                  and empty["daily"] == [] and empty["week"]["notch_cost_usd"] is None, empty.get("error"))
            if browser:
                dom, logged = chrome(browser, quiet + "/", width=1280, height=1600, dom=True)
                check("F27", "the page renders an empty meter",
                      "Nothing yet" in dom and "Reading…" not in part(dom, "fresh"), part(dom, "fresh"))

        # -- the first week, then trouble ------------------------------------------
        print("five days of use, then every refusal")
        story(s)
        refused = trouble(s)
        check("F4", "a refusal the counter saw is still the envelope, and nothing else",
              all(set(r.json()) == {"error"} and set(r.json()["error"]) <= {"code", "message", "retryable"}
                  for r in refused), [r.status_code for r in refused])
        s.services.refusals.flush()
        s.clock.now = at(10, 1, 0, 49, 40)     # 8:49 PM in New York, 49 minutes into the UTC day
        s.usage()                              # someone is watching: the health and spend probes start
        probes, patience = s.dash_app.state.sources, time.monotonic() + 10
        while not (probes.api.fresh(s.clock()) and probes.openrouter.fresh(s.clock())) and time.monotonic() < patience:
            time.sleep(0.1)   # both probes last ran on Sep 27, for the empty page: wait for today's
        u, text = s.usage()
        with open(os.path.join(out, "usage.json"), "w", encoding="utf-8") as f:
            json.dump(u, f, indent=1)
        numbers(check, s, u, text)

        # -- the page ---------------------------------------------------------------
        if browser:
            print("the page")
            dom, logged = chrome(browser, s.dash + "/", width=1280, height=2400, dom=True)
            with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as f:
                f.write(dom)
            page(check, s, u, dom, logged)
            for name, width, height, dark in (() if quick else (
                    ("desktop-light", 1280, 3300, False), ("desktop-dark", 1280, 3300, True),
                    ("phone-light", 390, 7400, False))):
                path = os.path.join(out, f"{name}.png")
                chrome(browser, s.dash + "/", width=width, height=height, shot=path, dark=dark)
                shots.append(f"{name}.png")
            if shots:
                light, dark = (png_pixel(os.path.join(out, f"desktop-{m}.png"), 6, 400) for m in ("light", "dark"))
                check("F31", "the dark screenshot is dark: paper is #1C1712, not #FBF6EC",
                      light == (0xFB, 0xF6, 0xEC) and dark == (0x1C, 0x17, 0x12), f"light {light}, dark {dark}")
        else:
            for number in ("F27", "F28", "F29", "F30", "F31"):
                check.skip(number, "the page in a browser", "no Chrome found; set CHROME=/path/to/chrome")

        # -- part two: a busy day, a deleted account, an old meter ------------------
        print("a busy day, a deleted account, an old meter")
        part_two(check, s, browser, out, shot=not quick)
        healthz = s.api_app.paths.get("/healthz", 0)
        check("F26", "the dashboard asked /healthz a few times, not every two seconds", 1 <= healthz <= 6, healthz)

        if serve:
            print(f"\nThe page is up at {s.dash}/  (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        # -- stopping the server writes what it held ---------------------------------
        s.call("POST", "/v2/analyze", json_body={"transcript": "x"}, expect=401, auth=False)
        api.stop()
        with sqlite3.connect(s.meter_db) as db:
            held = db.execute("SELECT sum(calls) FROM refusals WHERE code = 'unauthorized'").fetchone()[0]
        check("F6", "a refusal counted just before shutdown is in the meter", held == 3, held)

    if not quick:
        print("the container's way of starting")
        as_the_container_starts(check, os.path.join(work, "container"))
    write_report(out, check, shots)
    return check


def numbers(check, s, u, text):
    """/api/usage against the ledger: what the phones were answered, set beside what the dashboard says."""
    ledger, now = s.ledger, s.clock()
    ok = [row for row in ledger if row["status"] == "ok"]
    spend = sum(row["cost"] for row in ledger)
    notches = [row for row in ok if row["kind"] == "transcribe"]
    day_ago = [row for row in ledger if row["at"] >= now - 86400]
    problems = {(p["where"], p["outcome"], p["code"]): p for p in u["problems"]}

    check("F1", "each early refusal is on the page with its code and count",
          {key: p["calls"] for key, p in problems.items() if p["outcome"] == "turned_away"} == {
              ("/v2/analyze", "turned_away", "unauthorized"): 2,
              ("/v2/transcribe", "turned_away", "app_update_required"): 1,
              ("/v2/transcribe", "turned_away", "unsupported_audio"): 1,
              ("/v2/transcribe", "turned_away", "audio_unreadable"): 1,
              ("/v2/takeaways", "turned_away", "feature_disabled"): 1}, sorted(problems))
    check("F2", "the meter's own refusal is counted once",
          problems.get(("analyze", "refused", "idempotency_key_reused"), {}).get("calls") == 1
          and ("/v2/analyze", "turned_away", "idempotency_key_reused") not in problems, sorted(problems))
    with sqlite3.connect(s.meter_db) as db:
        rows = db.execute("SELECT route, status, code, calls FROM refusals ORDER BY route, code").fetchall()
    check("F3", "probes are one count on the page, and one row each in the meter",
          u["probes_7d"] == 4 and not any(p["code"] in ("not_found", "method_not_allowed") for p in u["problems"])
          and [r for r in rows if r[0] == "unmatched" or r[1] == 405] == [
              ("/v2/transcribe", 405, "method_not_allowed", 1), ("unmatched", 404, "not_found", 3)], rows)
    check("F35", "a real route's 404 is counted in the meter, and is neither a probe nor a problem on the page",
          ("/v2/cloud/keycheck", 404, "not_found", 1) in rows and u["probes_7d"] == 4
          and not [p for p in u["problems"] if p["code"] == "not_found" or p["where"].startswith("/v2/cloud")]
          and "not found" not in " ".join(a["text"] for a in u["attention"]), sorted(problems))
    leaks = [name for name, needle in (("the canary", CANARY), ("a canary word", "walrus"), ("account A", A),
                                       ("account B", B), *((f"key {k[:8]}", k) for k in s.keys))
             if needle.lower() in text.lower() or needle.lower() in json.dumps(rows).lower()]
    check("F5", "no content, path, whole account id or Idempotency-Key is in the document or the table", not leaks,
          leaks)
    check("F18", "the failed call and the refused one are in the problems table",
          problems.get(("analyze", "failed", "model_unavailable"), {}).get("calls") == 1
          and problems[("analyze", "failed", "model_unavailable")]["accounts"] == 1, sorted(problems))

    versions = u["versions"]
    check("F9", "two accounts, one row: each under its latest build",
          versions == [{"platform": "ios", "app_version": "1.0.6", "accounts": 2}], versions)
    check("F10", "spend keeps its decimals, and 7 days is not above all time",
          close(u["week"]["spend_usd"], spend) and close(u["all_time"]["spend_usd"], spend)
          and u["week"]["spend_usd"] <= u["all_time"]["spend_usd"]
          and all(close(d["cost_usd"], sum(r["cost"] for r in ledger if day_of(r["at"]) == d["day"]))
                  for d in u["daily"]), (u["week"]["spend_usd"], spend))
    notch_cost = sum(r["cost"] for r in ledger if r["kind"] in ("transcribe", "analyze")) / len(notches)
    report_cost = statistics.mean(r["cost"] for r in ok if r["kind"] == "reports")
    check("F11", "a notch costs its transcription and write-up, and a report is priced apart",
          close(u["week"]["notch_cost_usd"], notch_cost) and close(u["week"]["report_cost_usd"], report_cost)
          and notch_cost < spend / len(notches), (u["week"]["notch_cost_usd"], notch_cost))
    last = u["last_24h"]
    today = next(d for d in u["daily"] if d["day"] == u["today"])
    check("F12", "49 minutes into the UTC day, the last 24 hours still hold tonight's notch",
          u["today"] == "2026-10-01" and today["notches"] == 0 and last["notches"] == 1 and last["rewrites"] == 1
          and last["accounts"] == 1 and close(last["spend_usd"], sum(r["cost"] for r in day_ago))
          and last["failed"] == 1 and last["refused"] == 1 and last["turned_away"] == 6, last)
    # Its calls: the ledger's, the abandoned one, and the six turned away.
    check("F13", "nothing from Sep 28 is in the last 24 hours",
          last["reports"] == 0 and last["calls"] == len(day_ago) + 1 + 6, last)
    check("F30", "the caps reset at the next UTC midnight", close(u["resets_at"], at(10, 2, 0)), u["resets_at"])

    models = {(m["kind"], m["model"]): m for m in u["models"]}
    check("F14", "one row for a model, with its providers counted",
          len(u["models"]) == 4 and models[("analyze", CHAT)]["calls"] == 5
          and models[("analyze", CHAT)]["providers"] == [{"name": "Wafer", "calls": 5},
                                                          {"name": "Ionstream", "calls": 1}]
          and models[("transcribe", WHISPER)]["providers"] == [], u["models"])
    calls = {(c["kind"], c["prompt_version"]): c for c in u["calls"]}
    took = lambda kind, prompt: [r["took"] for r in ok if r["kind"] == kind and r["prompt"] == prompt]  # noqa: E731
    v4, v5 = calls.get(("analyze", "v4"), {}), calls.get(("analyze", "v5+c1"), {})
    check("F15", "no p95 for five calls", all(c["p95_s"] is None for c in u["calls"]), u["calls"])
    check("F16", "write-ups are split by prompt version, each with its own time and cost",
          set(calls) == {("transcribe", None), ("analyze", "v4"), ("analyze", "v5+c1"), ("takeaways", "v5+c1"),
                         ("reports", "r1")}
          and v4["calls"] == 4 and close(v4["p50_s"], median(took("analyze", "v4")))
          and close(v4["max_s"], 10.2) and close(v4["cost_usd"], 0.0024)
          and v5["calls"] == 1 and v5["failed"] == 1 and close(v5["p50_s"], 13.8) and close(v5["cost_usd"], 0.0036)
          and close(calls[("transcribe", None)]["p50_s"], 2.7) and close(u["audio_p50_s"], 15.2), u["calls"])
    recent = u["recent"]
    expected = sorted(ledger, key=lambda r: r["at"], reverse=True)
    check("F17", "recent calls are newest first, and the unfinished one has no duration",
          [r["status"] for r in recent[:3]] == ["in_flight", "rejected", "failed"] and recent[0]["took_s"] is None
          and recent[0]["overdue"] is True and [r["kind"] for r in recent[1:]] == [r["kind"] for r in expected]
          and all(close(r["took_s"], e["took"]) for r, e in zip(recent[3:], expected[2:]))
          and recent[4]["providers"] == ["Ionstream", "Wafer"] and recent[4]["prompt_version"] == "v5+c1"
          and recent[4]["account"] == "aaaaaaaa" and recent[4]["app_version"] == "1.0.6"
          and close(recent[4]["cost_usd"], 0.0036), recent[:5])
    check("F18", "the failed call and the refused one are in the recent list, with their codes",
          (recent[1]["code"], recent[2]["code"]) == ("idempotency_key_reused", "model_unavailable"), recent[1:3])
    zdr = u["zdr"]
    notices = " | ".join(a["text"] for a in u["attention"])
    check("F19", "five recordings unverified is said at the top, not left as a row",
          (zdr["audited"], zdr["hit"], zdr["miss"], zdr["unknown"]) == (5, 0, 0, 5)
          and "5 of 5 recordings" in notices and any(a["level"] == "warn" for a in u["attention"]), notices)
    accounts = {row["account"]: row for row in u["account_rows"]}
    check("F20", "the accounts table: a prefix, the latest build, the week's notches and spend",
          set(accounts) == {"aaaaaaaa", "bbbbbbbb"} and accounts["aaaaaaaa"]["app_version"] == "1.0.6"
          and accounts["aaaaaaaa"]["notches_7d"] == 5 and accounts["aaaaaaaa"]["reports_7d"] == 2
          and close(accounts["aaaaaaaa"]["spend_7d_usd"], spend) and close(accounts["aaaaaaaa"]["last_call_at"],
                                                                         at(9, 30, 22, 55))
          and accounts["bbbbbbbb"]["notches_7d"] == 0 and accounts["bbbbbbbb"]["last_call_at"] is None
          and accounts["bbbbbbbb"]["last_active_day"] == "2026-09-30"
          and (u["accounts"]["total"], u["accounts"]["notched"]) == (2, 1), u["account_rows"])
    days = [(d["day"], d["active"], d["notches"], d["rewrites"], d["reports"], d["failed"], d["refused"],
             d["turned_away"]) for d in u["daily"]]
    check("F22", "the day table runs from the first day of use to today, gap and rewrite included",
          days == [("2026-10-01", 0, 0, 0, 0, 0, 0, 0), ("2026-09-30", 2, 1, 1, 0, 1, 1, 6),
                   ("2026-09-29", 0, 0, 0, 0, 0, 0, 0), ("2026-09-28", 1, 1, 0, 1, 0, 0, 0),
                   ("2026-09-27", 1, 3, 0, 1, 0, 0, 0)], days)
    check("F23", "the call past its deadline is counted and said",
          u["in_flight"] == {"now": 1, "overdue": 1} and "past its deadline" in notices, (u["in_flight"], notices))
    server = u["server"]
    check("F24", "the server card is live: the API answers and OpenRouter's spend is read",
          server["api"] is not None and server["api"]["ok"] is True and server["openrouter"] is not None
          and close(server["openrouter"]["week_usd"], 0.2), server)
    check("F25", "no link to the health page where it has nothing to show", server["harness"] is False, server)
    config = u["config"]
    check("F16", "the config in force is named: its version, its note and its prompts",
          (config["version"], config["note"], config["prompts"]["analyze"], config["prompts"]["check"]) ==
          (3, "rewrites back on", "v5", "c1"), config)


def day_of(instant):
    return dt.datetime.fromtimestamp(instant, UTC).date().isoformat()


def page(check, s, u, dom, logged):
    """The rendered DOM: what a person reads, in the viewer's zone, under the page's own security policy."""
    week = u["week"]
    refused = [line for line in logged.splitlines() if "Refused to" in line or "Uncaught" in line]
    check("F29", "the browser refused nothing and threw nothing", not refused, refused[:2])
    check("F10", "money on the page has its decimals",
          usd(week["spend_usd"]) in part(dom, "week") and usd(week["notch_cost_usd"]) in part(dom, "week")
          and "$0.00 " not in part(dom, "daily") + " ", part(dom, "week"))
    check("F9", "the versions card shows 1.0.6 and no older build",
          "1.0.6" in part(dom, "versions") and not re.search(r"1\.0\.[0-5]", part(dom, "versions")),
          part(dom, "versions"))
    check("F19", "the page opens with what needs a look", "Needs a look" in part(dom, "notice")
          and "5 of 5 recordings" in part(dom, "notice")
          and not re.search(r'<section[^>]*\bid="notice"[^>]*\bhidden', dom), part(dom, "notice")[:200])
    check("F12", "the last-24-hours card shows tonight's notch",
          re.search(r"Notches 1\b", part(dom, "day")) is not None, part(dom, "day"))
    check("F30", "times are the viewer's: 22:55 UTC reads 18:55, and the caps reset at 20:00",
          "18:55" in part(dom, "recent") and "20:00" in part(dom, "server") and "22:55" not in part(dom, "recent"),
          part(dom, "server"))
    check("F25", "the page has no link to the health page", 'href="harness"' not in dom)
    check("F1", "the problems table reads in words", "Turned away" in part(dom, "problems")
          and "update required" in part(dom, "problems").lower(), part(dom, "problems")[:300])
    check("F5", "nothing said, and no whole id, is in the page",
          not [n for n in (CANARY, "walrus", A, B, *s.keys) if n.lower() in dom.lower()])
    check("F22", "the day table has five rows, not fourteen", dom.count('data-day="') == 5, dom.count('data-day="'))


def part_two(check, s, browser, out, shot=True):
    """Oct 1: twenty notches by one account, a provider with markup for a name, a deleted account, an old meter."""
    c = s.clock
    c.now = at(10, 1, 1, 0)
    s.config(C, "1.0.6+6")
    for n in range(20):
        c.advance(60)
        s.notch(C, "1.0.6+6", words=30, heard_in=2.0 + n / 10, written_in=1.0 + n / 10,
                serving=("<b>Bold</b>",) if n == 0 else ())
    c.advance(60)
    s.config(D, "1.0.6+6")
    s.notch(D, "1.0.6+6", words=30, heard_in=2.0, written_in=1.0)
    with_d = [row for row in s.ledger]
    s.call("DELETE", "/v2/account", user=D, expect=204)
    s.services.refusals.flush()
    u, _ = s.usage()
    with open(os.path.join(out, "usage-part-two.json"), "w", encoding="utf-8") as f:
        json.dump(u, f, indent=1)
    calls = {(c["kind"], c["prompt_version"]): c for c in u["calls"]}
    written = sorted(r["took"] for r in s.ledger if r["kind"] == "analyze" and r["prompt"] == "v5+c1"
                     and r["status"] == "ok" and r["user"] != D)
    k = (len(written) - 1) * 0.95
    p95 = written[int(k)] + (written[min(int(k) + 1, len(written) - 1)] - written[int(k)]) * (k - int(k))
    check("F15", "from twenty calls on, there is a p95", calls[("analyze", "v5+c1")]["calls"] == 21
          and close(calls[("analyze", "v5+c1")]["p95_s"], p95) and calls[("analyze", "v4")]["p95_s"] is None,
          calls[("analyze", "v5+c1")])
    kept = [r for r in with_d if r["user"] != D]
    notches = [r for r in kept if r["kind"] == "transcribe" and r["status"] == "ok"]
    cost = sum(r["cost"] for r in kept if r["kind"] in ("transcribe", "analyze")) / len(notches)
    check("F21", "a deleted account takes its notches, keeps the money spent, and leaves a notch's price right",
          u["accounts"]["deleted"] == 1 and u["all_time"]["notches"] == len(notches) == 25
          and close(u["all_time"]["spend_usd"], sum(r["cost"] for r in with_d))
          and close(u["week"]["notch_cost_usd"], cost) and "dddddddd" not in json.dumps(u), u["all_time"])
    check("F20", "an account at the daily cap is counted", u["limits"]["accounts_at_cap_today"] == 1
          and next(r for r in u["account_rows"] if r["account"] == "cccccccc")["notches_today"] == 20, u["limits"])
    if browser:
        dom, _ = chrome(browser, s.dash + "/", width=1280, height=2400, dom=True)
        check("F28", "a provider called <b>Bold</b> is text on the page, never an element",
              "&lt;b&gt;Bold&lt;/b&gt;" in dom and "<b>Bold</b>" not in dom)
        if shot:
            chrome(browser, s.dash + "/", width=1280, height=3600, shot=os.path.join(out, "desktop-busy.png"))

    old = os.path.join(s.work, "old-meter.db")
    with sqlite3.connect(s.meter_db) as source, sqlite3.connect(old) as copy:
        source.backup(copy)
        copy.execute("DROP TABLE refusals")
        copy.execute("DROP INDEX usage_by_day")
    before = dash_usage.read(old, s.clock() + 60)
    check("F7", "the dashboard reads a meter from before this change", "error" not in before
          and before["week"]["notches"] == u["week"]["notches"] and before["probes_7d"] == 0
          and not [p for p in before["problems"] if p["outcome"] == "turned_away"], before.get("error"))
    Meter(old, clock=s.clock).count_refusals({(int(s.clock() // 3600) * 3600, "/v2/analyze", 401, "unauthorized"):
                                              (1, s.clock())})
    # A counter whose meter's folder is gone when it writes, then back.
    folder = os.path.join(s.work, "gone")
    os.makedirs(folder)
    counter = RefusalCounter(Meter(os.path.join(folder, "meter.db"), clock=s.clock), clock=s.clock)
    counter.note({"status": 401, "route": "/v2/analyze", "error_code": "unauthorized"})
    shutil.rmtree(folder)
    try:
        counter.stop()
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    os.makedirs(folder)
    Meter(os.path.join(folder, "meter.db"), clock=s.clock)
    counter.flush()
    with sqlite3.connect(os.path.join(folder, "meter.db")) as db:
        kept = db.execute("SELECT sum(calls) FROM refusals").fetchone()[0]
    check("F36", "a write that cannot open the meter raises nothing and keeps its counts for the next one",
          raised is None and kept == 1, (raised, kept))
    with sqlite3.connect(old) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        events = db.execute("SELECT count(*) FROM usage_events").fetchone()[0]
    check("F8", "the new server opens that meter, adds what is missing and keeps every row",
          {"refusals", "usage_by_day"} <= names and events > 40, sorted(names))


def as_the_container_starts(check, work):
    """`python -m notch_api` and `python -m notch_dash` with compose.yaml's environment, on two free ports."""
    os.makedirs(work, exist_ok=True)
    api_port, dash_port, meter = free_port(), free_port(), os.path.join(work, "meter.db")
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NOTCH_", "OPENROUTER", "SUPABASE"))}
    api_env = base | {"NOTCH_ENV": "dev", "NOTCH_FAKE_MODELS": "1", "NOTCH_DEV_AUTH": "1", "NOTCH_HOST": "127.0.0.1",
                      "NOTCH_PORT": str(api_port), "NOTCH_METER_DB": meter, "NOTCH_TMP": os.path.join(work, "tmp"),
                      "NOTCH_METRICS": os.path.join(work, "metrics.jsonl"), "NOTCH_DB": os.path.join(work, "v1.db"),
                      "NOTCH_AUDIO_DIR": os.path.join(work, "v1-audio"), "OPENROUTER_API_KEY": ""}
    dash_env = base | compose_dash_environment() | {
        "NOTCH_DASH_BIND": "127.0.0.1", "NOTCH_DASH_PORT": str(dash_port), "NOTCH_PORT": str(api_port),
        "NOTCH_DASH_METER_DB": meter, "NOTCH_METRICS": api_env["NOTCH_METRICS"], "OPENROUTER_API_KEY": ""}
    api, dash = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{dash_port}"
    with open(os.path.join(work, "api.log"), "w") as api_log, open(os.path.join(work, "dash.log"), "w") as dash_log:
        servers = [subprocess.Popen([sys.executable, "-m", "notch_api"], cwd=HERE, env=api_env, stdout=api_log,
                                    stderr=api_log),
                   subprocess.Popen([sys.executable, "-m", "notch_dash"], cwd=HERE, env=dash_env, stdout=dash_log,
                                    stderr=dash_log)]
    try:
        for url in (api + "/healthz", dash + "/api/usage"):
            for _ in range(80):
                try:
                    httpx.get(url, timeout=1)
                    break
                except httpx.TransportError:
                    time.sleep(0.25)
            else:
                check("F34", "the two entry points start", False, f"{url} never answered; logs in {work}")
                return
        phone = {"authorization": "Bearer dev", "x-client": "ios/1.0.6+6"}
        heard = httpx.post(api + "/v2/transcribe", content=fake_recording("Shipped it."), timeout=30, headers=phone | {
            "idempotency-key": str(uuid.uuid4()), "content-type": "audio/mp4", "x-notch-mode": "daily",
            "x-notch-duration": "2"})
        stranger = {"x-client": "ios/1.0.6+6", "idempotency-key": str(uuid.uuid4())}
        denied = httpx.post(api + "/v2/analyze", json={"transcript": "x"}, headers=stranger)
        page = httpx.get(dash + "/")
        u = {}
        for _ in range(40):   # the counter writes every five seconds; the dashboard caches for ten
            u = httpx.get(dash + "/api/usage").json()
            if any(p["code"] == "unauthorized" for p in u.get("problems", [])) and (u["server"]["api"] or {}).get("ok"):
                break
            time.sleep(0.5)
        check("F34", "started as the container starts them, the two serve the usage page",
              heard.status_code == 200 and denied.status_code == 401 and page.status_code == 200
              and "Notch usage" in page.text and u.get("v") == 2 and u["week"]["notches"] == 1
              and any(p["code"] == "unauthorized" and p["calls"] == 1 for p in u["problems"])
              and u["server"] == {"api": u["server"]["api"], "openrouter": None, "harness": False}
              and u["server"]["api"]["ok"] is True, {k: u.get(k) for k in ("v", "error", "server", "problems")})
        httpx.post(api + "/v2/analyze", json={"transcript": "x"}, headers=stranger)
        servers[0].send_signal(signal.SIGTERM)
        servers[0].wait(timeout=15)
        with sqlite3.connect(meter) as db:
            held = db.execute("SELECT sum(calls) FROM refusals WHERE code = 'unauthorized'").fetchone()[0]
        check("F6", "SIGTERM, as `docker compose up` sends on a deploy, writes the last counts", held == 2, held)
    finally:
        for server in servers:
            if server.poll() is None:
                server.terminate()
        for server in servers:
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


def write_report(out, check, shots):
    rows = "".join(f"<tr class={state.lower()}><td>{html.escape(number)}</td><td>{html.escape(what)}</td>"
                   f"<td>{state}</td><td>{html.escape(detail[:400]) if state != 'PASS' else ''}</td></tr>"
                   for number, what, state, detail in check.rows)
    images = "".join(f'<h2>{html.escape(name)}</h2><img src="{html.escape(name)}" alt="{html.escape(name)}">'
                     for name in [*shots, "desktop-busy.png"] if os.path.exists(os.path.join(out, name)))
    verdict = "FAIL" if check.failed else "PASS"
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><meta charset="utf-8"><title>dash_usage e2e: {verdict}</title>
<style>body{{font:15px/1.45 system-ui;margin:32px auto;max-width:1100px;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td{{border-top:1px solid #ddd;padding:6px 8px;vertical-align:top}}
.fail td{{background:#fbe3dc}}.skip td{{color:#777}}
img{{max-width:100%;border:1px solid #ddd}}code{{background:#f3f0e8;padding:2px 5px}}</style>
<h1>The usage page, end to end: {verdict}</h1>
<p>{len(check.rows)} checks, {len(check.failed)} failed, on {dt.datetime.now().strftime('%b %d %Y at %H:%M')}.
Run it again with <code>.venv/bin/python e2e/dash_usage.py</code>; add <code>--serve</code> to keep the page up.
The documents the dashboard served are <a href="usage.json">usage.json</a> and
<a href="usage-part-two.json">usage-part-two.json</a>; the rendered page is <a href="page.html">page.html</a>.</p>
<table><tr><th>#</th><th>What must hold</th><th></th><th>What it found</th></tr>{rows}</table>{images}""")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--serve", action="store_true", help="leave the page up after the checks, to look at it")
    parser.add_argument("--out", help="where the report goes (default: e2e/runs/dash-usage-<stamp>)")
    args = parser.parse_args()
    out = args.out or os.path.join(HERE, "e2e", "runs", "dash-usage-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    check = run(out, serve=args.serve)
    print(f"\n{'FAIL' if check.failed else 'PASS'}: {len(check.rows) - len(check.failed)} of {len(check.rows)} checks; "
          f"the report is {os.path.join(out, 'report.html')}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
