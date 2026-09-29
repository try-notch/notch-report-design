"""
openrouter.py — the only module that talks HTTP to a model.

One key for every model job: the capture-time narrative and the report writing
(chat with a forced tool call), the typed decisions behind a notch's categories,
mood and project (Jev, on OpenRouter's alpha decisions endpoint), speech-to-text,
and — for E2E fixtures only — text-to-speech.

FAILURES ARE CLASSIFIED HERE, ONCE. Every failure leaves this module as one of
three ModelError subclasses whose `code` is a member of the §5 error envelope's
closed set, so a job row can store `exc.code` verbatim and the client can switch
on it:

  model_unavailable     the network, a 429 or a 5xx — after retrying with backoff.
                        Transient, so worth another try later.
  model_refused         a 4xx, or a reply without a usable tool call (after one more,
                        warmer try). Retrying the same request will not change the answer.
  transcription_failed  the audio was heard and contained no speech.

The HTTP layer is injectable (`http=httpx.Client(transport=httpx.MockTransport(...))`)
so the retry and parsing rules are tested offline.

/v2 BINDS A CLIENT TO ONE REQUEST. `client.bound(deadline=, usage=, models=, provider=)`
is a view on the same connection pool that:
  - stops at the request's Deadline: every attempt's timeout is at most the time left,
    a retry is only made while it still fits, and time running out mid-attempt is
    DeadlineExceeded (504 deadline_exceeded) rather than an outage;
  - writes what each model reply says it cost, which model and provider served it, and
    its generation id into `usage` (a Usage), for metering and the ZDR audit;
  - sends remote config's models, and its `provider` block (zdr, data_collection:
    "deny", require_parameters) on every chat call.
The unbound client is v1's, unchanged. `generation()` and `zdr_endpoints()` read
OpenRouter's metadata for zdr.py; they never reach a model.
"""

import base64
import json
import os
import threading
import time
from email.utils import parsedate_to_datetime

import httpx

from . import metrics
from .config import (CHAT_MODEL, DECISIONS_URL, JEV_MODEL, MAX_UPLOAD_BYTES, OPENROUTER_BASE, STT_MODEL,
                     TTS_MODEL, TTS_VOICE)

# A Retry-After longer than this is not worth holding a worker thread for.
MAX_RETRY_AFTER_SECONDS = 60
# A forced call re-asked after an unusable answer goes a little warmer: at 0 the same miss comes back.
RETRY_TEMPERATURE = 0.4
# An attempt is not started with less than this left on the request's deadline.
MIN_ATTEMPT_SECONDS = 1.0
METADATA_TIMEOUT = 10.0      # generation and ZDR-list lookups


class ModelError(Exception):
    code = "model_unavailable"
    retryable = True

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ModelUnavailable(ModelError):
    code = "model_unavailable"
    retryable = True


class ModelRefused(ModelError):
    code = "model_refused"
    retryable = False

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status  # the 4xx that refused, when it was one; /v2 reads 401/402/404 as an outage


class TranscriptionFailed(ModelError):
    code = "transcription_failed"
    retryable = False


class DeadlineExceeded(ModelError):
    """The request's deadline ran out before a model answered."""
    code = "deadline_exceeded"
    retryable = True


class Deadline:
    """The time one request may still spend, on the monotonic clock."""

    def __init__(self, seconds, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self):
        return self._end - self._clock()

    def expired(self):
        return self.remaining() <= 0


_MODEL_KINDS = ("chat", "stt", "classify", "tts")


