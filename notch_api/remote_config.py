"""
remote_config.py — what the server does, changeable without a deploy or an App Store release.

The meter DB's `config` table is append-only: each row is a version (its integer id) and
a JSON body of overrides, deep-merged over DEFAULTS below. The NEWEST ROW THAT VALIDATES
WINS; a row that does not validate is logged (`config_invalid`, once per version) and
skipped, so a bad push can never take the server down, and with no valid row at all the
server runs on DEFAULTS as config_version 0. A row overrides the defaults, never the row
before it, so rolling back is pushing the older body again.

Rows are added by `python -m notch_api.admin config push <file>` (which refuses one that
would not validate) and by zdr.py, which switches a path off when a provider it used is
not zero-retention. current() re-reads only when a newer version has appeared.

The config a phone sees (GET /v2/config) is a subset: `features` (without the server's
own `processing` switch), the phone-side `limits`, `min_app_version` and the version.
The server's backstops are twice the phone's daily limits (`backstop_multiplier`).
"""

import copy
import json
import logging
import threading

from jsonschema import Draft202012Validator

from . import config as env, prompts, wire_v2

log = logging.getLogger(__name__)

DEFAULTS = {
    "min_app_version": "1.0.0",
    "features": {"processing": True, "capture": True, "catch_up": True, "reports": True, "takeaways": True,
                 "notch_cloud": True},
    "models": {"stt": "openai/whisper-large-v3", "chat": "deepseek/deepseek-v4-pro-0813",
               "classifier": "typesafe/jev-1.13"},
    # "chat": the chat model classifies. "jev": Jev does, with the chat model as its fallback.
    "classifier": "chat",
    "prompts": {"analyze": "v4", "takeaways": "v4", "reports": "r1"},
    "category_thresholds": dict(env.CATEGORY_THRESHOLDS),
    "project_confidence": env.PROJECT_CONFIDENCE,
    # Sent on every chat call. zdr and data_collection are fixed: validation refuses anything else.
    "provider": {"zdr": True, "data_collection": "deny", "require_parameters": True},
    "stt": {"language": "en", "split_over_seconds": 480, "chunk_seconds": 300, "parallel": 3},
    "chat": {"max_tokens_analyze": 1500, "max_tokens_report": 4000, "report_temperature": 0.3},
    # Seconds the server may spend; the client waits 30 s longer.
    "deadlines": {"config": 5, "transcribe": 120, "analyze": 60, "takeaways": 45, "reports": 240, "account": 30},
    # What the phone enforces on its own local day, and the input caps it must respect.
    "limits": {"notches_per_day": 10, "reports_per_day": 5, "rewrites_per_day": 20,
               "max_recording_seconds": 1800, "max_audio_bytes": 25 * 1024 * 1024, "max_json_bytes": 1024 * 1024,
               "vocabulary": 100, "project_names": 500, "report_max_entries": 400, "report_transcripts_up_to": 60},
    "backstop_multiplier": 2,
    "spend": {"account_usd_per_day": 1.00, "global_usd_per_day": 20.00},
    "max_in_flight": 3,
    "attempts_per_hour": 5,
    # Transcriptions decoding and transcribing at once in the process (the VPS has 2 vCPUs);
    # one more waits up to 10 s for a slot, then is 503 unavailable.
    "transcribe_concurrency": 2,
    "max_transcript_chars": 40000,
    "max_request_json_bytes": 256 * 1024,   # analyze and takeaways; reports get limits.max_json_bytes
    "report_max_days": 400,
    "cloud": {"max_records": 500, "max_body_bytes": 4 * 1024 * 1024, "max_record_bytes": 256 * 1024,
              "account_bytes": 100 * 1024 * 1024, "page_bytes": 4 * 1024 * 1024},
    "zdr": {"list_ttl_seconds": 3600},
}

# The kind of call -> which phone limit its backstop doubles.
BACKSTOP_OF = {"transcribe": "notches_per_day", "analyze": "notches_per_day", "reports": "reports_per_day",
               "takeaways": "rewrites_per_day"}


def _closed(properties, **extra):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False, **extra}


_BOOL = {"type": "boolean"}
_POSITIVE = {"type": "integer", "minimum": 1}
_SECONDS = {"type": "number", "exclusiveMinimum": 0, "maximum": 600}
_UNIT = {"type": "number", "minimum": 0, "maximum": 1}
_MODEL = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$"}

