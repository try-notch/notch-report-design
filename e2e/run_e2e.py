"""
run_e2e.py — the server end to end, driven the way the phone drives it.

    .venv/bin/python e2e/run_e2e.py                 live: real speech-to-text, chat and Jev via OpenRouter
    .venv/bin/python e2e/run_e2e.py --offline       fake models: no key, no network
    .venv/bin/python e2e/run_e2e.py --save-sample   also copy the final entry and report into e2e/sample/

The unit tests prove each module against fakes. This proves the assembled thing: a
real server process, real HTTP, an AAC recording going in, and a report coming out
whose every number can be checked against the notches the API itself returned.

  1. preflight   ffmpeg and ffprobe, and in live mode the key (never shown).
  2. fixtures    five spoken notches (fixtures/scripts.json). Live mode speaks each
                 script once with OpenRouter TTS and caches it as fixtures/audio/<name>.m4a,
                 AAC mono 44.1 kHz like the iOS recorder.
  3. seed        the 52 demo notches through the real analysis (notch_api.seed), into
                 a fresh database; category agreement with the hand labels, and how many
                 notches Jev classified (the rest fell back to the chat model), are reported.
  4. server      `python -m notch_api` on a free port against that database.
  5. capture     upload every fixture, repeat each upload (same job back), poll every
                 job to the end validating each body, then check what the model heard.
  6. refusals    401, 404, invalid_span, 413 and empty_range, each as the error envelope.
  7. reports     week, month, one project and one tag: accepted idempotently, written,
                 and every frozen number checked against the entries.
  8. listings    GET /v1/reports and GET /v1/projects agree with the rest.

Exit code 0 only when every check passed. The run is recorded under e2e/runs/<UTC stamp>/:
exchanges.jsonl (every request and response), the final entry and report bodies,
server.log, the run's database and summary.json. The server is always stopped.

OFFLINE uses NOTCH_FAKE_MODELS=1 and FakeClient, and a short ffmpeg tone per fixture in a
temp dir (never in fixtures/audio/). Only the expectations that need a real model are
relaxed: the project, acknowledgement and impact each script states. Every contract,
status, idempotency, invariant and error-envelope check still runs, and it must pass
before a live run is worth paying for.
"""

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import uuid
from collections import Counter
from datetime import date, datetime, time as clock, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # notch_api, and the demo modules it imports (seed_db, llm, ...)

import httpx  # noqa: E402

import seed_db  # noqa: E402
from notch_api import config, contract, store  # noqa: E402
from notch_api import seed as seeding  # noqa: E402
from notch_api.fakes import FakeClient  # noqa: E402
from notch_api.openrouter import ModelError, OpenRouterClient  # noqa: E402

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(E2E_DIR, "fixtures", "scripts.json")
AUDIO_CACHE = os.path.join(E2E_DIR, "fixtures", "audio")
RUNS_DIR = os.path.join(E2E_DIR, "runs")
SAMPLE_DIR = os.path.join(E2E_DIR, "sample")
JOB_TIMEOUT = 240           # seconds a capture or report job may take
SPEECH_SECONDS = (20, 60)   # what a spoken fixture should last
TONE_SECONDS = 2            # the offline stand-in for one


class Abort(Exception):
    """A check the rest of the run depends on failed."""