class Usage:
    """
    What one request's model calls cost and who served them. Thread-safe: analysis asks
    Jev on a thread of its own and long audio is transcribed three pieces at a time.

    `calls` holds one dict per model reply that said anything about itself: kind (chat,
    stt, classify), model, provider, generation_id, cost, prompt_tokens,
    completion_tokens, seconds. `reached_model` turns true when any request is sent to
    a model, answered or not: that attempt may have been paid for. Once the request has
    settled, `late(on_late)` routes any further reply (a thread the deadline abandoned)
    to `on_late`, so its cost is still counted.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []
        self.reached_model = False
        self._late = None

    def reached(self):
        with self._lock:
            self.reached_model = True

    def add(self, call):
        with self._lock:
            self.calls.append(call)
            late = self._late
        if late is not None:
            late(call)

    def late(self, on_late):
        with self._lock:
            self._late = on_late

    def close(self, on_late):
        """
        Settle: the totals so far, and every reply from now on routed to `on_late`, both
        under one lock, so no reply is counted twice or missed.
        """
        with self._lock:
            self._late = on_late
            calls = list(self.calls)
        return self._sum(calls)

    def totals(self):
        """{cost, prompt_tokens, completion_tokens, seconds, models, providers} over every call so far."""
        with self._lock:
            calls = list(self.calls)
        return self._sum(calls)

    @staticmethod
    def _sum(calls):
        def total(key):
            values = [c[key] for c in calls if isinstance(c.get(key), (int, float))]
            return sum(values) if values else None

        return {"cost": total("cost") or 0.0, "prompt_tokens": total("prompt_tokens"),
                "completion_tokens": total("completion_tokens"), "seconds": total("seconds"),
                "models": sorted({c["model"] for c in calls if c.get("model")}),
                "providers": sorted({c["provider"] for c in calls if c.get("provider")})}


def _number(value):
    return value if type(value) in (int, float) and value >= 0 else None


def reply_usage(url, payload, response):
    """
    What a model reply says about itself -> a Usage call dict, or None when it is not a
    2xx JSON reply. Never reads the text: only `usage`, `model`, `provider`, `id` and the
    X-Generation-Id header.
    """
    kind = metrics.kind(url)
    if response is None or not 200 <= response.status_code < 300 or "json" not in response.headers.get(
            "content-type", "json"):
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    generation = response.headers.get("x-generation-id") or body.get("id")
    provider = body.get("provider")
    return {"kind": kind, "model": body.get("model") if isinstance(body.get("model"), str) else payload.get("model"),
            "provider": provider if isinstance(provider, str) else None,
            "generation_id": generation if isinstance(generation, str) else None,
            "cost": _number(usage.get("cost")),
            "prompt_tokens": _number(usage.get("prompt_tokens", usage.get("input_tokens"))),
            "completion_tokens": _number(usage.get("completion_tokens", usage.get("output_tokens"))),
            "seconds": _number(usage.get("seconds"))}


def _transient(status):
    """Worth retrying: a timeout, rate limiting, or the provider's own failure."""
    return status in (408, 429) or status >= 500


