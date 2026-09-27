"""
metrics.py — one JSON line per OpenRouter HTTP attempt, for notch_dash (see DASHBOARD.md).

Off (`path` None) unless notch_api.__main__ turns it on, so tests, seed and eval stay
silent. A line holds the model, the status, the latency, the usage numbers and the job
the call belonged to; never a body, a transcript, a prompt or a key. Recording never
raises into the call it measures, and opens the file per line so a deleted file comes back.
"""

import json
import threading
from contextvars import ContextVar

path = None
job = ContextVar("notch_job", default=None)  # {"job": "capture"|"report", "job_id": ...} inside a worker job
_lock = threading.Lock()
_USAGE = ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "input_tokens", "output_tokens", "seconds")


def kind(url):
    for part, name in (("/audio/transcriptions", "stt"), ("/audio/speech", "tts"), ("/alpha/decisions", "classify")):
        if part in url:
            return name
    return "chat"


def _outcome(url, status, response):
    """(ok, usage) for one attempt: ok as _post would take it; usage numbers from a 2xx JSON body."""
    if not (isinstance(status, int) and 200 <= status < 300):
        return False, None
    if kind(url) == "tts" and "json" not in response.headers.get("content-type", ""):
        return True, None  # raw audio
    try:
        body = response.json()
    except ValueError:
        return False, None
    if not isinstance(body, dict):
        return False, None
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    usage = {k: usage[k] for k in _USAGE if type(usage.get(k)) in (int, float)}
    return not (body.get("error") or first.get("error")), usage or None


def record(url, payload, attempt, wall, seconds, status, response):
    """One attempt: `wall` its time.time() start, `seconds` its span, `status` an HTTP code, "timeout" or "error"."""
    if path is None:
        return
    try:
        ok, usage = _outcome(url, status, response)
        choice = payload.get("tool_choice")
        tags = job.get() or {}
        event = {"ts": round(wall, 3), "kind": kind(url), "model": payload.get("model"),
                 "tool": choice["function"]["name"] if isinstance(choice, dict) else None,
                 "status": status, "ok": ok, "latency_ms": round(seconds * 1000, 1), "attempt": attempt,
                 "job": tags.get("job"), "job_id": tags.get("job_id"), "usage": usage}
        line = json.dumps(event) + "\n"
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