class Run:
    """One run's checks, timings and HTTP log, and the narrative printed as it goes."""

    def __init__(self, run_dir, offline):
        self.dir, self.offline = run_dir, offline
        self.checks, self.timings, self.agreement = [], {}, None
        self.db_path = os.path.join(run_dir, "notch_api.db")
        self.samples = {}  # what --save-sample copies: the last capture's entry, the week report
        self.section, self.http, self.server = None, None, None
        os.makedirs(run_dir)
        self._log = open(os.path.join(run_dir, "exchanges.jsonl"), "w")

    def heading(self, title):
        self.section = title
        print(f"\n  {title}")

    def note(self, text):
        print(f"        {text}")

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.checks.append({"section": self.section, "check": name, "ok": ok, "detail": "" if ok else detail})
        print(f"    {'✓' if ok else '✗'} {name}" + ("" if ok or not detail else f"\n        {detail}"))
        return ok

    def require(self, name, ok, detail=""):
        if not self.check(name, ok, detail):
            raise Abort(name)

    def save(self, name, body):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)

    def call(self, method, path, *, auth=True, logged=None, **kwargs):
        """One request -> (status, body). A transport error is status None, and still logged."""
        headers = {"Authorization": f"Bearer {config.DEV_TOKEN}"} if auth else {}
        started = time.monotonic()
        try:
            response = self.http.request(method, path, headers=headers, **kwargs)
            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = response.text
        except httpx.HTTPError as exc:
            status, body = None, f"{type(exc).__name__}: {exc}"
        self._log.write(json.dumps({
            "at": store.now(), "method": method, "path": path, "auth": auth,
            "request": logged if logged is not None else kwargs.get("json"),
            "status": status, "ms": round(1000 * (time.monotonic() - started)), "response": body,
        }, ensure_ascii=False) + "\n")
        self._log.flush()
        return status, body

    def upload(self, meta, audio):
        """POST /v1/entries as iOS sends it: the .m4a part plus an application/json meta part."""
        return self.call("POST", "/v1/entries", logged={"meta": meta, "audio_bytes": len(audio)}, files={
            "audio": ("submission.m4a", audio, "audio/mp4"),
            "meta": (None, json.dumps(meta), "application/json"),
        })


def problem(status, body, want, kind, code=None):
    """Why an answer is not HTTP `want` with a valid `kind` body (and error `code`), or None."""
    if status != want:
        return f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:300]}"
    try:
        contract.validate(kind, body)
    except contract.ContractError as exc:
        return str(exc)
    if code and body["error"]["code"] != code:
        return f"error code {body['error']['code']!r}, expected {code!r}"
    return None


def new_id():
    return str(uuid.uuid4()).upper()  # as Swift's UUID().uuidString mints it


# ---------------------------------------------------------------------------
# 1-2. Preflight and fixtures
# ---------------------------------------------------------------------------

def preflight(run):
    """-> the model client the fixtures and the seed use."""
    run.heading("preflight")
    run.require("ffmpeg and ffprobe on PATH", shutil.which("ffmpeg") and shutil.which("ffprobe"),
                "install ffmpeg (brew install ffmpeg)")
    if run.offline:
        return FakeClient()
    run.require("OPENROUTER_API_KEY present (never shown)", os.environ.get("OPENROUTER_API_KEY", "").strip(),
                "add it to .env or export it")
    run.note(f"chat {config.CHAT_MODEL} · decisions {config.JEV_MODEL} · speech-to-text {config.STT_MODEL}"
             f" · TTS {config.TTS_MODEL} ({config.TTS_VOICE})")
    return OpenRouterClient.from_env()


def to_m4a(source_args, dest):
    """ffmpeg -> AAC mono 44.1 kHz .m4a, the iOS recorder's format. Moved into place only when whole."""
    partial = dest + ".partial.m4a"
    proc = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *source_args,
                           "-ac", "1", "-ar", "44100", "-c:a", "aac", "-b:a", "64k", partial],
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"ffmpeg: {proc.stderr.strip()[-300:]}")
    os.replace(partial, dest)


