"""
v2.py — the stateless /v2 routes (docs/backend-contract.md in notch-ios-dev):

  GET    /v2/config        remote config for the app; with a token, today's usage too
  POST   /v2/transcribe    raw audio -> transcript
  POST   /v2/analyze       transcript + context -> summary, takeaways, tags, mood, categories...
  POST   /v2/takeaways     transcript + context -> takeaways and tags, written again
  POST   /v2/reports       a report's entries, sent by the device -> facts and prose
  DELETE /v2/account       Apple revoked, the Supabase user deleted, every metering row and
                           Notch Cloud record gone, the id kept as deleted

NOTHING IS KEPT. Every call is synchronous and its response carries the whole result;
request and response bodies live in this process's memory and, for audio, in one temp
directory on a tmpfs that is removed before the response goes. The meter (meter.py)
gets sizes, costs, codes and versions, never content.

ONE PATH FOR EVERY PROCESSING CALL, in this order, each step able to refuse:
  headers (400) -> token (401) -> account (403 gone / blocked) -> app version (426) ->
  processing switch (503 processing_paused) -> feature (503 feature_disabled) -> calls
  this process is running for the account (429 rate_limited) -> the body, read with a
  cap (413, 415, 400) -> for audio, a transcription slot (Slots: 503 unavailable when
  none frees up in time), then decode it (422, 413 audio_too_long) ->
  check-and-start (meter.py: 409, 429, 503) -> the models, under the deadline (502,
  422, 504) -> settle -> the response, checked against the enums of the client's
  contract version.
The deadline starts when the request arrives, so an upload's own time counts; the work
runs on a thread pool and the route answers 504 when the deadline passes, whatever the
thread is still doing (its late cost is still metered). Settling happens before the
response is written, and a settle that fails is logged but never replaces the answer.
"""

import asyncio
import json
import logging
import os
import tempfile
from collections import Counter

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import analysis, prompts, reports, speech, store, web, wire_v2
from .audio import AudioUnreadable
from .identity import AppleCodeRejected
from .openrouter import Deadline, DeadlineExceeded, ModelError, ModelRefused, TranscriptionFailed, Usage
from .privacy import alert, request_entry
from .speech import DEMUXERS
from .wire_v2 import Refusal, bad

log = logging.getLogger(__name__)

GRACE = 1.0                      # seconds past the deadline the route waits for its thread, as a backstop
PAUSED_RETRY = 300               # Retry-After while processing is switched off
FEATURE = {"transcribe": "capture", "analyze": "capture", "takeaways": "takeaways", "reports": "reports"}
MAX_TEXT = 200                   # a project name, a tag, an author field, a label
MAX_ID = 128                     # a report entry's id
APPLE_CODE = 2048
_ADJECTIVE = {"week": "Weekly", "month": "Monthly", "quarter": "Quarterly", "year": "Yearly", "custom": "Custom"}


# ---------------------------------------------------------------------------
# Request reading
# ---------------------------------------------------------------------------

