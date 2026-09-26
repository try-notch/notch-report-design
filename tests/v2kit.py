"""
Helpers the /v2 tests share: a settable wall clock, configs with test-sized limits, and a
key per call. Imported by the test modules; the fixtures that use them live in conftest.py.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from notch_api.remote_config import Config, build

# 2026-09-26 12:00:00 UTC: twelve hours before the UTC day turns over.
NOON = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc).timestamp()
MIDNIGHT = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc).timestamp()


class WallClock:
    """time.time() for the meter; a test moves it with .now or .advance()."""

    def __init__(self, now=NOON):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def config(version=0, **overrides):
    """A Config over DEFAULTS with `overrides` merged in, as if it were row `version`."""
    return Config(version, overrides, build(overrides))


def key():
    return str(uuid.uuid4())


def hmac_of(text):
    return hashlib.sha256(text.encode()).digest()
