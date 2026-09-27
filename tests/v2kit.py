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
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from notch_api.app import create_app
from notch_api.auth import Verifier
from notch_api.fakes import FakeApple, FakeAudio, FakeClient, FakeSupabaseAdmin
from notch_api.meter import connect
from notch_api.remote_config import Config, build
from notch_api.services import Services
from notch_api.wire_v2 import ERRORS, Client, validate
from notch_api.zdr import ZdrAuditor

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


# ---------------------------------------------------------------------------
# A /v2 server over the fakes.
# ---------------------------------------------------------------------------

USER = "55555555-5555-4555-8555-555555555555"
OTHER = "66666666-6666-4666-8666-666666666666"
IOS = "ios/1.0.0+42"


class Harness:
    """
    create_app(services=..., v1=False) over FakeClient, FakeAudio, FakeApple and
    FakeSupabaseAdmin, a meter on a WallClock, and tokens this run's key signs. The
    ZDR audit runs inline with no waits. Use it as a context manager (it runs the lifespan).
    """

    def __init__(self, tmp_path, *, fake=None, audio=None, overrides=None, dev_auth=False, apple=None, admin=None):
        self.clock = WallClock()
        self.fake = fake or FakeClient()
        self.audio = audio or FakeAudio()
        self.apple = apple or FakeApple()
        self.admin = admin or FakeSupabaseAdmin()
        self.signing = SigningKey("kid-1")
        self.jwks = JWKSStub(self.signing)
        self.tmp_root = str(tmp_path / "tmpfs")
        self.services = Services.build(meter_db=str(tmp_path / "meter.db"), tmp_root=self.tmp_root, client=self.fake,
                                       audio=self.audio, verifier=Verifier(SUPABASE_URL, http=self.jwks.http,
                                                                           cooldown=0),
                                       dev_auth=dev_auth, clock=self.clock, apple=self.apple,
                                       supabase_admin=self.admin)
        self.services.zdr = ZdrAuditor(self.fake, self.services.meter, self.services.remote, inline=True, waits=(0,),
                                       sleep=lambda seconds: None)
        if overrides:
            self.services.remote.push(overrides)
        self.app = create_app(services=self.services, v1=False)
        self.http = TestClient(self.app)

    def __enter__(self):
        self.http.__enter__()
        return self

    def __exit__(self, *exc):
        return self.http.__exit__(*exc)

    @property
    def meter(self):
        return self.services.meter

    def token(self, user=USER, **claims):
        return token(self.signing, sub=user, **claims)

    def headers(self, user=USER, *, request_key=None, client=IOS, auth=True, **extra):
        headers = {"X-Client": client}
        if auth:
            headers["Authorization"] = f"Bearer {self.token(user)}"
        if request_key is not None:
            headers["Idempotency-Key"] = request_key
        headers.update({k.replace("_", "-"): v for k, v in extra.items()})
        return headers

    def transcribe(self, recording, user=USER, *, request_key=None, content_type="audio/mp4", mode="daily",
                   duration="42", **extra):
        headers = self.headers(user, request_key=request_key or key(), Content_Type=content_type,
                               X_Notch_Mode=mode, X_Notch_Duration=duration, **extra)
        return self.http.post("/v2/transcribe", content=recording, headers=headers)

    def post(self, path, body, user=USER, *, request_key=None, **extra):
        return self.http.post(path, json=body, headers=self.headers(user, request_key=request_key or key(), **extra))

    def rows(self, sql, *args):
        conn = connect(self.meter.path)
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()


def refused(response, status, code):
    """The response is the /v2 envelope for `code`, with its fixed message and nothing else."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and body["error"]["code"] == code, body
    assert set(body["error"]) <= {"code", "message", "retryable", "resets_at"}
    assert body["error"]["retryable"] is ERRORS[code][1]
    if status in (429, 503):
        assert response.headers.get("retry-after", "").isdigit(), "429 and 503 carry Retry-After"
    return body["error"]


def ok(response, kind, client=IOS):
    """A 200 whose body is a valid `kind` for `client`'s contract version."""
    assert response.status_code == 200, response.text
    body = response.json()
    validate(kind, body, Client.parse(client))
    return body