def _retry_after(response):
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), else None."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _error_detail(body):
    """OpenRouter's {"error": {"message": ...}} if present, else a short excerpt."""
    try:
        return str(json.loads(body)["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return body[:300]


def _choice(body):
    """A chat completion's first choice, or {} when it has none."""
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    return first if isinstance(first, dict) else {}


def _json_body(response):
    """
    A 2xx body -> (object, None), or (None, reason) when it is worth retrying: it is not a
    JSON object, or it carries OpenRouter's in-band {"error": {"code": ...}} for a failing
    upstream provider, either at the top level or in a choice that ended
    finish_reason "error". An in-band error is retried like the HTTP status it names.
    """
    try:
        body = response.json()
    except ValueError:
        return None, "unparsable body"
    if not isinstance(body, dict):
        return None, "not a JSON object"
    error = body.get("error") or _choice(body).get("error")
    if not error:
        return body, None
    code, detail = (error.get("code"), error.get("message")) if isinstance(error, dict) else (None, None)
    reason = f"in-band error: {str(detail or error)[:300]}"
    if isinstance(code, int) and _transient(code):
        return None, reason
    raise ModelRefused(reason)


def _answered(body):
    """' (finish_reason=..., provider=...)': what a refused completion said about itself, for the message."""
    choice = _choice(body)
    said = {"finish_reason": choice.get("finish_reason"), "native_finish_reason": choice.get("native_finish_reason"),
            "provider": body.get("provider"), "model": body.get("model")}
    said = ", ".join(f"{k}={v}" for k, v in said.items() if v)
    return f" ({said})" if said else ""


def _tool_arguments(body, tool_name):
    """A chat completion -> (the forced call's arguments as a dict, None), or (None, why not)."""
    choice = _choice(body)
    if choice.get("finish_reason") == "length":
        return None, f"{tool_name} was cut off at max_tokens."
    try:
        arguments = choice["message"]["tool_calls"][0]["function"]["arguments"]
    except (KeyError, IndexError, TypeError):
        return None, f"The model did not call {tool_name}."
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None, f"{tool_name} arguments are not valid JSON."
    if not isinstance(arguments, dict):
        return None, f"{tool_name} arguments are not an object."
    return arguments, None


class OpenRouterClient:
    def __init__(self, api_key, *, http=None, max_attempts=5, backoff=1.0):
        self._http = http or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "notch-report-design"}
        self.max_attempts = max_attempts
        self.backoff = backoff
        self._deadline = self._usage = self._provider = None
        self._models = {}

    def bound(self, *, deadline=None, usage=None, models=None, provider=None):
        """
        This client, for one /v2 request: the same connection pool and key, with the
        request's Deadline, its Usage collector, remote config's models ({chat, stt,
        classifier}) and the provider block every chat call must carry.
        """
        view = object.__new__(OpenRouterClient)
        view.__dict__.update(self.__dict__)
        view._deadline, view._usage = deadline, usage
        view._models, view._provider = dict(models or {}), dict(provider) if provider else None
        return view

    def within(self, seconds):
        """
        This client with at most `seconds` more to spend: the sooner of its own deadline
        and one `seconds` from now. Everything else (pool, key, usage, models, provider)
        is shared, so what the calls cost is still counted where the request counts it.
        """
        view = object.__new__(OpenRouterClient)
        view.__dict__.update(self.__dict__)
        own = self._deadline
        view._deadline = own if own is not None and own.remaining() <= seconds else Deadline(seconds)
        return view

    @classmethod
    def from_env(cls):
        """Build from OPENROUTER_API_KEY (config.py has already loaded .env)."""
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to .env or export it.")
        return cls(key)

    # -- transport ----------------------------------------------------------

    def _post(self, url, payload, *, raw=False):
        """
        POST with retries. Returns parsed JSON, or the body bytes when `raw`.

        Retried: request errors (connect/read timeouts, a bad gzip body), 408, 429, 5xx,
        and a 2xx whose body is not a JSON object or carries OpenRouter's in-band
        {"error": {"code": 429 or 5xx}}. Any other 4xx is final: ModelRefused. A `raw`
        (audio) request answered with JSON is read as JSON, so an in-band error there
        is classified the same way instead of being handed on as audio.
        """
        reason = "no attempt made"
        for attempt in range(self.max_attempts):
            wait = self.backoff * 2 ** attempt
            timeout = self._attempt_timeout()
            try:
                response = self._send(url, payload, attempt + 1, timeout)
            except httpx.TimeoutException as exc:
                if self._deadline is not None and self._deadline.remaining() < MIN_ATTEMPT_SECONDS:
                    raise DeadlineExceeded("The request's deadline ran out waiting for the model.") from None
                reason = type(exc).__name__
            except httpx.RequestError as exc:
                reason = type(exc).__name__
            else:
                status = response.status_code
                if _transient(status):
                    reason = f"HTTP {status}: {_error_detail(response.text)}"
                    after = _retry_after(response)
                    wait = wait if after is None else after
                elif status >= 400:
                    raise ModelRefused(f"HTTP {status}: {_error_detail(response.text)}", status)
                elif raw and "json" not in response.headers.get("content-type", ""):
                    if not response.content:
                        raise ModelRefused("Empty response body.")
                    return response.content
                else:
                    body, reason = _json_body(response)
                    if body is not None and raw:
                        raise ModelRefused(f"Expected audio, got JSON: {response.text[:300]}")
                    if body is not None:
                        return body
            if attempt + 1 < self.max_attempts:
                if self._deadline is not None and wait + MIN_ATTEMPT_SECONDS > self._deadline.remaining():
                    break  # no time left for another attempt: the outage is the answer
                time.sleep(wait)
        raise ModelUnavailable(f"OpenRouter unavailable after {attempt + 1} attempt(s) ({reason}).")

    def _attempt_timeout(self):
        """None (httpx's own) when unbound; else at most the time left, and DeadlineExceeded when it is gone."""
        if self._deadline is None:
            return None
        left = self._deadline.remaining()
        if left < MIN_ATTEMPT_SECONDS:
            raise DeadlineExceeded("The request's deadline ran out before the model was asked.")
        return min(left, 120.0)

    def _send(self, url, payload, attempt, timeout=None):
        """One HTTP attempt, recorded by metrics.py however it ends; returns or raises exactly what httpx did."""
        wall, start, status, response = time.time(), time.perf_counter(), "error", None
        extra = {} if timeout is None else {"timeout": httpx.Timeout(timeout, connect=min(15.0, timeout))}
        if self._usage is not None and metrics.kind(url) in _MODEL_KINDS:
            self._usage.reached()
        try:
            response = self._http.post(url, json=payload, headers=self._headers, **extra)
            status = response.status_code
            return response
        except httpx.TimeoutException:
            status = "timeout"
            raise
        finally:
            metrics.record(url, payload, attempt, wall, time.perf_counter() - start, status, response)
            if self._usage is not None:
                call = reply_usage(url, payload, response)
                if call is not None:
                    self._usage.add(call)

    # -- the model jobs -----------------------------------------------------

    def tool_call(self, *, system, user, tool_name, description, parameters,
                  temperature=0.0, max_tokens=4000, parse=None):
        """
        One forced tool call -> its arguments as a dict, or `parse(arguments)` when given.

        A 200 without a usable call (none, cut off, unparsable JSON, not an object), or
        whose arguments `parse` refuses with ModelRefused (a blank summary, say), is asked
        once more, at RETRY_TEMPERATURE, before it counts as a refusal: it is usually a
        one-off. The refusal says what the completion reported about itself.
        """
        payload = {
            "model": self._models.get("chat", CHAT_MODEL),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": {
                "name": tool_name, "description": description, "parameters": parameters}}],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
            "temperature": temperature,
            "max_tokens": max_tokens,
            # With reasoning on, the chat model answers in prose and skips the forced call.
            "reasoning": {"enabled": False},
            # Route only to providers that honour every parameter above, tool_choice
            # included; otherwise the forced call can silently come back as prose. A bound
            # client sends remote config's block, which also keeps routing to ZDR endpoints.
            "provider": dict(self._provider) if self._provider else {"require_parameters": True},
        }
        for _ in range(2):
            body = self._post(OPENROUTER_BASE + "/chat/completions", payload)
            arguments, problem = _tool_arguments(body, tool_name)
            if problem is None:
                try:
                    return parse(arguments) if parse else arguments
                except ModelRefused as exc:
                    problem = exc.message
            payload["temperature"] = max(temperature, RETRY_TEMPERATURE)
        raise ModelRefused(problem + _answered(body))

    def decide(self, state, questions):
        """
        Jev typed decisions: {name: question} about `state` -> {name: answer}. Same
        retries as every other call; classify.py reads the answers.
        """
        body = self._post(DECISIONS_URL, {"model": self._models.get("classifier", JEV_MODEL), "state": state,
                                          "questions": questions})
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise ModelRefused("The decisions response has no answers.")
        return answers

    def transcribe(self, audio, *, fmt="m4a", language="en"):
        """
        Speech-to-text. Audio with no speech in it is TranscriptionFailed, not an empty notch;
        audio too long for one request under OpenRouter's cap is refused without sending it.
        """
        data = base64.b64encode(audio).decode("ascii")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ModelRefused(f"{len(audio):,} bytes of audio are over OpenRouter's request cap once base64'd.")
        body = self._post(OPENROUTER_BASE + "/audio/transcriptions", {
            "model": self._models.get("stt", STT_MODEL), "input_audio": {"data": data, "format": fmt},
            "language": language})
        text = body.get("text")
        if not isinstance(text, str):
            raise ModelRefused("The transcription response has no text.")
        if not text.strip():
            raise TranscriptionFailed("No speech detected.")
        return text.strip()

    def speech(self, text, *, voice=TTS_VOICE, fmt="mp3"):
        """Text-to-speech, for generating E2E fixture audio. Returns the encoded audio bytes."""
        return self._post(OPENROUTER_BASE + "/audio/speech", {
            "model": TTS_MODEL, "input": text, "voice": voice, "response_format": fmt,
        }, raw=True)

    # -- metadata (zdr.py): never a model call, never metered ----------------

    def generation(self, generation_id):
        """
        GET /api/v1/generation?id= -> {"provider", "model"} for a finished call, or None
        while OpenRouter has no record of it yet (a 404: its stats land a moment after the
        reply). ModelUnavailable when OpenRouter cannot be asked.
        """
        body = self._get(OPENROUTER_BASE + "/generation", {"id": generation_id}, missing_ok=True)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return None
        provider, model = data.get("provider_name"), data.get("model")
        return {"provider": provider if isinstance(provider, str) and provider else None,
                "model": model if isinstance(model, str) and model else None}

    def zdr_endpoints(self):
        """GET /api/v1/endpoints/zdr -> [{"provider", "model"}], every endpoint OpenRouter lists as zero retention."""
        body = self._get(OPENROUTER_BASE + "/endpoints/zdr", None)
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ModelUnavailable("The ZDR endpoint list has no data.")
        endpoints = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") if isinstance(item.get("name"), str) else ""
            named_provider, _, named_model = name.partition(" | ")
            provider = item.get("provider_name") or named_provider
            model = item.get("model_id") or named_model or item.get("model_name")
            if isinstance(provider, str) and provider:
                endpoints.append({"provider": provider, "model": model if isinstance(model, str) and model else None})
        return endpoints

    def _get(self, url, params, missing_ok=False):
        try:
            response = self._http.get(url, params=params, headers=self._headers, timeout=METADATA_TIMEOUT)
        except httpx.RequestError as exc:
            raise ModelUnavailable(f"OpenRouter metadata unreachable ({type(exc).__name__}).") from None
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ModelUnavailable(f"OpenRouter metadata answered HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise ModelUnavailable("OpenRouter metadata answered with no JSON.") from None