async def read_body(request, limit):
    """The body, read with a cap: a declared or actual length over `limit` is 413 payload_too_large."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise Refusal("payload_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise Refusal("payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_json(raw):
    """A JSON object from the body, else 400. Deep nesting and lone surrogates are 400 too."""
    try:
        value = web.encodable(json.loads(raw))
    except (ValueError, RecursionError):
        raise bad("The body must be a JSON object.") from None
    if not isinstance(value, dict):
        raise bad("The body must be a JSON object.")
    return value


def _fields(value, name, required, optional=()):
    if not isinstance(value, dict):
        raise bad(f"{name} must be a JSON object.")
    if set(value) - set(required) - set(optional):
        raise bad(f"{name} has a field this contract does not define.")
    if set(required) - set(value):
        raise bad(f"{name} is missing a required field.")
    return value


def _text(value, name, *, limit=MAX_TEXT, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise bad(f"{name} must be a non-empty string of at most {limit} characters.")
    return value.strip()


def _strings(value, name, *, most, limit=MAX_TEXT):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > most:
        raise bad(f"{name} must be a list of at most {most} strings.")
    return [_text(item, name, limit=limit) for item in value]


def _transcript(value, cfg):
    if not isinstance(value, str) or not value.strip():
        raise bad("transcript must be a non-empty string.")
    if len(value) > cfg["max_transcript_chars"]:
        raise Refusal("transcript_too_long")
    return value.strip()


def _writing_prompts(cfg, kind):
    """
    (labels, check, prompt_version) for analyze or takeaways: remote config's variant for
    `kind`, and its check, or None when that is "off". The version names both ("v5+c1"),
    so a notch says which words wrote it and whether they were checked.
    """
    version, check_name = cfg["prompts"][kind], cfg["prompts"]["check"]
    check = prompts.variant("check", check_name)
    return prompts.variant(kind, version), check, version if check is None else f"{version}+{check_name}"


def _date(value, name):
    try:
        return store.parse_date(value)
    except (ValueError, TypeError):
        raise bad(f"{name} must be a YYYY-MM-DD date.") from None


def canonical_project(name, project_names):
    """The model's project name as the device spelled it, or None when it is not one of the device's."""
    if not name:
        return None
    folded = name.strip().casefold()
    return next((p for p in project_names if p.strip().casefold() == folded), None)


def refusal_for(exc, kind):
    """Whatever the work raised -> the Refusal the client gets. Unexpected ones are logged, scrubbed."""
    if isinstance(exc, Refusal):
        return exc
    if isinstance(exc, DeadlineExceeded):
        return Refusal("deadline_exceeded")
    if isinstance(exc, TranscriptionFailed):
        return Refusal("no_speech")
    if isinstance(exc, AudioUnreadable):
        return Refusal("audio_unreadable")
    if isinstance(exc, ModelRefused):
        if exc.http_status in (401, 402, 404):   # our key, our credit, or no ZDR endpoint to route to: an outage
            return Refusal("model_unavailable")
        return Refusal("audio_unreadable" if kind == "transcribe" else "model_refused")
    if isinstance(exc, ModelError):
        return Refusal("model_unavailable")
    if isinstance(exc, wire_v2.ContractError):
        alert("contract_violation", kind=kind)
        return Refusal("internal_error")
    log.error("unexpected_exception", exc_info=(type(exc), exc, exc.__traceback__),
              extra={"notch": {"event": "unexpected_exception", "kind": kind}})
    return Refusal("internal_error")


class InFlight:
    """The calls this process is running per account, from before the body is read until after settling."""

    def __init__(self):
        self._running = {}

    def enter(self, user_id, limit):
        if self._running.get(user_id, 0) >= limit:
            raise Refusal("rate_limited", retry_after=5)
        self._running[user_id] = self._running.get(user_id, 0) + 1

    def leave(self, user_id):
        left = self._running.get(user_id, 1) - 1
        if left > 0:
            self._running[user_id] = left
        else:
            self._running.pop(user_id, None)


class Slots:
    """
    At most remote config's `transcribe_concurrency` transcriptions decode and transcribe at
    once in this process: ffmpeg is the one CPU-heavy step, and the VPS has two cores. A
    call over the limit waits for a slot (at most `TRANSCRIBE_WAIT` seconds, and never past
    half its deadline), then is 503 unavailable, which the app retries after Retry-After.
    """

    def __init__(self):
        self._loop = self._changed = None
        self.busy = 0

    def _condition(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:   # a new event loop (only ever in tests): nothing from the old one is running
            self._loop, self._changed, self.busy = loop, asyncio.Condition(), 0
        return self._changed

    async def acquire(self, limit, wait):
        changed = self._condition()
        async with changed:
            try:
                await asyncio.wait_for(changed.wait_for(lambda: self.busy < limit), timeout=wait)
            except asyncio.TimeoutError:
                raise Refusal("unavailable", retry_after=5) from None
            self.busy += 1

    async def release(self):
        changed = self._condition()
        async with changed:
            self.busy -= 1
            changed.notify_all()


TRANSCRIBE_WAIT = 10.0


class Caller:
    """The verified caller of one request: X-Client, the principal, the account, the config it runs under."""

    def __init__(self, client, principal, account, cfg):
        self.client, self.principal, self.account, self.cfg = client, principal, account, cfg
        self.features = cfg.features_for(account.flags if account else {})

    @property
    def user_id(self):
        return self.principal.user_id


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

class V2:
    def __init__(self, services):
        self.s = services
        self.inflight = InFlight()
        self.transcribing = Slots()

    async def caller(self, request, *, required=True, account=True):
        client = wire_v2.Client.parse(request.headers.get("x-client"))
        entry = request_entry(request.scope)
        entry.update(app_version=client.app_version, platform=client.platform)
        principal = await run_in_threadpool(self.s.auth.principal, request.headers.get("authorization"),
                                            required=required)
        cfg = await run_in_threadpool(self.s.remote.current)
        entry["config_version"] = cfg.version
        found = None
        if principal is not None and account:
            found = await run_in_threadpool(self.s.meter.touch_account, principal.user_id)
        return Caller(client, principal, found, cfg)

    def admit(self, caller, kind, *, mode=None):
        """The checks that need no body: blocked, app version, the switches."""
        if caller.account.blocked:
            raise Refusal("account_blocked")
        if caller.client.version < caller.cfg.min_app_version():
            raise Refusal("app_update_required")
        if not caller.features["processing"]:
            raise Refusal("processing_paused", retry_after=PAUSED_RETRY)
        if not caller.features[FEATURE[kind]] or (mode == "catch_up" and not caller.features["catch_up"]):
            raise Refusal("feature_disabled", retry_after=3600)

    async def run(self, request, caller, *, kind, key, body_hmac, sizes, prompt_version, deadline, work, respond,
                  audio_seconds=None):
        """Check-and-start, the work under the deadline, settle -> the response body, or Refusal."""
        s, cfg, entry = self.s, caller.cfg, request_entry(request.scope)
        versions = {"config_version": cfg.version, "prompt_version": prompt_version,
                    "app_version": caller.client.app_version, "platform": caller.client.platform}
        started = await run_in_threadpool(
            s.meter.start, user_id=caller.user_id, kind=kind, key=key, body_hmac=body_hmac,
            deadline_seconds=cfg["deadlines"][kind] + GRACE, config=cfg, sizes=sizes, versions=versions)
        entry["attempt"] = started.attempt
        usage, outcome, extras = Usage(), {"ok": False, "code": None}, {}
        try:
            bound = s.client.bound(deadline=deadline, usage=usage, models=cfg["models"], provider=cfg["provider"])
            future = _abandonable(asyncio.get_running_loop().run_in_executor(s.executor, work, bound, extras))
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=max(deadline.remaining(), 0) + GRACE)
            except asyncio.TimeoutError:
                raise DeadlineExceeded("The route's deadline passed.") from None
            body = respond(result, usage, extras)
            wire_v2.validate(kind, body, caller.client)
            outcome["ok"] = True
            return body
        except Exception as exc:  # noqa: BLE001 — every failure leaves as the envelope
            refusal = refusal_for(exc, kind)
            outcome["code"] = refusal.code
            raise refusal from None
        finally:
            totals = usage.close(lambda call: self._late_cost(started, call))
            try:
                await run_in_threadpool(s.meter.settle, started, ok=outcome["ok"], error_code=outcome["code"],
                                        totals=totals, reached_model=usage.reached_model,
                                        audio_seconds=extras.get("audio_seconds", audio_seconds),
                                        chunks=extras.get("chunks"))
            except Refusal:
                log.error("settle_failed", extra={"notch": {"event": "settle_failed", "kind": kind}})
            entry.update(model=",".join(totals["models"]) or None, provider=",".join(totals["providers"]) or None)
            s.zdr.submit(started.row_id, list(usage.calls), cfg["models"])

    def _late_cost(self, started, call):
        """A reply that came back after its call settled: count what it cost, and never raise into its thread."""
        try:
            self.s.meter.add_late_cost(started, call.get("cost") or 0.0)
        except Exception:  # noqa: BLE001
            log.error("late_cost_lost", extra={"notch": {"event": "late_cost_lost", "kind": started.kind}})

    # -- GET /v2/config ----------------------------------------------------------

    async def config(self, request: Request):
        async def answer():
            caller = await self.caller(request, required=False)
            request_entry(request.scope)["kind"] = "config"
            body = caller.cfg.public(caller.features)
            if caller.principal is not None:
                await run_in_threadpool(self.s.meter.mark_active, caller.user_id, caller.client.app_version,
                                        caller.client.platform)
                body["usage"] = await run_in_threadpool(self.s.meter.usage_today, caller.user_id)
            wire_v2.validate("config", body, caller.client)
            return JSONResponse(body)

        try:
            return await asyncio.wait_for(answer(), timeout=self.s.remote.current()["deadlines"]["config"])
        except asyncio.TimeoutError:
            raise Refusal("deadline_exceeded") from None

    # -- POST /v2/transcribe -----------------------------------------------------

    async def transcribe(self, request: Request):
        kind, s = "transcribe", self.s
        headers = request.headers
        wire_v2.Client.parse(headers.get("x-client"))
        key = wire_v2.idempotency_key(headers.get("idempotency-key"))
        locale = wire_v2.locale(headers.get("x-notch-locale"))
        mode = wire_v2.mode(headers.get("x-notch-mode"))
        claimed = wire_v2.claimed_duration(headers.get("x-notch-duration"))
        caller = await self.caller(request)
        cfg, entry = caller.cfg, request_entry(request.scope)
        entry["kind"] = kind
        deadline = Deadline(cfg["deadlines"][kind])
        self.admit(caller, kind, mode=mode)
        limits = cfg["limits"]
        if claimed > limits["max_recording_seconds"]:
            raise Refusal("audio_too_long")   # the client says so itself
        demuxer = DEMUXERS.get(headers.get("content-type", "").split(";")[0].strip().lower())
        if demuxer is None:
            raise Refusal("unsupported_audio")
        language = locale.split("-")[0] if locale and len(locale.split("-")[0]) == 2 else cfg["stt"]["language"]
        self.inflight.enter(caller.user_id, cfg["max_in_flight"])
        try:
            audio = await read_body(request, limits["max_audio_bytes"])
            entry["request_bytes"] = len(audio)
            if not audio:
                raise Refusal("audio_unreadable")
            body_hmac = s.body_hmac(audio)
            await self.transcribing.acquire(cfg["transcribe_concurrency"],
                                            min(TRANSCRIBE_WAIT, max(deadline.remaining(), 0) / 2))
            work_dir = None
            try:
                work_dir = await run_in_threadpool(tempfile.TemporaryDirectory, dir=s.tmp_root, prefix="notch-",
                                                   ignore_cleanup_errors=True)
                path = os.path.join(work_dir.name, "input")
                await run_in_threadpool(_write, path, audio)
                del audio
                probe = await self._probe(path, demuxer, deadline)
                entry["audio_seconds"] = round(probe.seconds, 2)
                if probe.seconds > limits["max_recording_seconds"]:
                    raise Refusal("audio_too_long")

                def work(bound, extras):
                    transcript, pieces = speech.transcribe(bound, path, demuxer, probe, work=work_dir.name,
                                                           tool=s.audio, stt=cfg["stt"], language=language,
                                                           deadline=deadline)
                    extras["chunks"] = pieces
                    return transcript

                def respond(transcript, usage, extras):
                    billed = usage.totals()["seconds"]
                    seconds = round(billed if billed else probe.seconds, 2)
                    if billed and abs(billed - probe.seconds) > 0.1 * probe.seconds:
                        alert("audio_seconds_mismatch", kind=kind, audio_seconds=round(probe.seconds, 2))
                    extras["audio_seconds"] = seconds
                    entry["chunks"] = extras["chunks"]
                    return {"transcript": transcript, "word_count": len(transcript.split()), "audio_seconds": seconds,
                            "chunks": extras["chunks"], "config_version": cfg.version}

                body = await self.run(request, caller, kind=kind, key=key, body_hmac=body_hmac,
                                      sizes={"request_bytes": entry["request_bytes"], "audio_seconds": probe.seconds},
                                      prompt_version=None, deadline=deadline, work=work, respond=respond,
                                      audio_seconds=probe.seconds)
            finally:
                if work_dir is not None:
                    await run_in_threadpool(work_dir.cleanup)
                await self.transcribing.release()
        finally:
            self.inflight.leave(caller.user_id)
        return JSONResponse(body)

    async def _probe(self, path, demuxer, deadline):
        """Decode the upload once, on the work pool, under the deadline."""
        future = _abandonable(asyncio.get_running_loop().run_in_executor(self.s.executor, self.s.audio.probe, path,
                                                                         demuxer, deadline))
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=max(deadline.remaining(), 0) + GRACE)
        except asyncio.TimeoutError:
            raise Refusal("deadline_exceeded") from None
        except Exception as exc:  # noqa: BLE001
            raise refusal_for(exc, "transcribe") from None

    # -- POST /v2/analyze and /v2/takeaways ---------------------------------------

    async def _labelled(self, request, kind):
        """The shared front half of analyze and takeaways -> (caller, key, raw body, fields, deadline)."""
        headers = request.headers
        wire_v2.Client.parse(headers.get("x-client"))
        key = wire_v2.idempotency_key(headers.get("idempotency-key"))
        wire_v2.locale(headers.get("x-notch-locale"))
        caller = await self.caller(request)
        cfg, entry = caller.cfg, request_entry(request.scope)
        entry["kind"] = kind
        deadline = Deadline(cfg["deadlines"][kind])
        self.admit(caller, kind)
        return caller, key, deadline

    async def _labelled_body(self, request, caller):
        cfg = caller.cfg
        raw = await read_body(request, cfg["max_request_json_bytes"])
        request_entry(request.scope)["request_bytes"] = len(raw)
        body = _fields(parse_json(raw), "body", ("transcript",), ("project_names", "vocabulary"))
        transcript = _transcript(body["transcript"], cfg)
        project_names = _strings(body.get("project_names"), "project_names", most=cfg["limits"]["project_names"])
        vocabulary = _strings(body.get("vocabulary"), "vocabulary", most=cfg["limits"]["vocabulary"])
        request_entry(request.scope)["input_chars"] = len(transcript)
        return raw, transcript, project_names, vocabulary

    async def analyze(self, request: Request):
        kind = "analyze"
        caller, key, deadline = await self._labelled(request, kind)
        cfg = caller.cfg
        self.inflight.enter(caller.user_id, cfg["max_in_flight"])
        try:
            raw, transcript, project_names, vocabulary = await self._labelled_body(request, caller)
            labels, check, version = _writing_prompts(cfg, kind)

            def work(bound, extras):
                return analysis.analyze_text(
                    bound, transcript, project_names=project_names, vocabulary=vocabulary, labels=labels,
                    classifier=cfg["classifier"], max_tokens=cfg["chat"]["max_tokens_analyze"],
                    thresholds=cfg["category_thresholds"], project_confidence=cfg["project_confidence"],
                    check=check)

            def respond(result, usage, extras):
                return {"summary": result["summary"], "takeaways": result["takeaways"], "tags": result["tags"],
                        "mood": result["mood"], "impact_note": result["impact_note"],
                        "acknowledged_by": result["acknowledged_by"],
                        "project_name": canonical_project(result["project_name"], project_names),
                        "categories": list(result["categories"]), "category_scores": result["category_scores"],
                        "classified_by": result["classified_by"], "config_version": cfg.version,
                        "prompt_version": version}

            body = await self.run(request, caller, kind=kind, key=key, body_hmac=self.s.body_hmac(raw),
                                  sizes={"request_bytes": len(raw), "input_chars": len(transcript)},
                                  prompt_version=version, deadline=deadline, work=work, respond=respond)
        finally:
            self.inflight.leave(caller.user_id)
        return JSONResponse(body)

    async def takeaways(self, request: Request):
        kind = "takeaways"
        caller, key, deadline = await self._labelled(request, kind)
        cfg = caller.cfg
        self.inflight.enter(caller.user_id, cfg["max_in_flight"])
        try:
            raw, transcript, project_names, vocabulary = await self._labelled_body(request, caller)
            labels, check, version = _writing_prompts(cfg, kind)

            def work(bound, extras):
                return analysis.write_takeaways(bound, transcript, project_names=project_names, vocabulary=vocabulary,
                                                labels=labels, max_tokens=cfg["chat"]["max_tokens_analyze"],
                                                check=check)

            def respond(result, usage, extras):
                return {"takeaways": result["takeaways"], "tags": result["tags"], "config_version": cfg.version,
                        "prompt_version": version}

            body = await self.run(request, caller, kind=kind, key=key, body_hmac=self.s.body_hmac(raw),
                                  sizes={"request_bytes": len(raw), "input_chars": len(transcript)},
                                  prompt_version=version, deadline=deadline, work=work, respond=respond)
        finally:
            self.inflight.leave(caller.user_id)
        return JSONResponse(body)

    # -- POST /v2/reports ---------------------------------------------------------

    async def reports(self, request: Request):
        kind = "reports"
        headers = request.headers
        wire_v2.Client.parse(headers.get("x-client"))
        key = wire_v2.idempotency_key(headers.get("idempotency-key"))
        wire_v2.locale(headers.get("x-notch-locale"))
        caller = await self.caller(request)
        cfg, entry = caller.cfg, request_entry(request.scope)
        entry["kind"] = kind
        deadline = Deadline(cfg["deadlines"][kind])
        self.admit(caller, kind)
        self.inflight.enter(caller.user_id, cfg["max_in_flight"])
        try:
            raw = await read_body(request, cfg["limits"]["max_json_bytes"])
            entry["request_bytes"] = len(raw)
            req = report_request(parse_json(raw), cfg, caller.client)
            entry["entry_count"] = len(req["entries"])
            facts = report_facts(req)
            version = cfg["prompts"][kind]
            writer = prompts.variant(kind, version)
            transcripts = len(req["entries"]) <= cfg["limits"]["report_transcripts_up_to"]

            def work(bound, extras):
                return write_report(bound, req, facts, writer, transcripts=transcripts,
                                    max_tokens=cfg["chat"]["max_tokens_report"],
                                    temperature=cfg["chat"]["report_temperature"])

            def respond(written, usage, extras):
                prose, themes, highlights = written
                return {"facts": facts, **prose, "themes": themes,
                        "highlights": [{"ordinal": n, **h} for n, h in enumerate(highlights)],
                        "source_entry_ids": [e["id"] for e in req["entries"]], "config_version": cfg.version,
                        "prompt_version": version}

            body = await self.run(request, caller, kind=kind, key=key, body_hmac=self.s.body_hmac(raw),
                                  sizes={"request_bytes": len(raw), "entry_count": len(req["entries"])},
                                  prompt_version=version, deadline=deadline, work=work, respond=respond)
        finally:
            self.inflight.leave(caller.user_id)
        return JSONResponse(body)

    # -- DELETE /v2/account -------------------------------------------------------

    async def delete_account(self, request: Request):
        """
        204 once the account is gone everywhere. Retrying after a lost 204 succeeds: the
        deleted id is not refused here, Apple is asked again only when a code is sent, and
        Supabase's 404 for a user already deleted is a success.
        """
        caller = await self.caller(request, account=False)
        entry = request_entry(request.scope)
        entry["kind"] = "account"
        cfg, principal, s = caller.cfg, caller.principal, self.s
        raw = await read_body(request, 16 * 1024)
        body = _fields(parse_json(raw) if raw.strip() else {}, "body", (), ("apple_authorization_code",))
        code = body.get("apple_authorization_code")
        if code is not None and (not isinstance(code, str) or not 0 < len(code) <= APPLE_CODE
                                 or not code.replace("-", "").replace(".", "").replace("_", "").isalnum()):
            raise bad("apple_authorization_code must be the code Sign in with Apple returned.")

        async def delete():
            already = await run_in_threadpool(s.meter.is_deleted, principal.user_id)
            if principal.apple_linked and code is None and not already:
                raise bad("An account linked to Apple must send apple_authorization_code.")
            if code is not None:
                if s.apple is None:
                    raise Refusal("unavailable")
                try:
                    await run_in_threadpool(s.apple.revoke, code)
                except AppleCodeRejected:
                    if not already:
                        raise bad("Apple did not accept apple_authorization_code.") from None
            if not principal.dev:
                if s.supabase_admin is None:
                    raise Refusal("unavailable")
                await run_in_threadpool(s.supabase_admin.delete_user, principal.user_id)
            await run_in_threadpool(s.meter.delete_account, principal.user_id)

        try:
            await asyncio.wait_for(delete(), timeout=cfg["deadlines"]["account"])
        except asyncio.TimeoutError:
            raise Refusal("deadline_exceeded") from None
        return Response(status_code=204)


def _abandonable(future):
    """
    A worker's future the route may stop waiting for at its deadline: whatever it ends
    with later is collected here, so asyncio never reports it as unretrieved.
    """
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# Reports: the request, the facts, the prompt. The device sends every notch in scope;
# the server counts them, so the numbers the model copies are the numbers the app shows.
# ---------------------------------------------------------------------------

_ENTRY_FIELDS = ("id", "date", "project_name", "tags", "categories", "is_milestone", "summary", "takeaways",
                 "impact_note", "acknowledged_by")


def report_request(body, cfg, client):
    """POST /v2/reports' body, checked -> a normalised request (entries oldest first)."""
    limits = cfg["limits"]
    body = _fields(body, "body", ("type", "range_start", "range_end", "range_label", "entries"),
                   ("scope", "author", "project_names"))
    if body["type"] not in wire_v2.enums_for(client)["report_type"]:
        raise bad("type is not a report type this app version knows.")
    start, end = _date(body["range_start"], "range_start"), _date(body["range_end"], "range_end")
    if end < start:
        raise bad("range_end is before range_start.")
    if (end - start).days + 1 > cfg["report_max_days"]:
        raise Refusal("range_too_large")
    scope = _fields(body.get("scope") or {}, "scope", (), ("project_name", "tag"))
    author = _fields(body.get("author") or {}, "author", (),
                     ("display_name", "role", "industry", "years_experience"))
    entries = body["entries"]
    if not isinstance(entries, list) or not entries:
        raise bad("entries must be a non-empty list.")
    if len(entries) > limits["report_max_entries"]:
        raise Refusal("range_too_large")
    parsed, seen = [], set()
    for raw in entries:
        e = _fields(raw, "entry", _ENTRY_FIELDS, ("transcript",))
        entry_id = _text(e["id"], "entry.id", limit=MAX_ID)
        if entry_id in seen:
            raise bad("entries has the same id twice.")
        seen.add(entry_id)
        day = _date(e["date"], "entry.date")
        if not start <= day <= end:
            raise bad("An entry's date is outside the range.")
        categories = e["categories"]
        if not isinstance(categories, list) or any(c not in prompts.CATEGORIES for c in categories):
            raise bad("entry.categories must name only the five categories.")
        if not isinstance(e["is_milestone"], bool):
            raise bad("entry.is_milestone must be true or false.")
        transcript = e.get("transcript")
        if transcript is not None and not isinstance(transcript, str):
            raise bad("entry.transcript must be a string.")
        parsed.append({
            "id": entry_id, "date": day, "project_name": _text(e["project_name"], "entry.project_name", nullable=True),
            "tags": store.normalize_tags(_strings(e["tags"], "entry.tags", most=50)),
            "categories": [c for c in prompts.CATEGORIES if c in categories], "is_milestone": e["is_milestone"],
            "summary": _text(e["summary"], "entry.summary", limit=4000, nullable=True),  # None: kept as transcript only
            "takeaways": _strings(e["takeaways"], "entry.takeaways", most=10, limit=1000),
            "impact_note": _text(e["impact_note"], "entry.impact_note", limit=1000, nullable=True),
            "acknowledged_by": _text(e["acknowledged_by"], "entry.acknowledged_by", nullable=True),
            "transcript": transcript.strip() if isinstance(transcript, str) and transcript.strip() else None})
    tag = scope.get("tag")
    return {
        "type": body["type"], "range_start": start, "range_end": end,
        "range_label": _text(body["range_label"], "range_label"),
        "scope": {"project_name": _text(scope.get("project_name"), "scope.project_name", nullable=True),
                  "tag": store.normalize_tag(_text(tag, "scope.tag")) if tag is not None else None},
        "author": {k: _text(author.get(k), f"author.{k}", nullable=True)
                   for k in ("display_name", "role", "industry", "years_experience")},
        "project_names": _strings(body.get("project_names"), "project_names", most=limits["project_names"]),
        "entries": sorted(parsed, key=lambda e: (e["date"], e["id"])),
    }