SCHEMA = _closed({
    "min_app_version": {"type": "string", "pattern": r"^\d{1,3}\.\d{1,3}\.\d{1,3}$"},
    "features": _closed({k: _BOOL for k in DEFAULTS["features"]}),
    "models": _closed({k: _MODEL for k in DEFAULTS["models"]}),
    "classifier": {"enum": ["chat", "jev"]},
    "prompts": _closed({kind: {"enum": sorted(prompts.VARIANTS[kind])} for kind in DEFAULTS["prompts"]}),
    "category_thresholds": _closed({c: _UNIT for c in prompts.CATEGORIES}),
    "project_confidence": _UNIT,
    "provider": {"type": "object", "properties": {
        "zdr": {"const": True}, "data_collection": {"const": "deny"}, "require_parameters": {"const": True},
        "quantizations": {"type": "array", "items": {"type": "string"}},
        "only": {"type": "array", "items": {"type": "string"}},
        "ignore": {"type": "array", "items": {"type": "string"}},
        "order": {"type": "array", "items": {"type": "string"}},
        "sort": {"enum": ["price", "throughput", "latency"]},
    }, "required": ["zdr", "data_collection", "require_parameters"], "additionalProperties": False},
    "stt": _closed({"language": {"type": "string", "pattern": r"^[a-z]{2}$"}, "split_over_seconds": _POSITIVE,
                    "chunk_seconds": {"type": "integer", "minimum": 30}, "parallel": {"type": "integer",
                                                                                     "minimum": 1, "maximum": 8}}),
    "chat": _closed({"max_tokens_analyze": _POSITIVE, "max_tokens_report": _POSITIVE,
                     "report_temperature": {"type": "number", "minimum": 0, "maximum": 2}}),
    "deadlines": _closed({k: _SECONDS for k in DEFAULTS["deadlines"]}),
    "limits": _closed({k: _POSITIVE for k in DEFAULTS["limits"]}),
    "backstop_multiplier": {"type": "number", "minimum": 1, "maximum": 10},
    "spend": _closed({"account_usd_per_day": {"type": "number", "minimum": 0},
                      "global_usd_per_day": {"type": "number", "minimum": 0}}),
    "max_in_flight": _POSITIVE,
    "attempts_per_hour": _POSITIVE,
    "transcribe_concurrency": {"type": "integer", "minimum": 1, "maximum": 16},
    "max_transcript_chars": _POSITIVE,
    "max_request_json_bytes": _POSITIVE,
    "report_max_days": _POSITIVE,
    "cloud": _closed({k: _POSITIVE for k in DEFAULTS["cloud"]}),
    "zdr": _closed({"list_ttl_seconds": _POSITIVE}),
})
_VALIDATOR = Draft202012Validator(SCHEMA)


class ConfigInvalid(ValueError):
    """A config body that cannot run. The message names the path, never a value."""


def merge(base, overrides):
    """`overrides` deep-merged over a copy of `base`: objects merge key by key, anything else replaces."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def build(overrides):
    """A row's body -> the config it runs, or ConfigInvalid."""
    if not isinstance(overrides, dict):
        raise ConfigInvalid("<root>: the body must be a JSON object")
    data = merge(DEFAULTS, overrides)
    errors = sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
        raise ConfigInvalid(f"{where}: {errors[0].validator}")
    stt = data["stt"]
    if stt["chunk_seconds"] > stt["split_over_seconds"]:
        raise ConfigInvalid("stt/chunk_seconds: over split_over_seconds")
    return data


class Config:
    """One version of the config: `version`, the row's `overrides`, and `data` (merged and validated)."""

    def __init__(self, version, overrides, data):
        self.version, self.overrides, self.data = version, overrides, data

    def __getitem__(self, key):
        return self.data[key]

    def backstop(self, kind):
        """The server's per-UTC-day cap on distinct Idempotency-Keys of `kind`."""
        return int(self.data["limits"][BACKSTOP_OF[kind]] * self.data["backstop_multiplier"])

    def features_for(self, flags):
        """Config's features with an account's per-account overrides (accounts.flags) on top."""
        features = dict(self.data["features"])
        features.update({k: v for k, v in (flags or {}).items() if k in features and isinstance(v, bool)})
        return features

    def min_app_version(self):
        return wire_v2.parse_version(self.data["min_app_version"])

    def public(self, features):
        """The phone's view: GET /v2/config without its usage block."""
        return {"config_version": self.version, "min_app_version": self.data["min_app_version"],
                "features": {k: features[k] for k in ("capture", "catch_up", "reports", "takeaways", "notch_cloud")},
                "limits": dict(self.data["limits"])}


DEFAULT_CONFIG = Config(0, {}, build({}))


class RemoteConfig:
    """current() -> the newest valid Config in the meter DB, re-read only when a newer version appears."""

    def __init__(self, meter):
        self._meter = meter
        self._lock = threading.Lock()
        self._seen = None          # the newest version current() has looked at
        self._config = DEFAULT_CONFIG
        self._invalid = set()      # versions already logged as invalid

    def current(self):
        latest = self._meter.latest_config_version()
        with self._lock:
            if latest == self._seen:
                return self._config
            chosen = DEFAULT_CONFIG
            for version, body in self._meter.config_rows_newest_first():
                try:
                    chosen = Config(version, json.loads(body), None)
                    chosen.data = build(chosen.overrides)
                    break
                except (ValueError, ConfigInvalid, RecursionError) as exc:
                    if version not in self._invalid:
                        self._invalid.add(version)
                        log.error("config_invalid", extra={"notch": {"event": "config_invalid",
                                                                     "config_version": version,
                                                                     "error_code": type(exc).__name__}})
                    chosen = DEFAULT_CONFIG
            self._seen, self._config = latest, chosen
            return chosen

    def push(self, overrides, *, note=None, created_by=None):
        """Validate and append a row -> its version. ConfigInvalid leaves the table untouched."""
        build(overrides)
        return self._meter.append_config(json.dumps(overrides, sort_keys=True), note=note, created_by=created_by)
