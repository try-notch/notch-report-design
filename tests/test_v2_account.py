"""
DELETE /v2/account over HTTP (Apple revoked when a code is sent, the Supabase user deleted,
every metering row and Notch Cloud record gone, the id kept as deleted, a retry after a
lost 204 still 204), the real Apple and Supabase admin clients over httpx.MockTransport,
and the settings Services.from_env refuses to start without in prod.
"""

import base64
import os
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from notch_api.fakes import FakeApple, FakeSupabaseAdmin, fake_recording
from notch_api.identity import AppleCodeRejected, AppleRevoker, SupabaseAdmin
from notch_api.services import DEV_BODY_KEY, Services
from notch_api.wire_v2 import Refusal
from tests.v2kit import OTHER, USER, Harness, ok, refused

APPLE_LINKED = {"app_metadata": {"provider": "apple", "providers": ["apple"]}}


def delete(v2, body=None, user=USER, **claims):
    headers = {"X-Client": "ios/1.0.0+42", "Authorization": f"Bearer {v2.token(user, **claims)}"}
    return v2.http.request("DELETE", "/v2/account", json=body, headers=headers)


def populate(v2, user=USER):
    ok(v2.transcribe(fake_recording("Shipped the thing."), user=user), "transcribe")
    ok(v2.http.get("/v2/config", headers=v2.headers(user)), "config")
    v2.http.put("/v2/cloud/records", headers=v2.headers(user), json={"records": [
        {"id": "88888888-8888-4888-8888-888888888888", "deleted": False,
         "ciphertext": base64.b64encode(b"sealed").decode(), "key_id": "0123456789abcdef"}]})
    v2.http.put("/v2/cloud/keycheck", headers=v2.headers(user),
                json={"key_id": "0123456789abcdef", "verifier": base64.b64encode(b"v").decode()})


def owned(v2, user=USER):
    tables = ("accounts", "usage_events", "active_days", "cloud_records", "cloud_keycheck")
    return {t: v2.rows(f"SELECT count(*) AS n FROM {t} WHERE user_id = ?", user)[0]["n"] for t in tables}


@pytest.fixture
def v2(tmp_path):
    with Harness(tmp_path) as harness:
        yield harness


def test_deleting_an_account_removes_everything_it_owns_and_keeps_the_day_spend(v2):
    populate(v2)
    populate(v2, OTHER)
    spend = v2.rows("SELECT sum(cost_usd) AS c FROM daily_spend")
    response = delete(v2)
    assert response.status_code == 204 and response.content == b""
    assert v2.admin.deleted == [USER]
    assert set(owned(v2).values()) == {0}
    assert v2.rows("SELECT user_id FROM deleted_accounts") == [{"user_id": USER}]
    assert v2.rows("SELECT sum(cost_usd) AS c FROM daily_spend") == spend
    assert owned(v2, OTHER)["cloud_records"] == 1 and owned(v2, OTHER)["usage_events"] == 1


def test_the_deleted_accounts_still_valid_token_is_gone_everywhere(v2):
    populate(v2)
    delete(v2)
    refused(v2.http.get("/v2/config", headers=v2.headers()), 403, "account_gone")
    refused(v2.transcribe(fake_recording("Hello there.")), 403, "account_gone")
    refused(v2.http.get("/v2/cloud/changes", headers=v2.headers()), 403, "account_gone")
    assert owned(v2)["accounts"] == 0


def test_a_retry_after_a_lost_204_succeeds(v2):
    populate(v2)
    assert delete(v2).status_code == 204
    assert delete(v2).status_code == 204
    assert v2.admin.deleted == [USER, USER]   # Supabase is asked again; its 404 is fine


def test_an_apple_linked_account_must_send_its_code(v2):
    populate(v2)
    refused(delete(v2, **APPLE_LINKED), 400, "invalid_request")
    assert owned(v2)["accounts"] == 1 and v2.admin.deleted == []
    assert delete(v2, {"apple_authorization_code": "c0de.from-apple_1"}, **APPLE_LINKED).status_code == 204
    assert v2.apple.codes == ["c0de.from-apple_1"] and v2.admin.deleted == [USER]