def report_facts(req):
    """The numbers the report shows, counted from the entries sent (reports.momentum and project_breakdown)."""
    entries = req["entries"]
    granularity, momentum = reports.momentum([e["date"] for e in entries], req["range_start"], req["range_end"])
    return {"notch_count": len(entries),
            "project_count": len({e["project_name"] for e in entries} - {None}),
            "milestone_count": sum(e["is_milestone"] for e in entries),
            "project_breakdown": reports.project_breakdown([e["project_name"] for e in entries]),
            "momentum": momentum, "momentum_granularity": granularity,
            "eyebrow": f"{_ADJECTIVE[req['type']]} report · {req['range_label']}"}


def report_message(req, facts, *, transcripts):
    """The writer's user message, laid out as v1's reports._user_message lays it out from the database."""
    author, scope, entries = req["author"], req["scope"], req["entries"]
    years = author["years_experience"]
    who = ", ".join(filter(None, [author["role"], author["industry"], years and f"{years} years' experience"]))
    scoped = ([f'the project "{scope["project_name"]}"'] if scope["project_name"] else []) + (
        [f"notches tagged #{scope['tag']}"] if scope["tag"] else [])
    total = facts["notch_count"]
    categories = Counter(c for e in entries for c in set(e["categories"]))
    recognitions = dict.fromkeys(e["acknowledged_by"] for e in entries if e["acknowledged_by"])
    lines = [
        f"Write the {_ADJECTIVE[req['type']].lower()} report for {author['display_name'] or 'this person'}"
        f"{f' ({who})' if who else ''}, covering {req['range_label']}.",
        f"Scope: {' and '.join(scoped) or 'every notch in the range'}.",
        "",
        "FACTS (numbers you may state, copied verbatim)",
        f"- notches: {total}",
        f"- projects: {facts['project_count']}",
        f"- milestones: {facts['milestone_count']}",
        "- project breakdown: " + ("; ".join(f"{p['name']}: {reports._notches(p['notch_count'])}, {p['share']}%"
                                            for p in facts["project_breakdown"]) or "none"),
        "- recognized by: " + (", ".join(recognitions) or "nobody named"),
        "",
        "CATEGORY COUNTS (context only: never name a category or state these counts)",
        "- " + ("; ".join(f"{c}: {categories[c]} of {reports._notches(total)}"
                          for c in sorted(categories, key=lambda c: (-categories[c], prompts.CATEGORIES.index(c))))
                or "none"),
        "",
        f"NOTCHES ({'with transcripts' if transcripts else 'summaries only'})",
    ]
    for e in entries:
        lines.append(f"[id {e['id']}] {e['date'].isoformat()} — project: {e['project_name'] or 'none'}"
                     f" — tags: {', '.join(e['tags']) or 'none'}"
                     f" — categories: {', '.join(e['categories']) or 'none'}"
                     + (" — milestone" if e["is_milestone"] else ""))
        lines.append(f"  summary: {e['summary'] or '(kept as a transcript only)'}")
        lines += [f"  takeaway: {t}" for t in e["takeaways"]]
        if e["impact_note"]:
            lines.append(f"  impact: {e['impact_note']}")
        if e["acknowledged_by"]:
            lines.append(f"  recognized by: {e['acknowledged_by']}")
        if transcripts and e["transcript"]:
            lines.append(f"  transcript: {e['transcript']}")
        lines.append("")
    return "\n".join(lines)


