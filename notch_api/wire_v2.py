"""
wire_v2.py — the /v2 wire (docs/backend-contract.md in notch-ios-dev): the header rules,
the error codes, and the response schemas with their closed enums per contract version.

THE ERROR ENVELOPE IS {"error": {code, message, retryable}}, plus `resets_at` on a
429 quota_exceeded. Every code has one status, one retryable flag and one fixed message
(ERRORS below), so a message can never echo input: a Refusal may swap in a more precise
message, but only a literal from the code that raises it. The contract's "later" is
`retryable: true` with a Retry-After saying when.

CLOSED ENUMS PER CONTRACT VERSION. The device stores mood, highlight kind,
momentum_granularity, report type and classified_by in columns with CHECK constraints,
so a response may only carry values the client's build can store. X-Client names the
build; CONTRACTS maps it to the enum set it was built against, and every 2xx body is
validated against that set before it is sent. Growing an enum is a new contract version
here, gated by min_app_version, never a silent change.
"""

import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from . import prompts

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# code -> (status, retryable, message). The contract's table, plus the framework's 404/405/500.
ERRORS = {
    "invalid_request": (400, False, "The request is malformed."),
    "unauthorized": (401, False, "A valid access token is required."),
    "account_gone": (403, False, "This account has been deleted."),
    "account_blocked": (403, False, "This account cannot use Notch processing. Contact support."),
    "not_found": (404, False, "Not found."),
    "method_not_allowed": (405, False, "Method not allowed."),
    "request_in_flight": (409, True, "This request is already being processed."),
    "idempotency_key_reused": (409, False, "This Idempotency-Key was already used with a different body."),
    "payload_too_large": (413, False, "The request body is too large."),
    "audio_too_long": (413, False, "The recording is longer than allowed."),
    "transcript_too_long": (413, False, "The transcript is longer than allowed."),
    "cloud_record_too_large": (413, False, "A Notch Cloud record is larger than allowed."),
    "cloud_quota_exceeded": (413, False, "This account's Notch Cloud storage is full."),
    "unsupported_audio": (415, False, "This audio format is not supported."),
    "audio_unreadable": (422, False, "The recording could not be read as audio."),
    "no_speech": (422, False, "No speech was found in the recording."),
    "model_refused": (422, False, "The model could not process this."),
    "range_too_large": (422, False, "The report covers too many notches or days."),
    "app_update_required": (426, False, "This version of the app needs an update."),
    "quota_exceeded": (429, True, "This account's daily limit has been reached."),
    "rate_limited": (429, True, "Too many requests are in progress."),
    "attempts_exhausted": (429, False, "This request was tried too many times in the last hour."),
    "internal_error": (500, False, "Something went wrong on the server."),
    "model_unavailable": (502, True, "The model service is unavailable. Try again later."),
    "feature_disabled": (503, False, "This feature is turned off."),
    "processing_paused": (503, True, "Processing is paused. Try again later."),
    "unavailable": (503, True, "The service is unavailable. Try again later."),
    "deadline_exceeded": (504, True, "The request took too long."),
}


# Seconds of Retry-After for a 429 or 503 raised without its own (the contract puts one on every 429 and 503).
DEFAULT_RETRY_AFTER = {"unavailable": 5, "rate_limited": 5, "processing_paused": 300, "feature_disabled": 3600}


