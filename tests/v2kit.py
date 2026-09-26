"""
Helpers the /v2 tests share: a settable wall clock, configs with test-sized limits, a key
per call, and Supabase-shaped tokens signed with keys minted for the run (never committed).
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

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


# ---------------------------------------------------------------------------
# Supabase tokens.
# ---------------------------------------------------------------------------

SUPABASE_URL = "https://notch-test.supabase.test"
ISSUER = SUPABASE_URL + "/auth/v1"
JWKS_URL = ISSUER + "/.well-known/jwks.json"


class SigningKey:
    """A key pair minted for this run: .private signs, .jwk is what the JWKS endpoint publishes."""

    def __init__(self, kid, alg="ES256"):
        self.kid, self.alg = kid, alg
        if alg == "ES256":
            self.private = ec.generate_private_key(ec.SECP256R1())
            public = json.loads(ECAlgorithm.to_jwk(self.private.public_key()))
        else:
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            public = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        self.jwk = public | {"kid": kid, "alg": alg, "use": "sig"}


class JWKSStub:
    """The JWKS endpoint over httpx.MockTransport: .keys is what it serves, .fetches how often it was asked."""

    def __init__(self, *keys):
        self.keys, self.fetches, self.down = list(keys), 0, False

        def handler(request):
            assert str(request.url) == JWKS_URL, request.url
            self.fetches += 1
            if self.down:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [k.jwk for k in self.keys]})

        self.http = httpx.Client(transport=httpx.MockTransport(handler))


def token(key, *, sub="55555555-5555-4555-8555-555555555555", now=None, headers=None, alg=None, **claims):
    """A Supabase-shaped access token signed with `key`; `claims` override it (a None removes one)."""
    now = int(time.time()) if now is None else now
    body = {"iss": ISSUER, "aud": "authenticated", "role": "authenticated", "sub": sub, "iat": now,
            "exp": now + 3600, "is_anonymous": False, "session_id": "s",
            "app_metadata": {"provider": "email", "providers": ["email"]}} | claims
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key.private, algorithm=alg or key.alg, headers={"kid": key.kid} | (headers or {}))
