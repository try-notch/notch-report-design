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
  model_refused         a 4xx, or a reply without the tool call we forced (after one
                        more try). Retrying the same request will not change the answer.
  transcription_failed  the audio was heard and contained no speech.

The HTTP layer is injectable (`http=httpx.Client(transport=httpx.MockTransport(...))`)
so the retry and parsing rules are tested offline.
"""

import base64
import json
import os
import time
from email.utils import parsedate_to_datetime

import httpx

from .config import CHAT_MODEL, DECISIONS_URL, JEV_MODEL, OPENROUTER_BASE, STT_MODEL, TTS_MODEL, TTS_VOICE

# A Retry-After longer than this is not worth holding a worker thread for.
MAX_RETRY_AFTER_SECONDS = 60


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


class TranscriptionFailed(ModelError):
    code = "transcription_failed"
    retryable = False


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


def _json_body(response):
    """
    A 2xx body -> (object, None), or (None, reason) when it is worth retrying.

    OpenRouter can answer 200 with an in-band {"error": {"code": ...}} when the
    upstream provider fails; that is retried like the HTTP status it names.
    """
    try:
        body = response.json()
    except ValueError:
        return None, "unparsable body"
    error = body.get("error") if isinstance(body, dict) else "not a JSON object"
    if not error:
        return body, None
    reason = f"in-band error: {_error_detail(response.text)}"
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and _transient(code):
        return None, reason
    raise ModelRefused(reason)


def _tool_arguments(body, tool_name):
    """A chat completion -> (the forced call's arguments as a dict, None), or (None, why not)."""
    try:
        arguments = body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
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

        Retried: transport errors (connect/read timeouts included), 408, 429, 5xx, and
        a 2xx whose body is not a JSON object or carries OpenRouter's in-band
        {"error": {"code": 429 or 5xx}}. Any other 4xx is final: ModelRefused. A `raw`
        (audio) request answered with JSON is read as JSON, so an in-band error there
        is classified the same way instead of being handed on as audio.
        """
        reason = "no attempt made"
        for attempt in range(self.max_attempts):
            wait = self.backoff * 2 ** attempt
            try:
                response = self._http.post(url, json=payload, headers=self._headers)
            except httpx.TransportError as exc:
                reason = type(exc).__name__
            else:
                status = response.status_code
                if _transient(status):
                    reason = f"HTTP {status}: {_error_detail(response.text)}"
                    after = _retry_after(response)
                    wait = wait if after is None else after
                elif status >= 400:
                    raise ModelRefused(f"HTTP {status}: {_error_detail(response.text)}")
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
                time.sleep(wait)
        raise ModelUnavailable(f"OpenRouter unavailable after {self.max_attempts} attempts ({reason}).")

    # -- the model jobs -----------------------------------------------------

    def tool_call(self, *, system, user, tool_name, description, parameters,
                  temperature=0.0, max_tokens=4000):
        """
        One forced tool call; returns its arguments as a dict.

        A 200 without a usable call (none, unparsable JSON, not an object) is asked
        once more before it counts as a refusal: it is usually a one-off.
        """
        payload = {
            "model": CHAT_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": {
                "name": tool_name, "description": description, "parameters": parameters}}],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
            "temperature": temperature,
            "max_tokens": max_tokens,
            # With reasoning on, the chat model answers in prose and skips the forced call.
            "reasoning": {"enabled": False},
            # Route only to providers that honour every parameter above, tool_choice
            # included; otherwise the forced call can silently come back as prose.
            "provider": {"require_parameters": True},
        }
        for _ in range(2):
            arguments, problem = _tool_arguments(self._post(OPENROUTER_BASE + "/chat/completions", payload),
                                                 tool_name)
            if problem is None:
                return arguments
        raise ModelRefused(problem)

    def decide(self, state, questions):
        """
        Jev typed decisions: {name: question} about `state` -> {name: answer}. Same
        retries as every other call; classify.py reads the answers.
        """
        body = self._post(DECISIONS_URL, {"model": JEV_MODEL, "state": state, "questions": questions})
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise ModelRefused("The decisions response has no answers.")
        return answers

    def transcribe(self, audio, *, fmt="wav", language="en"):
        """Speech-to-text. Audio with no speech in it is TranscriptionFailed, not an empty notch."""
        body = self._post(OPENROUTER_BASE + "/audio/transcriptions", {
            "model": STT_MODEL,
            "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": fmt},
            "language": language,
        })
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