class Refusal(Exception):
    """A /v2 answer that is not a 2xx: one of ERRORS, with Retry-After and resets_at when they apply."""

    def __init__(self, code, message=None, *, retry_after=None, resets_at=None):
        status, retryable, default = ERRORS[code]
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable
        self.message = message or default
        if retry_after is None and status in (429, 503):
            retry_after = DEFAULT_RETRY_AFTER.get(code, 60)
        self.retry_after, self.resets_at = retry_after, resets_at

    def body(self):
        error = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.resets_at is not None:
            error["resets_at"] = self.resets_at
        return {"error": error}

    def headers(self):
        headers = {}
        if self.retry_after is not None:
            headers["Retry-After"] = str(max(1, int(-(-self.retry_after // 1))))  # whole seconds, rounded up
        if self.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return headers


def bad(message=None):
    return Refusal("invalid_request", message)


# ---------------------------------------------------------------------------
# Headers. Validated, never trusted, never logged raw.
# ---------------------------------------------------------------------------

X_CLIENT = re.compile(r"(ios|android)/(\d{1,3})\.(\d{1,3})\.(\d{1,3})\+(\d{1,6})")
LOCALE = re.compile(r"[a-z]{2,3}(-[A-Z]{2})?")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MODES = ("daily", "catch_up")
DURATION = re.compile(r"\d{1,6}(\.\d{1,3})?")
VERSION = re.compile(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})")


class Client:
    """A validated X-Client: platform, version as a tuple, build."""

    def __init__(self, platform, version, build):
        self.platform, self.version, self.build = platform, version, build

    @property
    def app_version(self):
        return ".".join(map(str, self.version))

    @classmethod
    def parse(cls, value):
        match = X_CLIENT.fullmatch(value or "")
        if match is None:
            raise bad("X-Client is missing or malformed.")
        return cls(match[1], tuple(int(match[i]) for i in (2, 3, 4)), int(match[5]))


def parse_version(value):
    """'1.2.3' -> (1, 2, 3); ValueError otherwise."""
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("not a version")
    return tuple(int(match[i]) for i in (1, 2, 3))


def idempotency_key(value):
    """The Idempotency-Key as a lowercase uuid (iOS sends UUID().uuidString, upper case)."""
    if not UUID.fullmatch(value or ""):
        raise bad("Idempotency-Key must be a UUID.")
    return value.lower()


def locale(value):
    """X-Notch-Locale, or None when absent."""
    if value is None:
        return None
    if not LOCALE.fullmatch(value):
        raise bad("X-Notch-Locale is malformed.")
    return value


def mode(value):
    if value not in MODES:
        raise bad("X-Notch-Mode must be daily or catch_up.")
    return value


def claimed_duration(value):
    if not DURATION.fullmatch(value or ""):
        raise bad("X-Notch-Duration must be a number of seconds.")
    return float(value)


# ---------------------------------------------------------------------------
# Contract versions and their closed enums.
# ---------------------------------------------------------------------------

# contract version -> the enum set a client of that version can store.
ENUMS = {
    1: {"mood": list(prompts.MOODS), "highlight_kind": list(prompts.HIGHLIGHT_KINDS),
        "momentum_granularity": ["day", "week", "month"], "report_type": list(prompts.REPORT_TYPES),
        "classified_by": ["jev", "llm"]},
}
# platform -> [(first app version, contract version)], oldest first: a build speaks the
# newest contract whose first version it has reached, and a build older than every entry
# speaks the first.
CONTRACTS = {
    "ios": [((1, 0, 0), 1)],
    "android": [((1, 0, 0), 1)],
}


def contract_version(client):
    table = CONTRACTS[client.platform]
    chosen = table[0][1]
    for first, version in table:
        if client.version >= first:
            chosen = version
    return chosen


def enums_for(client):
    return ENUMS[contract_version(client)]


# ---------------------------------------------------------------------------
# Response schemas, per enum set.
# ---------------------------------------------------------------------------

_STRING = {"type": "string"}
_TEXT = {"type": "string", "minLength": 1}
_COUNT = {"type": "integer", "minimum": 0}
_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_TAG = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9:-]*$"}
_STRINGS = {"type": "array", "items": _STRING}
_VERSIONS = {"config_version": _COUNT, "prompt_version": _TEXT}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def _schemas(enums):
    return {
        "transcribe": _object({"transcript": _TEXT, "word_count": {"type": "integer", "minimum": 1},
                               "audio_seconds": {"type": "number", "minimum": 0},
                               "chunks": {"type": "integer", "minimum": 1}, "config_version": _COUNT}),
        "analyze": _object({
            "summary": _TEXT, "takeaways": _STRINGS, "tags": {"type": "array", "items": _TAG},
            "mood": {"enum": enums["mood"]}, "impact_note": _nullable(_STRING),
            "acknowledged_by": _nullable(_STRING), "project_name": _nullable(_TEXT),
            "categories": {"type": "array", "items": {"enum": list(prompts.CATEGORIES)}},
            "category_scores": _nullable({"type": "object", "additionalProperties": {
                "type": "number", "minimum": 0, "maximum": 1}}),
            "classified_by": {"enum": enums["classified_by"]}, **_VERSIONS}),
        "takeaways": _object({"takeaways": {"type": "array", "items": _TEXT, "minItems": 1},
                              "tags": {"type": "array", "items": _TAG}, **_VERSIONS}),
        "reports": _object({
            "facts": _object({
                "notch_count": _COUNT, "project_count": _COUNT, "milestone_count": _COUNT,
                "project_breakdown": {"type": "array", "items": _object({
                    "name": _TEXT, "notch_count": _COUNT, "share": {"type": "integer", "minimum": 0, "maximum": 100}})},
                "momentum": {"type": "array", "items": _object({"date": _DATE, "count": _COUNT})},
                "momentum_granularity": {"enum": enums["momentum_granularity"]},
                "eyebrow": _TEXT}),
            "headline": _TEXT, "lede": _TEXT, "body": _TEXT, "themes": {"type": "array", "items": _TAG},
            "highlights": {"type": "array", "items": _object({
                "ordinal": _COUNT, "title": _TEXT, "detail": _STRING, "kind": {"enum": enums["highlight_kind"]},
                "source_entry_ids": {"type": "array", "items": _TEXT}})},
            "source_entry_ids": {"type": "array", "items": _TEXT}, **_VERSIONS}),
        "config": _object({
            "config_version": _COUNT, "min_app_version": {"type": "string", "pattern": r"^\d{1,3}\.\d{1,3}\.\d{1,3}$"},
            "features": _object({k: {"type": "boolean"} for k in
                                 ("capture", "catch_up", "reports", "takeaways", "notch_cloud")}),
            "limits": _object({k: _COUNT for k in (
                "notches_per_day", "reports_per_day", "rewrites_per_day", "max_recording_seconds", "max_audio_bytes",
                "max_json_bytes", "vocabulary", "project_names", "report_max_entries", "report_transcripts_up_to")}),
            "usage": _object({"day_utc": _DATE, "notches": _COUNT, "reports": _COUNT, "rewrites": _COUNT}),
        }, required=["config_version", "min_app_version", "features", "limits"]),
        "cloud_put": _object({"cursor": _COUNT}),
        "cloud_changes": _object({
            "records": {"type": "array", "items": _object({
                "id": _TEXT, "deleted": {"type": "boolean"}, "ciphertext": _nullable(_STRING),
                "key_id": {"type": "string", "pattern": r"^[0-9a-f]{16}$"}, "seq": {"type": "integer", "minimum": 1}})},
            "cursor": _COUNT, "more": {"type": "boolean"}}),
        "keycheck": _object({"key_id": {"type": "string", "pattern": r"^[0-9a-f]{16}$"}, "verifier": _TEXT}),
        "health": _object({"ok": {"const": True}}),
    }


class ContractError(ValueError):
    """A /v2 body the client's contract version cannot take. The message names the path, never a value."""


_VALIDATORS = {}


def validate(kind, body, client=None):
    """Raise ContractError unless `body` is a valid `kind` for `client`'s contract version (the newest when None)."""
    enums = ENUMS[contract_version(client) if client is not None else max(ENUMS)]
    cache_key = json.dumps(enums, sort_keys=True)
    validators = _VALIDATORS.get(cache_key)
    if validators is None:
        validators = _VALIDATORS[cache_key] = {k: Draft202012Validator(s) for k, s in _schemas(enums).items()}
    error = best_match(validators[kind].iter_errors(body))
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise ContractError(f"{kind}: {where}: {error.validator}")