def write_report(client, req, facts, writer, *, transcripts, max_tokens, temperature):
    """One forced write_report call -> (prose, themes, highlights), repaired by reports._clean."""
    report_ids = {e["id"] for e in req["entries"]}
    milestone_ids = {e["id"] for e in req["entries"] if e["is_milestone"]}
    projects = req["project_names"] + [e["project_name"] for e in req["entries"] if e["project_name"]]
    return client.tool_call(
        system=writer.system, user=report_message(req, facts, transcripts=transcripts), tool_name="write_report",
        description="Write the Notch report document for this range.", parameters=writer.schema,
        temperature=temperature, max_tokens=max_tokens,
        parse=lambda doc: reports._clean(doc, report_ids, milestone_ids, projects))


def register(app, services):
    """Mount /v2 on `app`. Notch Cloud's routes are cloud.py's."""
    from . import cloud

    v2 = V2(services)
    app.add_api_route("/v2/config", v2.config, methods=["GET"])
    app.add_api_route("/v2/transcribe", v2.transcribe, methods=["POST"])
    app.add_api_route("/v2/analyze", v2.analyze, methods=["POST"])
    app.add_api_route("/v2/takeaways", v2.takeaways, methods=["POST"])
    app.add_api_route("/v2/reports", v2.reports, methods=["POST"])
    app.add_api_route("/v2/account", v2.delete_account, methods=["DELETE"])
    cloud.register(app, v2)
    return v2
