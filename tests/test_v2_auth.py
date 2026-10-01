"""
Supabase JWT verification (auth.py) with keys minted for the run and a stubbed JWKS
endpoint: valid, expired, wrong iss / aud / role, missing claims, anonymous, forged, an
unknown kid (fetched again, at most once per cooldown), the 10-minute key-set cache, a
key set that cannot be fetched, and dev auth. The route-level half (a deleted account's
still-valid token is 403 account_gone) is in test_v2_routes.py.
"""

import base64
import time

import jwt
import pytest

from notch_api.auth import Authenticator, Principal, Verifier
from notch_api.config import DEV_USER_ID
from notch_api.wire_v2 import Refusal
from tests.v2kit import ISSUER, SUPABASE_URL, JWKSStub, SigningKey, token

USER = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(scope="module")
def es256():
    return SigningKey("key-es256")


@pytest.fixture(scope="module")
def rs256():
    return SigningKey("key-rs256", "RS256")


@pytest.fixture
def jwks(es256, rs256):
    return JWKSStub(es256, rs256)


@pytest.fixture
def verifier(jwks):
    return Verifier(SUPABASE_URL, http=jwks.http, cooldown=0)


def refused(code, call):
    with pytest.raises(Refusal) as refusal:
        call()
    assert refusal.value.code == code
    return refusal.value


def test_a_valid_es256_token_is_its_user(verifier, es256):
    principal = verifier.verify(token(es256))
    assert (principal.user_id, principal.providers, principal.dev) == (USER, ("email",), False)


def test_rs256_is_also_accepted(verifier, rs256):
    assert verifier.verify(token(rs256)).user_id == USER


def test_the_user_id_is_lowercased_and_apple_is_noticed(verifier, es256):
    principal = verifier.verify(token(es256, sub=USER.upper(), app_metadata={"provider": "apple",
                                                                             "providers": ["apple", "email"]}))
    assert principal.user_id == USER and principal.apple_linked and principal.providers == ("apple", "email")


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 10},                        # expired
    {"iss": "https://someone-else.supabase.test/auth/v1"},  # another project
    {"iss": SUPABASE_URL},                                  # not the auth issuer
    {"aud": "anon"},
    {"role": "anon"},
    {"role": "service_role"},
    {"is_anonymous": True},                                 # an anonymous sign-in is still role authenticated
    {"exp": None}, {"iat": None}, {"sub": None},
    {"sub": "not-a-uuid"},
])
def test_a_token_failing_any_check_is_unauthorized(verifier, es256, claims):
    refusal = refused("unauthorized", lambda: verifier.verify(token(es256, **claims)))
    assert refusal.status == 401 and refusal.message == "A valid access token is required."


def test_a_token_signed_by_a_key_the_project_does_not_publish_is_refused(verifier):
    impostor = SigningKey("key-es256")  # same kid, different key
    refused("unauthorized", lambda: verifier.verify(token(impostor)))


def test_a_tampered_token_is_refused(verifier, es256):
    head, body, signature = token(es256).split(".")
    forged = jwt.encode({"sub": "66666666-6666-4666-8666-666666666666"}, b"k" * 32, algorithm="HS256").split(".")[1]
    refused("unauthorized", lambda: verifier.verify(f"{head}.{forged}.{signature}"))


@pytest.mark.parametrize("alg", ["HS256", "none"])
def test_algorithms_other_than_es256_and_rs256_are_refused(verifier, es256, alg):
    body = {"iss": ISSUER, "aud": "authenticated", "role": "authenticated", "sub": USER,
            "iat": int(time.time()), "exp": int(time.time()) + 60}
    forged = jwt.encode(body, b"k" * 32 if alg == "HS256" else None, algorithm=alg, headers={"kid": es256.kid})
    refused("unauthorized", lambda: verifier.verify(forged))


def test_a_token_whose_alg_does_not_match_its_key_is_refused(verifier, rs256, es256):
    mismatched = token(rs256, headers={"kid": es256.kid})  # RS256 signature, naming the ES256 key
    refused("unauthorized", lambda: verifier.verify(mismatched))


def test_garbage_is_unauthorized(verifier):
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b"=").decode()
    for junk in ("", "abc", "a.b.c", f"{header}.e30."):
        refused("unauthorized", lambda: verifier.verify(junk))


def test_the_key_set_is_fetched_once_and_cached(verifier, jwks, es256):
    for _ in range(3):
        verifier.verify(token(es256))
    assert jwks.fetches == 1


def test_the_key_set_is_cached_for_at_most_ten_minutes(verifier, jwks, es256, monkeypatch):
    verifier.verify(token(es256))
    assert verifier.jwks.jwk_set_cache.lifespan == 600
    later = time.monotonic() + 601
    monkeypatch.setattr(time, "monotonic", lambda: later)
    verifier.verify(token(es256))
    assert jwks.fetches == 2


def test_an_unknown_kid_fetches_the_key_set_again(verifier, jwks, es256):
    verifier.verify(token(es256))
    rotated = SigningKey("key-rotated")
    jwks.keys.append(rotated)                 # Supabase rotated its signing key
    assert verifier.verify(token(rotated)).user_id == USER
    assert jwks.fetches == 2
    refused("unauthorized", lambda: verifier.verify(token(SigningKey("key-nobody-has"))))
    assert jwks.fetches == 3


def test_unknown_kids_refetch_at_most_once_per_cooldown(jwks, es256):
    verifier = Verifier(SUPABASE_URL, http=jwks.http)  # the default 30 s cooldown
    verifier.verify(token(es256))
    for n in range(5):
        refused("unauthorized", lambda: verifier.verify(token(SigningKey(f"made-up-{n}"))))
    assert jwks.fetches == 1


def test_a_key_set_that_cannot_be_fetched_is_unavailable_not_unauthorized(verifier, jwks, es256):
    jwks.down = True
    refusal = refused("unavailable", lambda: verifier.verify(token(es256)))
    assert (refusal.status, refusal.retryable) == (503, True)


def test_the_authenticator_reads_the_bearer_header(verifier, es256):
    auth = Authenticator(verifier)
    assert auth.principal(f"Bearer {token(es256)}").user_id == USER
    assert auth.principal(None, required=False) is None
    for header in (None, "", "Basic abc", "Bearer", "Bearer  ", "Bearer dev"):
        refused("unauthorized", lambda: auth.principal(header))


def test_dev_auth_is_the_dev_user_only_when_turned_on(verifier, es256):
    principal = Authenticator(verifier, dev_auth=True).principal("Bearer dev")
    assert isinstance(principal, Principal) and (principal.user_id, principal.dev) == (DEV_USER_ID, True)
    assert Authenticator(verifier, dev_auth=True).principal(f"Bearer {token(es256)}").user_id == USER
    refused("unauthorized", lambda: Authenticator(None).principal("Bearer dev"))