def seconds_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def fixture_audio(run, client, tmp):
    """-> [(fixture, audio bytes, seconds)]. Live: cached TTS speech. Offline: a tone each, in tmp."""
    run.heading("fixtures")
    with open(SCRIPTS) as f:
        fixtures = json.load(f)["entries"]
    out = []
    for i, fx in enumerate(fixtures):
        name = fx["name"]
        try:
            if run.offline:
                path, how = os.path.join(tmp, f"{name}.m4a"), "tone"
                to_m4a(["-f", "lavfi", "-i", f"sine=frequency={330 + 110 * i}:duration={TONE_SECONDS}"], path)
            else:
                path, how = os.path.join(AUDIO_CACHE, f"{name}.m4a"), "cached"
                if not os.path.exists(path):
                    os.makedirs(AUDIO_CACHE, exist_ok=True)
                    mp3 = os.path.join(tmp, f"{name}.mp3")
                    with open(mp3, "wb") as f:
                        f.write(client.speech(fx["script"], voice=config.TTS_VOICE, fmt="mp3"))
                    to_m4a(["-i", mp3], path)
                    how = "generated with TTS"
        except (ModelError, RuntimeError) as exc:
            run.require(f"{name}.m4a", False, str(exc))
        seconds = seconds_of(path)
        low, high = (TONE_SECONDS - 0.5, TONE_SECONDS + 0.5) if run.offline else SPEECH_SECONDS
        run.check(f"{name}.m4a · {seconds:.1f} s ({how})", low <= seconds <= high,
                  f"should last {low}-{high} s")
        with open(path, "rb") as f:
            out.append((fx, f.read(), seconds))
    return out


# ---------------------------------------------------------------------------
# 3-4. Seed, then the server
# ---------------------------------------------------------------------------

def seed_history(run, client, db_path):
    run.heading(f"seed · {len(seed_db.ENTRIES)} demo notches through the analysis")
    started, results, error = time.monotonic(), None, ""
    try:
        results = seeding.seed(db_path, client, workers=4)
    except ModelError as exc:
        error = f"{exc.code}: {exc.message}"
    run.timings["seed"] = time.monotonic() - started
    run.require(f"every demo notch analysed ({run.timings['seed']:.1f} s)", results, error)
    agree, total = run.agreement = seeding.agreement(results)
    run.note(f"categories match the hand labels exactly on {agree}/{total} ({100 * agree // total}%)"
             " · reported, not a gate")
    by = classifiers(db_path)
    run.note(f"classified by Jev {by.get('jev', 0)}/{total}, by the chat model after Jev failed "
             f"{by.get('llm', 0)}/{total} · reported, not a gate")
    return results


def classifiers(db_path):
    """{'jev': n, 'llm': n}: which path classified the analysed notches (the chat model when Jev failed)."""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT classified_by, count(*) FROM entries WHERE classified_by IS NOT NULL "
                                 "GROUP BY classified_by"))
    finally:
        conn.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(run, db_path, audio_dir):
    run.heading("server")
    port = free_port()
    env = dict(os.environ, NOTCH_DB=db_path, NOTCH_AUDIO_DIR=audio_dir, NOTCH_PORT=str(port), PYTHONUNBUFFERED="1")
    env.pop("NOTCH_FAKE_MODELS", None)  # a stray export must not turn a live run fake
    if run.offline:
        env.update(NOTCH_FAKE_MODELS="1", OPENROUTER_API_KEY="")  # blank, so .env cannot supply one
    log_path = os.path.join(run.dir, "server.log")
    with open(log_path, "wb") as log:
        run.server = subprocess.Popen([sys.executable, "-m", "notch_api"], cwd=REPO_ROOT, env=env,
                                      stdout=log, stderr=subprocess.STDOUT)
    run.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120.0, trust_env=False)
    started, status, body = time.monotonic(), None, None
    while status != 200 and run.server.poll() is None and time.monotonic() - started < 30:
        time.sleep(0.2)
        status, body = run.call("GET", "/healthz", auth=False)
    run.timings["server start"] = time.monotonic() - started
    with open(log_path, errors="replace") as f:
        tail = " | ".join(f.read().strip().splitlines()[-5:])
    run.require(f"python -m notch_api answers /healthz on port {port} "
                f"({run.timings['server start']:.1f} s)", status == 200 and body == {"ok": True},
                f"exit code {run.server.poll()}; server.log: {tail}")


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def seeded_over_api(run, results):
    """-> {id: entry} for the seeded notches, as the API returns them."""
    run.heading("seeded notches over the API")
    entries, invalid, wrong_project = {}, [], []
    for (entry_id, _, _), (_, _, _, is_project, *_) in zip(results, seed_db.ENTRIES):
        status, body = run.call("GET", f"/v1/entries/{entry_id}")
        p = problem(status, body, 200, "entry") or (
            body["analysis_state"] != "complete" and f"analysis_state {body['analysis_state']}")
        if p:
            invalid.append(f"{entry_id}: {p}")
            continue
        entries[entry_id] = body
        if (body["project"] == seed_db.PROJECT_NAME) != is_project:
            wrong_project.append(f"{entry_id}: {body['project']!r}")
    run.check(f"all {len(results)} are valid, complete entries", not invalid, "; ".join(invalid[:3]))
    run.check(f"seeded project kept: {sum(e[3] for e in seed_db.ENTRIES)} on {seed_db.PROJECT_NAME}, the rest none",
              not wrong_project, "; ".join(wrong_project[:5]))
    return entries


