"""
auth.py — who is calling /v2: a Supabase access token, verified here, with no call to
Supabase per request.

  - Keys come from <SUPABASE_URL>/auth/v1/.well-known/jwks.json through PyJWT's
    PyJWKClient: the key set is cached for at most 10 minutes and fetched again when a
    token names a kid it does not hold (at most once per cooldown, so a flood of made-up
    kids cannot hammer Supabase). Fetching goes through httpx, like every other call
    this server makes, so tests stub it and the suite's network guard covers it.
  - ES256 is Supabase's signing algorithm; RS256 is also accepted. Nothing else: the
    token's own `alg` must be one of the two, and must match the key's.
  - The token must carry exp, iat and sub; iss must be <SUPABASE_URL>/auth/v1, aud and
    role must be "authenticated", and an anonymous user (is_anonymous) is refused.
  - Any failure is 401 unauthorized, whose message never says which check failed. A
    key set that cannot be fetched at all is 503 unavailable instead: the token may be
    fine, and a 401 would sign the person out.

Whether the account behind a valid token still exists is the meter's question
(deleted_accounts -> 403 account_gone), asked by the route.

DEV AUTH. With NOTCH_DEV_AUTH=1, `Bearer dev` is the one development user, as on /v1.
services.py refuses to build a server with it when NOTCH_ENV=prod.
"""

import time

import httpx
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWTError

from . import config
from .wire_v2 import UUID, Refusal

ALGORITHMS = ("ES256", "RS256")
AUDIENCE = "authenticated"
JWKS_LIFESPAN = 600       # seconds: "cached for at most 10 minutes"
JWKS_COOLDOWN = 30        # seconds between fetches forced by an unknown kid


class Principal:
    """The verified caller: the account id (a lowercase uuid) and the sign-in providers the token names."""

    def __init__(self, user_id, *, providers=(), dev=False):
        self.user_id, self.providers, self.dev = user_id, tuple(providers), dev

    @property
    def apple_linked(self):
        return "apple" in self.providers


class JWKSClient(PyJWKClient):
    """PyJWKClient, fetching the key set through an injectable httpx client."""

    def __init__(self, uri, *, http=None, lifespan=JWKS_LIFESPAN, cooldown=JWKS_COOLDOWN):
        super().__init__(uri, cache_keys=False, cache_jwk_set=True, lifespan=lifespan, cooldown_duration=cooldown,
                         timeout=10)
        self._http = http or httpx.Client(timeout=10.0)
        self.fetches = 0

    def fetch_data(self):
        try:
            response = self._http.get(self.uri)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PyJWKClientConnectionError(f"The key set could not be fetched ({type(exc).__name__}).") from None
        if not isinstance(data, dict):
            raise PyJWKClientError("The key set is not a JSON object.")
        self.fetches += 1
        self._last_successful_fetch = time.monotonic()  # starts PyJWKClient's unknown-kid cooldown
        return data


class Verifier:
    def __init__(self, supabase_url, *, http=None, jwks=None, cooldown=JWKS_COOLDOWN, leeway=0):
        self.issuer = supabase_url.rstrip("/") + "/auth/v1"
        self.jwks = jwks or JWKSClient(self.issuer + "/.well-known/jwks.json", http=http, cooldown=cooldown)
        self.leeway = leeway

    def verify(self, token):
        """A Supabase access token -> Principal, else Refusal (401 unauthorized, or 503 unavailable)."""
        try:
            header = jwt.get_unverified_header(token)
            alg, kid = header.get("alg"), header.get("kid")
            if alg not in ALGORITHMS or not isinstance(kid, str):
                raise Refusal("unauthorized")
            key = self.jwks.get_signing_key(kid)
            if key.algorithm_name != alg:
                raise Refusal("unauthorized")
            claims = jwt.decode(token, key.key, algorithms=[alg], audience=AUDIENCE, issuer=self.issuer,
                                leeway=self.leeway, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except PyJWKClientConnectionError:
            raise Refusal("unavailable") from None
        except (PyJWKClientError, PyJWTError, ValueError, TypeError):
            raise Refusal("unauthorized") from None
        sub = claims.get("sub")
        if claims.get("role") != AUDIENCE or claims.get("is_anonymous") is True:
            raise Refusal("unauthorized")
        if not isinstance(sub, str) or not UUID.fullmatch(sub):
            raise Refusal("unauthorized")
        metadata = claims.get("app_metadata") if isinstance(claims.get("app_metadata"), dict) else {}
        providers = metadata.get("providers") if isinstance(metadata.get("providers"), list) else []
        providers = {p for p in providers if isinstance(p, str)}
        if isinstance(metadata.get("provider"), str):
            providers.add(metadata["provider"])
        return Principal(sub.lower(), providers=sorted(providers))


class Authenticator:
    """The Authorization header -> Principal: Supabase tokens, and `Bearer dev` when dev auth is on."""

    def __init__(self, verifier=None, *, dev_auth=False):
        self.verifier, self.dev_auth = verifier, dev_auth

    def principal(self, authorization, *, required=True):
        """None only when the header is absent and not required (a token-less GET /v2/config)."""
        if not authorization:
            if required:
                raise Refusal("unauthorized")
            return None
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Refusal("unauthorized")
        if self.dev_auth and token == config.DEV_TOKEN:
            return Principal(config.DEV_USER_ID, dev=True)
        if self.verifier is None:
            raise Refusal("unauthorized")
        return self.verifier.verify(token)
