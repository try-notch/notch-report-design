"""
services.py — everything /v2 needs, built once: the meter, remote config, the token
verifier, the model client, the audio tool, the ZDR auditor, the account-deletion calls,
the temp root and the body-HMAC key. create_app(services=...) mounts /v2 with them; tests
build one with fakes, and `from_env` builds the real one.

THE ENVIRONMENT (see deploy/notch.env.example):
  NOTCH_ENV=prod          refuses to start without SUPABASE_URL, SUPABASE_SECRET_KEY,
                          NOTCH_BODY_HMAC_KEY and NOTCH_TMP, and with NOTCH_DEV_AUTH=1
  NOTCH_DEV_AUTH=1        `Bearer dev` is the development user (never in prod)
  SUPABASE_URL            the project; its JWKS verifies tokens
  SUPABASE_SECRET_KEY     the admin API key, for DELETE /v2/account
  NOTCH_METER_DB          the meter's SQLite file
  NOTCH_TMP               where ffmpeg's per-request directories go: a tmpfs on the VPS
  NOTCH_BODY_HMAC_KEY     the key body HMACs are taken under (32+ characters)
  APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_CLIENT_ID, APPLE_PRIVATE_KEY_PATH  Apple token revocation
  OPENROUTER_API_KEY      read on the first model call, as on /v1

THE BODY KEY. Idempotency checks compare HMACs of request bodies, so the key must stay
the same across restarts, or every retry after one would look like a reused key. Outside
prod, with no key set, a fixed development key is used; it protects nothing and is only
there so a laptop run behaves like the server.
"""

import hashlib
import hmac
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config
from .auth import Authenticator, Verifier
from .identity import AppleRevoker, SupabaseAdmin
from .meter import Meter
from .remote_config import RemoteConfig
from .speech import FFmpeg
from .worker import LazyClient
from .zdr import ZdrAuditor

DEV_BODY_KEY = hashlib.sha256(b"notch development body hmac: not a secret, never used in prod").digest()


@dataclass
class Services:
    meter: Meter
    remote: RemoteConfig
    auth: Authenticator
    client: object                      # OpenRouterClient-shaped: bound(), generation(), zdr_endpoints()
    audio: object                       # speech.FFmpeg-shaped: probe(), encode()
    zdr: ZdrAuditor
    tmp_root: str
    body_key: bytes = DEV_BODY_KEY
    apple: AppleRevoker | None = None
    supabase_admin: SupabaseAdmin | None = None
    prod: bool = False
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(32, thread_name_prefix="notch-v2"))

    def body_hmac(self, body):
        return hmac.new(self.body_key, body, hashlib.sha256).digest()

    def shutdown(self):
        self.zdr.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def build(cls, *, meter_db, tmp_root, client, audio=None, verifier=None, dev_auth=False, zdr_inline=False,
              clock=None, **extra):
        """A Services over one meter file, with the given pieces and fakes where a test wants them."""
        meter = Meter(meter_db, clock=clock) if clock else Meter(meter_db)
        remote = RemoteConfig(meter)
        os.makedirs(tmp_root, exist_ok=True)
        return cls(meter=meter, remote=remote, auth=Authenticator(verifier, dev_auth=dev_auth), client=client,
                   audio=audio or FFmpeg(), zdr=ZdrAuditor(client, meter, remote, inline=zdr_inline),
                   tmp_root=tmp_root, **extra)

    @classmethod
    def from_env(cls, environ=os.environ, *, client=None, audio=None):
        prod = environ.get("NOTCH_ENV") == "prod"
        dev_auth = environ.get("NOTCH_DEV_AUTH") == "1"
        supabase_url = environ.get("SUPABASE_URL", "").strip()
        body_key = environ.get("NOTCH_BODY_HMAC_KEY", "").strip()
        tmp_root = environ.get("NOTCH_TMP", "").strip()
        if prod:
            missing = [name for name, value in (("SUPABASE_URL", supabase_url),
                                                ("SUPABASE_SECRET_KEY", environ.get("SUPABASE_SECRET_KEY", "").strip()),
                                                ("NOTCH_TMP", tmp_root)) if not value]
            if missing:
                raise RuntimeError(f"NOTCH_ENV=prod needs {', '.join(missing)}.")
            if dev_auth:
                raise RuntimeError("NOTCH_DEV_AUTH=1 is refused when NOTCH_ENV=prod.")
            if len(body_key) < 32:
                raise RuntimeError("NOTCH_ENV=prod needs NOTCH_BODY_HMAC_KEY of at least 32 characters.")
        return cls.build(
            meter_db=environ.get("NOTCH_METER_DB") or config.METER_DB,
            tmp_root=tmp_root or os.path.join(tempfile.gettempdir(), "notch-tmp"),
            client=client if client is not None else LazyClient(), audio=audio,
            verifier=Verifier(supabase_url) if supabase_url else None, dev_auth=dev_auth,
            body_key=body_key.encode() if body_key else DEV_BODY_KEY,
            apple=AppleRevoker.from_env(environ), supabase_admin=SupabaseAdmin.from_env(environ), prod=prod)