# ---------------------------------------------------------------------------
# 5. Capture
# ---------------------------------------------------------------------------

def entry_meta(fx, seconds, today):
    """The recording facts the phone sends. Days are clamped to this week's Monday."""
    day = max(today - timedelta(days=fx["days_ago"]), today - timedelta(days=today.weekday()))
    recorded = datetime.combine(day, clock.fromisoformat(fx["time"]), timezone.utc)
    span = None
    if fx["mode"] == "catch_up":
        span = {"start": (day - timedelta(days=fx["span_days"] - 1)).isoformat(), "end": day.isoformat()}
    return {"id": new_id(), "recorded_at": store.iso(min(recorded, datetime.now(timezone.utc))),
            "duration_seconds": round(seconds, 1), "mode": fx["mode"], "catch_up_span": span}


def await_job(run, label, job_id, started):
    """
    Poll a job to its end, validating every body. -> the final body, or None (a failed check).
    `started` is when the job was accepted: the timing and the timeout run from there.
    """
    polls = 0
    while True:
        status, body = run.call("GET", f"/v1/jobs/{job_id}")
        polls += 1
        p = problem(status, body, 200, "job")
        if not p and body["status"] == "processing":
            if time.monotonic() - started < JOB_TIMEOUT:
                time.sleep(body["poll_after_ms"] / 1000)
                continue
            p = f"still processing after {JOB_TIMEOUT} s"
        if not p and body["status"] == "failed":
            p = f"job failed: {body['code']} ({body['message']})"
        run.timings[label] = time.monotonic() - started
        run.check(f"job complete {run.timings[label]:.1f} s after acceptance "
                  f"({polls} poll{'s' * (polls != 1)}, every body a valid job)", not p, p)
        return None if p else body


