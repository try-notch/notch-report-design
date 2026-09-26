"""
app.py — the HTTP surface: the §5 routes the iOS app calls, and nothing else.

Routes are thin. Each one checks the bearer, reads its own request, calls into
store / reports / the job runner, and hands back one contract.py kind — validated
here before it leaves, so a shape the phone cannot decode is a loud 500
`contract_violation` in our logs rather than a silent decode failure on a device.

EVERY NON-2xx IS THE ERROR ENVELOPE, {"error": {code, message, retryable}}: our own
refusals (store.ApiError, raised here and in reports.py), Starlette's 404/405 for
unknown routes, a malformed multipart body, FastAPI's validation errors and anything
unexpected. The client switches on `code`, so no response may fall back to FastAPI's
{"detail": ...}.

THE RECORD AND ACCOUNT ROUTES LIVE IN THEIR OWN MODULES (record_routes: the entry
list, edit, delete, takeaways and report delete; account_routes: stats and /v1/me),
registered from create_app. Every route reads its bearer and body and writes its
answer through web.py, so these rules hold wherever a route lives.

/v2 IS MOUNTED BESIDE /v1 when create_app is given `services` (services.py): the
stateless routes in v2.py and cloud.py, which keep no content. /v1 stores transcripts
and audio, so it is the development harness only: `v1=False` (NOTCH_ENV=prod) leaves it
unmounted and creates no content database or audio directory at all. Either way the
app is wrapped in privacy.Guard, the outermost layer: one scrubbed log line per request,
and the 500 for any exception answered there, never by uvicorn.

AUTH IS STUBBED: `Bearer dev` is the dev user and anything else is 401. No route
takes a user id; every query is scoped to the token's user, and a record owned by
someone else answers exactly like one that does not exist (404), except on create,
where a client-minted id already taken by another user is a 409.

UPLOADS ARE CAPPED WHILE THEY STREAM. A declared Content-Length over the cap is
refused before any body is read, and the body is counted as it arrives, so a
missing or lying Content-Length cannot make the server spool an unbounded upload.
The audio part itself may be up to 25 MB (§5 "Request size"); the multipart framing
and the meta part get a little room on top.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from fastapi.responses import JSONResponse

from . import account_routes, audio, config, contract, privacy, record_routes, reports, store, web, worker
from .store import ApiError
from .wire_v2 import Refusal

log = logging.getLogger(__name__)

POLL_AFTER_MS = 750          # §5's poll example
FORM_SLACK = 64 * 1024       # multipart boundaries, part headers and the meta part
_JSON = web.json_body()      # POST /v1/projects and /v1/reports are a few hundred bytes

# Entry ids become a path segment of the stored audio (<user>/<entry>/000), so only
# characters that cannot name another directory. iOS sends UUID strings.
_ENTRY_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# failure_code -> the job's `message`. The job tables keep only the code; the message
# is for logs and support and is never rendered (§5 "Error envelope").
_FAILURE_MESSAGES = {
    "transcription_failed": "No speech detected.",
    "audio_unreadable": "The recording could not be read as audio.",
    "model_refused": "The model could not process this.",
    "model_unavailable": "The model service is unavailable. Try again later.",
}

_HTTP_CODES = {400: "invalid_request", 401: "unauthorized", 404: "not_found",
               405: "method_not_allowed", 413: "payload_too_large"}


# ---------------------------------------------------------------------------
# Request reading: the capture meta. Auth and capped bodies are web.py's.
# ---------------------------------------------------------------------------

def _parse_meta(raw):
    """The `meta` part -> the recording facts, checked. A span that disagrees with the mode is invalid_span."""
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        meta = None
    if not isinstance(meta, dict):
        raise web.bad("meta must be a JSON object.")
    entry_id, duration, mode, span = (meta.get(k) for k in ("id", "duration_seconds", "mode", "catch_up_span"))
    if not isinstance(entry_id, str) or not _ENTRY_ID.fullmatch(entry_id):
        raise web.bad("meta.id must be 1-128 letters, digits, '-' or '_'.")
    try:
        recorded_at = store.iso(store.parse_instant(meta.get("recorded_at")))
    except (TypeError, ValueError, OverflowError):
        raise web.bad("meta.recorded_at must be an ISO 8601 instant with a zone.") from None
    # The upper bound also keeps out a JSON integer too big for float().
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= sys.float_info.max:
        raise web.bad("meta.duration_seconds must be a number >= 0.")
    if mode not in ("daily", "catch_up"):
        raise web.bad("meta.mode must be daily or catch_up.")
    if span is not None:
        try:
            span = store.parse_date(span["start"]), store.parse_date(span["end"])
        except (TypeError, KeyError, ValueError):
            raise web.bad("meta.catch_up_span must be {start, end} as YYYY-MM-DD dates.") from None
    if (mode == "catch_up") != (span is not None) or (span and span[1] < span[0]):
        raise ApiError("invalid_span", 400,
                       "A daily notch has no span; a catch-up notch needs one that ends on or after its start.")
    return {"id": entry_id, "recorded_at": recorded_at, "duration_seconds": float(duration), "mode": mode,
            "span": tuple(d.isoformat() for d in span) if span else (None, None)}


def _existing_capture_job(conn, user_id, entry_id):
    """The job of the entry this id already names, or None. Another user's id is 409."""
    row = conn.execute("SELECT e.user_id, j.id AS job_id FROM entries e "
                       "LEFT JOIN capture_jobs j ON j.entry_id = e.id WHERE e.id = ?", (entry_id,)).fetchone()
    if row is None:
        return None
    # An entry with no capture job (a seeded one) has no job to hand back either.
    if row["user_id"] != user_id or row["job_id"] is None:
        raise ApiError("conflict", 409, "This entry id is already in use.")
    return row["job_id"]