def test_a_code_apple_refuses_deletes_nothing_until_the_account_is_already_gone(tmp_path):
    with Harness(tmp_path, apple=FakeApple(reject=True)) as v2:
        populate(v2)
        refused(delete(v2, {"apple_authorization_code": "stale"}, **APPLE_LINKED), 400, "invalid_request")
        assert owned(v2)["accounts"] == 1 and v2.admin.deleted == []
        v2.meter.delete_account(USER)   # deleted by an earlier call whose 204 was lost
        assert delete(v2, {"apple_authorization_code": "stale"}, **APPLE_LINKED).status_code == 204


def test_apple_or_supabase_being_down_is_unavailable_and_deletes_nothing(tmp_path):
    cases = (({"apple": FakeApple(down=True)}, {"apple_authorization_code": "code"}),
             ({"admin": FakeSupabaseAdmin(down=True)}, None))
    for n, (kwargs, body) in enumerate(cases):
        with Harness(tmp_path / f"case{n}", **kwargs) as v2:
            populate(v2)
            response = delete(v2, body, **(APPLE_LINKED if body else {}))
            refused(response, 503, "unavailable")
            assert owned(v2)["accounts"] == 1 and owned(v2)["cloud_records"] == 1


@pytest.mark.parametrize("body", [{"apple_authorization_code": ""}, {"apple_authorization_code": 5},
                                  {"apple_authorization_code": "has spaces"}, {"code": "x"}, []])
def test_a_malformed_body_is_invalid_request(v2, body):
    refused(delete(v2, body), 400, "invalid_request")


def test_the_dev_user_is_not_sent_to_supabase(tmp_path):
    with Harness(tmp_path, dev_auth=True) as v2:
        response = v2.http.delete("/v2/account", headers={"X-Client": "ios/1.0.0+42", "Authorization": "Bearer dev"})
        assert response.status_code == 204 and v2.admin.deleted == []


def test_deleting_needs_a_valid_token(v2):
    refused(v2.http.delete("/v2/account", headers={"X-Client": "ios/1.0.0+42"}), 401, "unauthorized")