def capture(run, fixtures, today):
    """Upload every fixture (so the jobs run side by side), then follow each. -> {id: entry}."""
    run.heading("capture · upload")
    accepted = []
    for fx, audio, seconds in fixtures:
        meta = entry_meta(fx, seconds, today)
        status, body = run.upload(meta, audio)
        p = problem(status, body, 202, "entry_accepted") or (body["entry_id"] != meta["id"] and "entry_id not echoed")
        if not run.check(f"{fx['name']}: POST /v1/entries → 202 entry_accepted", not p, p):
            continue
        status, again = run.upload(meta, audio)
        run.check(f"{fx['name']}: the same upload again → the same job", status == 202 and again == body,
                  f"HTTP {status}: {again}")
        accepted.append((fx, meta, body["job_id"], time.monotonic()))

    entries = {}
    for fx, meta, job_id, started in accepted:
        expect = fx["expect"]
        run.heading(f"capture · {fx['name']}")
        run.note(f"{meta['mode']} · recorded {meta['recorded_at']} · expects {expect['project'] or 'no project'}")
        job = await_job(run, f"capture {fx['name']}", job_id, started)
        if job is None:
            continue
        entry = job["entry"]
        status, body = run.call("GET", f"/v1/entries/{meta['id']}")
        p = problem(status, body, 200, "entry") or (body != entry and "differs from the job's inline entry")
        run.check("GET /v1/entries/{id} → the same valid entry", not p, p)
        entries[entry["id"]] = run.samples["entry"] = entry
        run.save(f"entry-{fx['name']}.json", entry)

        echoed = {k: entry[k] for k in ("recorded_at", "duration_seconds", "mode", "catch_up_span")}
        run.check("recorded facts echoed (recorded_at, duration, mode, span)",
                  echoed == {k: meta[k] for k in echoed}, f"sent {meta}, got {echoed}")
        run.check(f"transcript heard ({entry['word_count']} words)",
                  (entry["transcript"] or "").strip() and entry["word_count"] > 0, "empty transcript")
        run.check(f"{len(entry['takeaways'])} takeaway(s), 1-3 expected", 1 <= len(entry["takeaways"]) <= 3)
        run.check("tags present and normalised", entry["tags"] and entry["tags"] == store.normalize_tags(entry["tags"]),
                  f"tags {entry['tags']}")
        run.note(f"summary   {entry['summary']}")
        run.note(f"tags      {' '.join('#' + t for t in entry['tags'])} · mood {entry['mood']}")
        if run.offline:
            run.note("project, acknowledgement and impact need a real model: not checked offline")
            continue
        run.check(f"project: {expect['project'] or 'none'}", entry["project"] == expect["project"],
                  f"got {entry['project']!r}")
        for key, field, what in (("acknowledged", "acknowledged_by", "acknowledgement"), ("impact", "impact_note", "impact")):
            if expect[key]:
                run.check(f"{what} heard" + (f": {entry[field]}" if entry[field] else ""), entry[field], f"{field} is null")
    return entries


# ---------------------------------------------------------------------------
# 6. Refusals
# ---------------------------------------------------------------------------

def refusals(run, today):
    run.heading("refusals")
    status, body = run.call("GET", "/v1/projects", auth=False)
    p = problem(status, body, 401, "error", "unauthorized")
    run.check("no bearer token → 401 unauthorized", not p, p)

    status, body = run.call("GET", f"/v1/jobs/{uuid.uuid4()}")
    p = problem(status, body, 404, "error", "not_found")
    run.check("unknown job → 404 not_found", not p, p)

    day = today.isoformat()
    for name, meta, audio, want, code in [
        ("a daily notch with a span", {"mode": "daily", "catch_up_span": {"start": day, "end": day}},
         b"not really audio", 400, "invalid_span"),
        (f"{config.MAX_UPLOAD_BYTES + 1:,} bytes of audio", {"mode": "daily", "catch_up_span": None},
         bytes(config.MAX_UPLOAD_BYTES + 1), 413, "payload_too_large"),
    ]:
        meta = {"id": new_id(), "recorded_at": store.now(), "duration_seconds": 5.0} | meta
        status, body = run.upload(meta, audio)
        p = problem(status, body, want, "error", code)
        if not p:
            status, _ = run.call("GET", f"/v1/entries/{meta['id']}")
            p = status != 404 and f"GET /v1/entries/{{id}} answered {status}: the refused upload was stored"
        run.check(f"{name} → {want} {code}, nothing stored", not p, p)

    req = {"id": new_id(), "type": "custom", "range_start": "2001-01-01", "range_end": "2001-01-07",
           "range_label": "Jan 1 – Jan 7, 2001", "project_id": None, "tag": None}
    status, body = run.call("POST", "/v1/reports", json=req)
    p = problem(status, body, 422, "error", "empty_range")
    if not p:
        status, _ = run.call("GET", f"/v1/reports/{req['id']}")
        p = status != 404 and f"GET /v1/reports/{{id}} answered {status}: the refused report was stored"
    run.check("a report over a range with no notches → 422 empty_range, nothing stored", not p, p)


