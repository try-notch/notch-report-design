"""
identity.py — the two outside calls DELETE /v2/account makes. Both are injectable, and
tests replace them with fakes.

AppleRevoker. Apple requires an app that offers Sign in with Apple to revoke the user's
Apple tokens when the account is deleted, and Supabase does not do it. The app re-runs
Sign in with Apple and sends the fresh authorization code; this exchanges it at
appleid.apple.com/auth/token, with a client secret signed by the team's .p8 key, and
revokes the refresh token (the access token, if Apple returned no refresh token) at
/auth/revoke. The tokens live only in this call's memory. Configured by APPLE_TEAM_ID,
APPLE_KEY_ID, APPLE_CLIENT_ID (the app's bundle id) and APPLE_PRIVATE_KEY_PATH.

SupabaseAdmin. Deletes the user through the Auth admin API with SUPABASE_SECRET_KEY,
which also ends every session. A new `sb_secret_` key goes only in the `apikey` header
(Supabase rejects it as a Bearer token); a legacy service_role key, a JWT, goes in both.
A user who is already gone (404) is a success, so a retry after a lost 204 succeeds.
"""

import time

import httpx
import jwt

from .wire_v2 import Refusal

APPLE = "https://appleid.apple.com"
TIMEOUT = 10.0


class AppleCodeRejected(Exception):
    """Apple refused the authorization code (expired, already used, or not this app's)."""


class AppleRevoker:
    def __init__(self, *, team_id, key_id, client_id, private_key, http=None, clock=time.time):
        self.team_id, self.key_id, self.client_id, self._key = team_id, key_id, client_id, private_key
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self.clock = clock

    @classmethod
    def from_env(cls, environ, *, http=None):
        """None unless all four APPLE_* settings are present."""
        names = ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_CLIENT_ID", "APPLE_PRIVATE_KEY_PATH")
        values = [environ.get(name, "").strip() for name in names]
        if not all(values):
            return None
        team_id, key_id, client_id, key_path = values
        with open(key_path, encoding="utf-8") as f:
            private_key = f.read()
        return cls(team_id=team_id, key_id=key_id, client_id=client_id, private_key=private_key, http=http)

    def client_secret(self):
        now = int(self.clock())
        return jwt.encode({"iss": self.team_id, "iat": now, "exp": now + 300, "aud": APPLE, "sub": self.client_id},
                          self._key, algorithm="ES256", headers={"kid": self.key_id})

    def _post(self, path, form):
        try:
            return self._http.post(APPLE + path, data=form, timeout=TIMEOUT)
        except httpx.RequestError:
            raise Refusal("unavailable") from None

    def revoke(self, code):
        """Exchange `code` and revoke what it yields. AppleCodeRejected, or Refusal('unavailable') on an outage."""
        secret = self.client_secret()
        answer = self._post("/auth/token", {"client_id": self.client_id, "client_secret": secret, "code": code,
                                            "grant_type": "authorization_code"})
        if answer.status_code == 400:
            raise AppleCodeRejected()
        if answer.status_code != 200:
            raise Refusal("unavailable")
        try:
            tokens = answer.json()
        except ValueError:
            raise Refusal("unavailable") from None
        hint = "refresh_token" if tokens.get("refresh_token") else "access_token"
        token = tokens.get(hint)
        if not isinstance(token, str) or not token:
            raise Refusal("unavailable")
        revoked = self._post("/auth/revoke", {"client_id": self.client_id, "client_secret": secret, "token": token,
                                              "token_type_hint": hint})
        if revoked.status_code != 200:
            raise Refusal("unavailable")


class SupabaseAdmin:
    def __init__(self, supabase_url, secret_key, *, http=None):
        self.base = supabase_url.rstrip("/") + "/auth/v1/admin/users/"
        self._headers = {"apikey": secret_key}
        if secret_key.startswith("eyJ") and secret_key.count(".") == 2:   # a legacy service_role JWT
            self._headers["Authorization"] = f"Bearer {secret_key}"
        self._http = http or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def from_env(cls, environ, *, http=None):
        url, key = environ.get("SUPABASE_URL", "").strip(), environ.get("SUPABASE_SECRET_KEY", "").strip()
        return cls(url, key, http=http) if url and key else None

    def delete_user(self, user_id):
        """Delete the Supabase user; one already gone is fine. Refusal('unavailable') otherwise."""
        try:
            answer = self._http.delete(self.base + user_id, headers=self._headers, timeout=TIMEOUT)
        except httpx.RequestError:
            raise Refusal("unavailable") from None
        if answer.status_code not in (200, 204, 404):
            raise Refusal("unavailable")
