"""
contract.py — the iOS §5 wire shapes as JSON Schema, and one validate() for all of them.

Every route validates its own response against these before returning it, and the
tests and the E2E run validate what they receive. That makes the contract
executable: a field the client does not expect, a missing one, a hashed tag, or a
`complete` entry with no summary fails loudly here instead of as a decode error on
a phone.

`additionalProperties: false` everywhere is the point, not strictness for its own
sake: it is what proves `categories` (internal) never reaches the wire.

Where the iOS doc contradicts itself, these schemas follow the decisions in the
build spec (SERVER.md lists them): momentum is [{date, count}] plus a sibling
momentum_granularity, counts are {notches, projects, milestones}, and highlight
provenance is source_entry_ids: [..].
"""

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match


class ContractError(ValueError):
    """An object does not match its wire schema."""


_INSTANT = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}
_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_TAG = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9:-]*$"}
_ID = {"type": "string", "minLength": 1}
_COUNT = {"type": "integer", "minimum": 0}
_MOODS = ["up", "flat", "down"]
_REPORT_TYPES = ["week", "month", "quarter", "year", "custom"]

# The closed failure-code set a failed job may carry (§5 "Error envelope").
JOB_FAILURE_CODES = ["transcription_failed", "model_refused", "model_unavailable", "audio_unreadable"]


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties, required=None):
    """A closed object. Every property is required unless `required` says otherwise."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def _if(prop, value, then, otherwise=None):
    """When `prop` equals `value`, these properties must match `then`, else `otherwise`."""
    rule = {"if": {"properties": {prop: {"const": value}}}, "then": {"properties": then}}
    if otherwise:
        rule["else"] = {"properties": otherwise}
    return rule


_ENTRY = _object({
    "id": _ID,
    "recorded_at": _INSTANT,
    "duration_seconds": {"type": "number", "minimum": 0},
    "word_count": _COUNT,
    "summary": _nullable({"type": "string"}),
    "transcript": _nullable({"type": "string"}),
    "takeaways": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": _TAG},
    "project_id": _nullable(_ID),
    "project": _nullable({"type": "string"}),
    "mood": _nullable({"enum": _MOODS}),
    "is_milestone": {"type": "boolean"},
    "acknowledged_by": _nullable({"type": "string"}),
    "impact_note": _nullable({"type": "string"}),
    "mode": {"enum": ["daily", "catch_up"]},
    "catch_up_span": _nullable(_object({"start": _DATE, "end": _DATE})),
    "analysis_state": {"enum": ["pending", "transcribing", "analyzing", "complete", "failed"]},
    "analysis_failure_code": _nullable({"type": "string"}),
    "retryable_until": _nullable(_INSTANT),
    "updated_at": _INSTANT,
})
_ENTRY["allOf"] = [
    # A complete notch is one every card can render.
    _if("analysis_state", "complete", {"summary": {"type": "string"}, "transcript": {"type": "string"},
                                       "mood": {"enum": _MOODS}, "word_count": {"minimum": 1}}),
    # §3.3's CHECK ((analysis_state = 'failed') = (analysis_failure_code IS NOT NULL)).
    _if("analysis_state", "failed", {"analysis_failure_code": {"type": "string"}},
        {"analysis_failure_code": {"type": "null"}}),
    # daily <=> no span.
    _if("mode", "daily", {"catch_up_span": {"type": "null"}}, {"catch_up_span": {"type": "object"}}),
]

# A job answers for exactly one record: an entry or a report.
_ONE_RECORD = {"oneOf": [{"required": ["entry_id"]}, {"required": ["report_id"]}]}

_JOB = {"oneOf": [
    _object({"status": {"const": "processing"}, "entry_id": _ID, "report_id": _ID,
             "poll_after_ms": _COUNT}, required=["status", "poll_after_ms"]) | _ONE_RECORD,
    _object({"status": {"const": "failed"}, "entry_id": _ID, "report_id": _ID,
             "code": {"enum": JOB_FAILURE_CODES}, "message": {"type": "string"},
             "retryable_until": _nullable(_INSTANT)},
            required=["status", "code", "message", "retryable_until"]) | _ONE_RECORD,
    _object({"status": {"const": "complete"}, "entry_id": _ID, "entry": {"$ref": "#/$defs/entry"}}),
    _object({"status": {"const": "complete"}, "report_id": _ID}),
]}

_PROJECT = _object({"id": _ID, "name": {"type": "string", "minLength": 1},
                    "notch_count": _COUNT, "share": {"type": "integer", "minimum": 0, "maximum": 100}})

_REPORT = _object({
    "id": _ID,
    "type": {"enum": _REPORT_TYPES},
    "range_start": _DATE,
    "range_end": _DATE,
    "range_label": {"type": "string", "minLength": 1},
    "headline": _nullable({"type": "string"}),
    "eyebrow": _nullable({"type": "string"}),
    "lede": _nullable({"type": "string"}),
    "body": _nullable({"type": "string"}),
    "generated_at": _INSTANT,
    "counts": _object({"notches": _COUNT, "projects": _COUNT, "milestones": _COUNT}),
    "momentum": {"type": "array", "items": _object({"date": _DATE, "count": _COUNT})},
    "momentum_granularity": {"enum": ["day", "week", "month"]},
    "project_breakdown": {"type": "array", "items": _object({
        "name": {"type": "string", "minLength": 1}, "notch_count": _COUNT,
        "share": {"type": "integer", "minimum": 0, "maximum": 100}})},
    "highlights": {"type": "array", "items": _object({
        "ordinal": _COUNT,
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "kind": {"enum": ["milestone", "shipped", "collaboration", "note"]},
        "source_entry_ids": {"type": "array", "items": _ID}})},
    "source_entry_ids": {"type": "array", "items": _ID},
    "themes": {"type": "array", "items": _TAG},
})

_DEFS = {
    "entry": _ENTRY,
    "entry_accepted": _object({"job_id": _ID, "entry_id": _ID}),
    "report_accepted": _object({"job_id": _ID, "report_id": _ID}),
    "job": _JOB,
    "project": _PROJECT,
    "project_list": _object({"projects": {"type": "array", "items": _PROJECT}}),
    "report": _REPORT,
    "report_list": _object({
        "reports": {"type": "array", "items": _object({
            "id": _ID, "range_label": {"type": "string"}, "headline": _nullable({"type": "string"}),
            "type": {"enum": _REPORT_TYPES}, "generated_at": _INSTANT})},
        "next_cursor": {"type": "null"},
    }),
    "error": _object({"error": _object({"code": {"type": "string", "minLength": 1},
                                        "message": {"type": "string"},
                                        "retryable": {"type": "boolean"}})}),
}

KINDS = tuple(_DEFS)

_VALIDATORS = {
    kind: Draft202012Validator({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _DEFS,
        "$ref": f"#/$defs/{kind}",
    })
    for kind in KINDS
}


def validate(kind, obj):
    """Raise ContractError, naming the offending path, unless `obj` is a valid `kind`."""
    error = best_match(_VALIDATORS[kind].iter_errors(obj))
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise ContractError(f"{kind}: {where}: {error.message}")