# ---------------------------------------------------------------------------
# 7-8. Reports and listings
# ---------------------------------------------------------------------------

def span_label(start, end):
    return f"{start:%b} {start.day} – {end:%b} {end.day}"


def in_scope(entries, req):
    """The ids a report over `req` must count, oldest first, worked out from the API's own entries."""
    rows = sorted(entries.values(), key=lambda e: (e["recorded_at"], e["id"]))
    return [e["id"] for e in rows
            if e["analysis_state"] == "complete" and req["range_start"] <= e["recorded_at"][:10] <= req["range_end"]
            and (req["project_id"] is None or e["project_id"] == req["project_id"])
            and (req["tag"] is None or req["tag"] in e["tags"])]


def granularity_for(start, end):
    days = (end - start).days + 1
    return "day" if days <= 31 else "week" if days <= 120 else "month"


def bucket(d, granularity):
    """The first day of the momentum bucket holding `d`: itself, its Monday, or the 1st."""
    return {"day": d, "week": d - timedelta(days=d.weekday()), "month": d.replace(day=1)}[granularity]


def contiguous(momentum, granularity, start, end):
    """Every bucket from the one holding `start` to the one holding `end`: none missing, none extra."""
    def step(d):
        if granularity == "month":
            return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return d + timedelta(days=1 if granularity == "day" else 7)

    dates = [date.fromisoformat(b["date"]) for b in momentum]
    return (bool(dates) and dates[0] == bucket(start, granularity) and all(step(a) == b for a, b in zip(dates, dates[1:]))
            and dates[-1] <= end < step(dates[-1]))