# ---------------------------------------------------------------------------
# The real Apple and Supabase admin clients
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def apple_key():
    """A stand-in for the team's .p8 key, minted for this run."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return private, pem


def _apple(apple_key, handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    revoker = AppleRevoker(team_id="TEAM123456", key_id="KEY1234567", client_id="xyz.trynotch.app",
                           private_key=apple_key[1], http=httpx.Client(transport=httpx.MockTransport(record)))
    return revoker, requests


def _form(request):
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def test_apple_exchanges_the_code_then_revokes_the_refresh_token(apple_key):
    def handler(request):
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "id_token": "it"})
        return httpx.Response(200)

    revoker, requests = _apple(apple_key, handler)
    revoker.revoke("the-code")
    exchange, revoke = requests
    assert (str(exchange.url), str(revoke.url)) == ("https://appleid.apple.com/auth/token",
                                                    "https://appleid.apple.com/auth/revoke")
    form = _form(exchange)
    assert (form["code"], form["grant_type"], form["client_id"]) == ("the-code", "authorization_code",
                                                                      "xyz.trynotch.app")
    claims = jwt.decode(form["client_secret"], apple_key[0].public_key(), algorithms=["ES256"],
                        audience="https://appleid.apple.com")
    assert (claims["iss"], claims["sub"]) == ("TEAM123456", "xyz.trynotch.app")
    assert jwt.get_unverified_header(form["client_secret"])["kid"] == "KEY1234567"
    assert {k: _form(revoke)[k] for k in ("token", "token_type_hint")} == {"token": "rt",
                                                                            "token_type_hint": "refresh_token"}


def test_apple_without_a_refresh_token_revokes_the_access_token(apple_key):
    revoker, requests = _apple(apple_key, lambda request: httpx.Response(200, json={"access_token": "at"})
                               if request.url.path == "/auth/token" else httpx.Response(200))
    revoker.revoke("code")
    assert _form(requests[1])["token_type_hint"] == "access_token"


@pytest.mark.parametrize("answer, raised", [(httpx.Response(400, json={"error": "invalid_grant"}), AppleCodeRejected),
                                            (httpx.Response(503), Refusal)])
def test_apple_refusing_or_failing(apple_key, answer, raised):
    revoker, _ = _apple(apple_key, lambda request: answer)
    with pytest.raises(raised):
        revoker.revoke("code")


def test_apple_is_configured_only_with_all_four_settings(apple_key, tmp_path):
    key_path = tmp_path / "apple.p8"
    key_path.write_text(apple_key[1])
    environ = {"APPLE_TEAM_ID": "T", "APPLE_KEY_ID": "K", "APPLE_CLIENT_ID": "c", "APPLE_PRIVATE_KEY_PATH": str(key_path)}
    assert isinstance(AppleRevoker.from_env(environ), AppleRevoker)
    assert AppleRevoker.from_env({k: v for k, v in environ.items() if k != "APPLE_KEY_ID"}) is None


# Stand-ins built at run time, shaped only as far as SupabaseAdmin looks: a prefix, or three dotted parts.
NEW_STYLE_KEY = "sb_" + "secret_" + "test" * 4
LEGACY_KEY = ".".join(["eyJ" + "a" * 16, "eyJ" + "b" * 16, "c" * 16])


@pytest.mark.parametrize("secret, bearer", [(NEW_STYLE_KEY, False), (LEGACY_KEY, True)])
def test_supabase_admin_deletes_the_user_with_the_right_headers(secret, bearer):
    seen = []
    admin = SupabaseAdmin("https://proj.supabase.test/", secret,
                          http=httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or
                                                                           httpx.Response(200, json={}))))
    admin.delete_user(USER)
    (request,) = seen
    assert request.method == "DELETE" and str(request.url) == f"https://proj.supabase.test/auth/v1/admin/users/{USER}"
    assert request.headers["apikey"] == secret
    assert ("authorization" in request.headers) is bearer


@pytest.mark.parametrize("status, fine", [(204, True), (404, True), (401, False), (500, False)])
def test_supabase_admin_statuses(status, fine):
    admin = SupabaseAdmin("https://proj.supabase.test", NEW_STYLE_KEY,
                          http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))))
    if fine:
        admin.delete_user(USER)
    else:
        with pytest.raises(Refusal):
            admin.delete_user(USER)


# ---------------------------------------------------------------------------
# What prod refuses to start without
# ---------------------------------------------------------------------------

PROD = {"NOTCH_ENV": "prod", "SUPABASE_URL": "https://proj.supabase.test", "SUPABASE_SECRET_KEY": "placeholder",
        "NOTCH_BODY_HMAC_KEY": "k" * 64}


@pytest.fixture
def prod(tmp_path):
    return PROD | {"NOTCH_METER_DB": str(tmp_path / "meter.db"), "NOTCH_TMP": str(tmp_path / "tmpfs")}


def test_prod_starts_with_its_settings(prod):
    services = Services.from_env(prod)
    assert services.prod and not services.auth.dev_auth and services.body_key == ("k" * 64).encode()
    assert services.supabase_admin is not None and services.apple is None
    assert os.path.isdir(prod["NOTCH_TMP"])


@pytest.mark.parametrize("change, why", [
    ({"NOTCH_DEV_AUTH": "1"}, "NOTCH_DEV_AUTH"), ({"SUPABASE_URL": ""}, "SUPABASE_URL"),
    ({"SUPABASE_SECRET_KEY": ""}, "SUPABASE_SECRET_KEY"), ({"NOTCH_TMP": ""}, "NOTCH_TMP"),
    ({"NOTCH_BODY_HMAC_KEY": "short"}, "NOTCH_BODY_HMAC_KEY"),
])
def test_prod_refuses_to_start_without_a_setting_or_with_dev_auth(prod, change, why):
    with pytest.raises(RuntimeError, match=why):
        Services.from_env(prod | change)


def test_outside_prod_dev_auth_is_allowed_and_the_body_key_is_the_fixed_dev_one(tmp_path):
    services = Services.from_env({"NOTCH_DEV_AUTH": "1", "NOTCH_METER_DB": str(tmp_path / "m.db"),
                                  "NOTCH_TMP": str(tmp_path / "t")})
    assert services.auth.dev_auth and services.body_key == DEV_BODY_KEY and services.auth.verifier is None