# ---------------------------------------------------------------------------
# The app.
# ---------------------------------------------------------------------------

def create_app(*, db_path=config.DB_PATH, audio_dir=config.AUDIO_DIR, client=None,
               transcode=audio.to_m4a_16k, inline_jobs=False, clock=None, services=None, v1=True):
    """
    `clock` -> the current aware instant; stats' "today" reads it, so tests can pin the day.
    `services` mounts /v2; `v1=False` leaves the /v1 harness out. Returns the app wrapped
    in privacy.Guard (which passes attribute access through to the FastAPI app).
    """
    @asynccontextmanager
    async def lifespan(app):
        if v1:
            os.makedirs(audio_dir, exist_ok=True)
            store.init_db(db_path)
            conn = store.connect(db_path)
            try:
                store.ensure_dev_user(conn)
            finally:
                conn.close()
            app.state.runner = worker.JobRunner(
                db_path, client=client if client is not None else worker.LazyClient(), transcode=transcode,
                audio_dir=audio_dir, inline=inline_jobs)
            log.info("resumed %d unfinished job(s)", app.state.runner.resume_pending())
        if services is not None:
            log.info("expired %d unsettled call(s)", services.meter.expire_stale())
        try:
            yield
        finally:
            if v1:
                app.state.runner.shutdown()
            if services is not None:
                services.shutdown()

    app = FastAPI(title="notch_api", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def db():
        conn = store.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    # -- errors: one envelope for every non-2xx --------------------------------

    @app.exception_handler(Refusal)
    async def refused_v2(request, exc):
        privacy.request_entry(request.scope)["error_code"] = exc.code
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers())

    @app.exception_handler(ApiError)
    async def refused(request, exc):
        privacy.request_entry(request.scope)["error_code"] = exc.code
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return web.envelope(exc.status, exc.code, exc.message, exc.retryable, headers)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        privacy.request_entry(request.scope)["error_code"] = code
        return web.envelope(exc.status_code, code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        first = next(iter(exc.errors()), {})
        where = ".".join(str(p) for p in first.get("loc", ()))
        return web.envelope(400, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(contract.ContractError)
    async def violation(request, exc):
        log.error("response broke the contract on %s %s: %s", request.method, request.url.path, exc)
        return web.envelope(500, "contract_violation", "The server built a response the contract does not allow.")

    @app.exception_handler(Exception)
    async def crashed(request, exc):
        # Starlette re-raises after this; privacy.Guard catches it and logs it scrubbed.
        privacy.request_entry(request.scope)["error_code"] = "internal_error"
        return web.envelope(500, "internal_error", "Something went wrong on the server.")

    # -- routes -----------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    def mount_v1():
        @app.post("/v1/entries")
        async def create_entry(request: Request, user: str = Depends(web.user)):
            try:
                form = await web.capped(request, config.MAX_UPLOAD_BYTES + FORM_SLACK).form(
                    max_files=2, max_fields=16, max_part_size=FORM_SLACK)
            except HTTPException as exc:
                # Starlette reads a part with no filename= as a text field, capped at FORM_SLACK.
                if "exceeded maximum size" not in str(exc.detail):
                    raise
                raise web.bad("A text part is over 64 KB. Send audio as a file part: its Content-Disposition "
                           "needs filename=.") from None
            try:
                upload, meta = form.get("audio"), form.get("meta")
                if not isinstance(upload, UploadFile):
                    raise web.bad("The audio part is missing; send it as a file part.")
                if upload.size > config.MAX_UPLOAD_BYTES:
                    raise web.too_large(config.MAX_UPLOAD_BYTES)
                if not upload.size:
                    raise web.bad("The recording is empty.")
                meta = _parse_meta((await meta.read()).decode(errors="replace") if isinstance(meta, UploadFile) else meta)
                job_id = await run_in_threadpool(accept_entry, user, meta, upload)
            finally:
                await form.close()
            return web.reply("entry_accepted", {"job_id": job_id, "entry_id": meta["id"]}, 202)

        def accept_entry(user, meta, upload):
            """
            Idempotent on the entry id (§3.5): a repeat gets the existing job and nothing is
            stored again. The audio is written first, under its final name by an atomic
            rename, then the entry, its job and the audio row commit together, so a job
            never exists without its file.
            """
            conn = store.connect(db_path)
            try:
                job_id = _existing_capture_job(conn, user, meta["id"])
                if job_id:
                    return job_id
                key = f"{user}/{meta['id']}/000"
                path = os.path.join(audio_dir, key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                upload.file.seek(0)
                with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as f:
                    shutil.copyfileobj(upload.file, f)
                os.replace(f.name, path)
                job_id = store.new_id()
                try:
                    with conn:
                        conn.execute("INSERT INTO entries (id, user_id, recorded_at, duration_seconds, capture_mode, "
                                     "span_start, span_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (meta["id"], user, meta["recorded_at"], meta["duration_seconds"], meta["mode"],
                                      *meta["span"]))
                        conn.execute("INSERT INTO capture_jobs (id, user_id, entry_id) VALUES (?, ?, ?)",
                                     (job_id, user, meta["id"]))
                        conn.execute("INSERT INTO audio_objects (id, user_id, capture_job_id, entry_id, storage_key, "
                                     "content_type, byte_size, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (store.new_id(), user, job_id, meta["id"], key,
                                      upload.content_type or "audio/mp4", upload.size, meta["duration_seconds"]))
                except sqlite3.IntegrityError:
                    # A concurrent repeat won the insert (and wrote the same file): answer as a repeat.
                    job_id = _existing_capture_job(conn, user, meta["id"])
                    if job_id is None:
                        raise
                    return job_id
            finally:
                conn.close()
            app.state.runner.submit_capture(job_id)
            return job_id

        @app.get("/v1/jobs/{job_id}")
        def get_job(job_id: str, user: str = Depends(web.user), conn=Depends(db)):
            row = conn.execute(
                "SELECT entry_id, NULL AS report_id, state, failure_code FROM capture_jobs WHERE id = ? AND user_id = ? "
                "UNION ALL "
                "SELECT NULL, report_id, state, failure_code FROM report_jobs WHERE id = ? AND user_id = ?",
                (job_id, user, job_id, user)).fetchone()
            if row is None:
                raise ApiError("not_found", 404, "No such job.")
            entry_id = row["entry_id"]
            record = {"entry_id": entry_id} if entry_id else {"report_id": row["report_id"]}
            if row["state"] == "complete":
                job = {"status": "complete", **record}
                if entry_id:
                    job["entry"] = store.load_entry(conn, user, entry_id)
            elif row["state"] == "failed":
                job = {"status": "failed", **record, "code": row["failure_code"],
                       "message": _FAILURE_MESSAGES.get(row["failure_code"], "The job failed."),
                       # §5: the audio's purge_after, so #c-failed knows whether a retry is still possible.
                       "retryable_until": store.load_entry(conn, user, entry_id)["retryable_until"] if entry_id else None}
            else:
                job = {"status": "processing", **record, "poll_after_ms": POLL_AFTER_MS}
            return web.reply("job", job)

        @app.get("/v1/entries/{entry_id}")
        def get_entry(entry_id: str, user: str = Depends(web.user), conn=Depends(db)):
            entry = store.load_entry(conn, user, entry_id)
            if entry is None:
                raise ApiError("not_found", 404, "No such entry.")
            return web.reply("entry", entry)

        @app.get("/v1/projects")
        def get_projects(user: str = Depends(web.user), conn=Depends(db)):
            return web.reply("project_list", {"projects": store.list_projects(conn, user)})

        @app.post("/v1/projects")
        def create_project(body=Depends(_JSON), user: str = Depends(web.user), conn=Depends(db)):
            """201 with the client's id, or 200 with the existing project when the folded name is taken (§5)."""
            project_id, name = (body.get(k) for k in ("id", "name")) if isinstance(body, dict) else (None, None)
            if not isinstance(project_id, str) or not project_id.strip():
                raise web.bad("id must be a non-empty string.")
            if not isinstance(name, str) or not name.strip():
                raise web.bad("name must be a non-empty string.")
            existing, status = store.find_project_id(conn, user, name), 200
            if existing is None:
                try:
                    with conn:
                        conn.execute("INSERT INTO projects (id, user_id, name) VALUES (?, ?, ?)",
                                     (project_id, user, name.strip()))
                    existing, status = project_id, 201
                except sqlite3.IntegrityError:
                    # Lost a race for the same folded name (answer with the winner), or the id is taken.
                    existing = store.find_project_id(conn, user, name)
                    if existing is None:
                        raise ApiError("conflict", 409, "This project id is already in use.") from None
            project = next(p for p in store.list_projects(conn, user) if p["id"] == existing)
            return web.reply("project", project, status)

        @app.post("/v1/reports")
        def create_report(body=Depends(_JSON), user: str = Depends(web.user), conn=Depends(db)):
            report_id, job_id, created = reports.accept_report(conn, user, body)
            if created:
                app.state.runner.submit_report(job_id)
            return web.reply("report_accepted", {"job_id": job_id, "report_id": report_id}, 202)

        @app.get("/v1/reports")
        def get_reports(user: str = Depends(web.user), conn=Depends(db)):
            rows = conn.execute("SELECT id, range_label, headline, type, generated_at FROM reports WHERE user_id = ? "
                                "ORDER BY generated_at DESC, rowid DESC", (user,)).fetchall()
            return web.reply("report_list", {"reports": [dict(r) for r in rows], "next_cursor": None})

        @app.get("/v1/reports/{report_id}")
        def get_report(report_id: str, user: str = Depends(web.user), conn=Depends(db)):
            report = store.load_report(conn, user, report_id)
            if report is None:
                raise ApiError("not_found", 404, "No such report.")
            return web.reply("report", report)

        record_routes.register(app, db=db, audio_dir=audio_dir)
        account_routes.register(app, db=db, audio_dir=audio_dir,
                                clock=clock or (lambda: datetime.now(timezone.utc)))

    if v1:
        mount_v1()
    if services is not None:
        from . import v2
        v2.register(app, services)
    return privacy.Guard(app)