def report_requests(run, entries, today):
    """-> {name: POST /v1/reports body}: this week, this month, one project and one tag over all the history."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    first = date.fromisoformat(min(e["recorded_at"][:10] for e in entries.values()))
    project_id = next((e["project_id"] for e in entries.values() if e["project"] == seed_db.PROJECT_NAME), None)
    handles = {store.normalize_tag(e["project"]) for e in entries.values() if e["project"]}
    counts = Counter(t for e in entries.values() for t in e["tags"] if t not in handles)
    tag = next((t for t, n in counts.most_common() if n >= 3), None)

    def req(kind, start, end, label, **scope):
        return {"id": new_id(), "type": kind, "range_start": start.isoformat(), "range_end": end.isoformat(),
                "range_label": label, "project_id": None, "tag": None} | scope

    run.heading("reports · accept")
    requests = {"week": req("week", monday, sunday, span_label(monday, sunday)),
                "month": req("month", today.replace(day=1), month_end, f"{today:%B %Y}")}
    if run.check(f"a {seed_db.PROJECT_NAME} project id to scope by", project_id):
        requests["project"] = req("custom", first, today, span_label(first, today), project_id=project_id)
    if run.check(f"a tag on at least 3 notches to scope by: #{tag} ({counts[tag]})", tag):
        requests["tag"] = req("custom", first, today, span_label(first, today), tag=tag)
    return requests


def write_reports(run, entries, today):
    """-> the ids of every report requested."""
    requests = report_requests(run, entries, today)
    accepted = []
    for name, req in requests.items():
        status, body = run.call("POST", "/v1/reports", json=req)
        p = problem(status, body, 202, "report_accepted") or (body["report_id"] != req["id"] and "report_id not echoed")
        if not run.check(f"{name}: POST /v1/reports → 202 report_accepted", not p, p):
            continue
        status, again = run.call("POST", "/v1/reports", json=req)
        run.check(f"{name}: the same request again → the same job", status == 202 and again == body,
                  f"HTTP {status}: {again}")
        accepted.append((name, req, body["job_id"], time.monotonic()))

    for name, req, job_id, started in accepted:
        scope = f" · project {seed_db.PROJECT_NAME}" if req["project_id"] else f" · #{req['tag']}" if req["tag"] else ""
        run.heading(f"report · {name}")
        run.note(f"{req['type']} · {req['range_label']}{scope}")
        if await_job(run, f"report {name}", job_id, started) is None:
            continue
        status, report = run.call("GET", f"/v1/reports/{req['id']}")
        p = problem(status, report, 200, "report")
        if run.check("GET /v1/reports/{id} → a valid report", not p, p):
            run.save(f"report-{name}.json", report)
            if name == "week":
                run.samples["report"] = report
            check_report(run, req, report, entries)
    return [req["id"] for req in requests.values()]


def check_report(run, req, report, entries):
    """Every frozen number, worked out again from the entries the API itself returned."""
    ids = report["source_entry_ids"]
    start, end = date.fromisoformat(req["range_start"]), date.fromisoformat(req["range_end"])
    granularity = report["momentum_granularity"]
    run.check("request echoed (type, range, label)",
              all(report[k] == req[k] for k in ("type", "range_start", "range_end", "range_label")))
    expected = in_scope(entries, req)
    run.check(f"counts the {len(expected)} notches in scope, oldest first", ids == expected,
              f"expected {expected}, got {ids}")
    scoped = [entries[i] for i in expected]
    counts = {"notches": len(scoped), "projects": len({e["project_id"] for e in scoped} - {None}),
              "milestones": sum(e["is_milestone"] for e in scoped)}
    run.check(f"counts {counts} are those notches'", report["counts"] == counts, f"got {report['counts']}")
    run.check(f"momentum buckets contiguous by {granularity}",
              granularity == granularity_for(start, end) and contiguous(report["momentum"], granularity, start, end),
              f"{granularity}: {[b['date'] for b in report['momentum']]}")
    days = Counter(bucket(date.fromisoformat(e["recorded_at"][:10]), granularity).isoformat() for e in scoped)
    run.check("momentum counts every notch in its own bucket",
              {b["date"]: b["count"] for b in report["momentum"] if b["count"]} == days, str(report["momentum"]))
    projects = Counter(e["project"] for e in scoped if e["project"])
    breakdown = [{"name": name, "notch_count": n, "share": 100 * n // len(scoped)}
                 for name, n in sorted(projects.items(), key=lambda item: (-item[1], item[0]))]
    run.check("project breakdown is those notches' projects, shares floored",
              report["project_breakdown"] == breakdown, f"expected {breakdown}, got {report['project_breakdown']}")
    run.check("highlights cite only this report's notches",
              all(set(h["source_entry_ids"]) <= set(ids) for h in report["highlights"]))
    run.check("headline, lede and body written", all((report[k] or "").strip() for k in ("headline", "lede", "body")))
    run.check("themes normalised", report["themes"] == store.normalize_tags(report["themes"]), str(report["themes"]))
    run.note(f"{report['eyebrow']} — {report['headline']}")
    run.note(report["lede"])
    run.note("themes " + " ".join("#" + t for t in report["themes"]) + " · highlights "
             + "; ".join(f"{h['title']} ({h['kind']})" for h in report["highlights"]))


def listings(run, entries, report_ids):
    run.heading("listings")
    status, body = run.call("GET", "/v1/reports")
    p = problem(status, body, 200, "report_list")
    listed = {r["id"] for r in body["reports"]} if not p else set()
    run.check(f"GET /v1/reports lists exactly the {len(report_ids)} reports written",
              not p and listed == set(report_ids), p or f"listed {sorted(listed)}")

    status, body = run.call("GET", "/v1/projects")
    p = problem(status, body, 200, "project_list")
    complete = [e for e in entries.values() if e["analysis_state"] == "complete"]
    projects = [] if p else body["projects"]
    wrong = [f"{name} missing" for name in set(seeding.PROJECTS.values()) - {x["name"] for x in projects}]
    for project in projects:
        n = sum(e["project_id"] == project["id"] for e in complete)
        if (project["notch_count"], project["share"]) != (n, 100 * n // max(len(complete), 1)):
            wrong.append(f"{project['name']}: {project['notch_count']} ({project['share']}%), expected {n}")
    run.check("GET /v1/projects: counts and shares match the entries", not p and not wrong, p or "; ".join(wrong))
    if not p:
        run.note(" · ".join(f"{x['name']} {x['notch_count']} ({x['share']}%)" for x in body["projects"]))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarise(run, args):
    failed = [c for c in run.checks if not c["ok"]]
    passed = bool(run.checks) and not failed
    print("\n  summary")
    sections = {}
    for c in run.checks:
        sections.setdefault(c["section"], Counter())[c["ok"]] += 1
    for section, tally in sections.items():
        print(f"    {section:<46} {tally[True]:>3} ✓ {tally[False]:>3} ✗")
    print(f"    {'total':<46} {len(run.checks) - len(failed):>3} ✓ {len(failed):>3} ✗")
    if run.timings:
        timings = " · ".join(f"{k} {v:.1f} s" for k, v in run.timings.items())
        print("\n" + textwrap.fill(timings, width=100, break_on_hyphens=False,
                                  initial_indent="    timings     ", subsequent_indent=" " * 16))
    by = classifiers(run.db_path)
    if run.agreement:
        agree, total = run.agreement
        models = "fake models" if run.offline else f"{config.JEV_MODEL}, falling back to {config.CHAT_MODEL}"
        print(f"    categories  {agree}/{total} seeded notches match the hand labels exactly ({100 * agree // total}%);"
              f" classified by Jev {by.get('jev', 0)}, by the chat model after Jev failed {by.get('llm', 0)}"
              f" ({models})")
    for c in failed:
        print(f"    ✗ {c['section']} › {c['check']}" + (f": {c['detail']}" if c["detail"] else ""))
    print(f"\n    recorded in {os.path.relpath(run.dir, REPO_ROOT)}/")
    run.save("summary.json", {"mode": "offline" if run.offline else "live", "passed": passed,
                              "checks": run.checks, "timings": run.timings, "category_agreement": run.agreement,
                              "classified_by": by})
    if args.save_sample:
        save_sample(run, passed)
    print(f"\n  {'PASSED' if passed else 'FAILED'} · {len(run.checks) - len(failed)}/{len(run.checks)} checks\n")
    return 0 if passed else 1


def save_sample(run, passed):
    """The committed sample is real model output from a passing run, never a fake's."""
    if run.offline or not passed:
        print(f"    --save-sample skipped: {'offline output is a fake model' if run.offline else 'the run failed'}")
        return
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    for kind, body in run.samples.items():
        with open(os.path.join(SAMPLE_DIR, f"{kind}.json"), "w") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
    print(f"    sample saved to {os.path.relpath(SAMPLE_DIR, REPO_ROOT)}/: "
          + ", ".join(f"{kind}.json" for kind in run.samples))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the Notch server end to end.")
    parser.add_argument("--offline", action="store_true", help="fake models and tone audio: no key, no network")
    parser.add_argument("--save-sample", action="store_true",
                        help="on a passing live run, copy the final entry and the week report into e2e/sample/")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = Run(os.path.join(RUNS_DIR, stamp), args.offline)
    today = datetime.now(timezone.utc).date()
    print(f"\n  Notch E2E · {'offline, fake models' if args.offline else 'live, OpenRouter'} · {stamp}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = preflight(run)
            fixtures = fixture_audio(run, client, tmp)
            results = seed_history(run, client, run.db_path)
            start_server(run, run.db_path, os.path.join(run.dir, "audio"))
            entries = seeded_over_api(run, results)
            entries |= capture(run, fixtures, today)
            refusals(run, today)
            listings(run, entries, write_reports(run, entries, today))
    except Abort:
        pass
    except Exception as exc:  # still summarise, still stop the server
        traceback.print_exc()
        run.heading("unexpected error")
        run.check("the run finished", False, f"{type(exc).__name__}: {exc}")
    finally:
        stop_server(run.server)
    return summarise(run, args)


if __name__ == "__main__":
    sys.exit(main())
