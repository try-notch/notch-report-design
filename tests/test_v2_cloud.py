"""
Notch Cloud over HTTP: sequence order and the newest write winning, cursor paging (by count
and by bytes), every size cap, the keycheck, turning it off, and who may write.
"""

import base64
import os

import pytest

from tests.v2kit import OTHER, USER, Harness, key, ok, refused

KEY_ID = "0123456789abcdef"


@pytest.fixture
def v2(tmp_path):
    with Harness(tmp_path) as harness:
        yield harness


def blob(size=48):
    return base64.b64encode(os.urandom(size)).decode()


def record(record_id=None, *, deleted=False, ciphertext="auto", key_id=KEY_ID):
    return {"id": record_id or key(), "deleted": deleted,
            "ciphertext": blob() if ciphertext == "auto" else ciphertext, "key_id": key_id}


def put(v2, records, user=USER):
    return v2.http.put("/v2/cloud/records", json={"records": records}, headers=v2.headers(user))


def changes(v2, since=None, limit=None, user=USER):
    query = "&".join(f"{k}={v}" for k, v in (("since", since), ("limit", limit)) if v is not None)
    return v2.http.get("/v2/cloud/changes" + (f"?{query}" if query else ""), headers=v2.headers(user))


def test_records_come_back_in_sequence_with_their_ciphertext(v2):
    written = [record() for _ in range(3)]
    assert ok(put(v2, written), "cloud_put") == {"cursor": 3}
    body = ok(changes(v2), "cloud_changes")
    assert body["cursor"] == 3 and body["more"] is False
    assert [(r["id"], r["ciphertext"], r["key_id"], r["seq"], r["deleted"]) for r in body["records"]] == [
        (w["id"], w["ciphertext"], KEY_ID, n, False) for n, w in enumerate(written, 1)]


def test_the_newest_write_of_an_id_wins_and_moves_it_to_the_end(v2):
    first, second = record(), record()
    ok(put(v2, [first, second]), "cloud_put")
    newer = record(first["id"])
    assert ok(put(v2, [newer]), "cloud_put") == {"cursor": 3}
    body = ok(changes(v2), "cloud_changes")
    assert [(r["id"], r["seq"]) for r in body["records"]] == [(second["id"], 2), (first["id"], 3)]
    assert body["records"][-1]["ciphertext"] == newer["ciphertext"]
    assert [r["id"] for r in ok(changes(v2, since=2), "cloud_changes")["records"]] == [first["id"]]


def test_an_id_twice_in_one_batch_keeps_its_last_write(v2):
    same = key()
    ok(put(v2, [record(same), record(), record(same, deleted=True, ciphertext=None)]), "cloud_put")
    body = ok(changes(v2), "cloud_changes")
    assert len(body["records"]) == 2
    last = body["records"][-1]
    assert (last["id"], last["deleted"], last["ciphertext"]) == (same, True, None)


def test_a_deleted_record_may_keep_ciphertext_and_a_live_one_must_have_it(v2):
    ok(put(v2, [record(deleted=True)]), "cloud_put")
    ok(put(v2, [record(deleted=True, ciphertext=None)]), "cloud_put")
    refused(put(v2, [record(ciphertext=None)]), 400, "invalid_request")


def test_the_cursor_pages_through_by_count(v2):
    written = [record() for _ in range(5)]
    ok(put(v2, written), "cloud_put")
    seen, cursor, pages = [], 0, 0
    while True:
        body = ok(changes(v2, since=cursor, limit=2), "cloud_changes")
        seen += [r["id"] for r in body["records"]]
        cursor, pages = body["cursor"], pages + 1
        if not body["more"]:
            break
    assert seen == [w["id"] for w in written] and pages == 3 and cursor == 5
    assert ok(changes(v2, since=5), "cloud_changes") == {"records": [], "cursor": 5, "more": False}


def test_a_page_stops_at_its_byte_budget_but_always_holds_one_record(tmp_path):
    with Harness(tmp_path, overrides={"cloud": {"page_bytes": 100}}) as v2:
        ok(put(v2, [record(ciphertext=blob(60)) for _ in range(3)] + [record(ciphertext=blob(300))]), "cloud_put")
        first = ok(changes(v2), "cloud_changes")
        assert len(first["records"]) == 1 and first["more"] is True
        pages = [first]
        while pages[-1]["more"]:
            pages.append(ok(changes(v2, since=pages[-1]["cursor"]), "cloud_changes"))
        assert [len(p["records"]) for p in pages] == [1, 1, 1, 1]   # 60 + 60 > 100; the 300 alone still comes


def test_each_account_sees_only_its_own_records(v2):
    ok(put(v2, [record()]), "cloud_put")
    assert ok(changes(v2, user=OTHER), "cloud_changes")["records"] == []
    assert ok(put(v2, [record()], user=OTHER), "cloud_put") == {"cursor": 1}  # its own sequence


def test_more_than_the_record_cap_in_one_write_is_payload_too_large(tmp_path):
    with Harness(tmp_path, overrides={"cloud": {"max_records": 3}}) as v2:
        refused(put(v2, [record() for _ in range(4)]), 413, "payload_too_large")
        refused(changes(v2, limit=4), 400, "invalid_request")


def test_a_body_over_its_cap_is_payload_too_large(tmp_path):
    with Harness(tmp_path, overrides={"cloud": {"max_body_bytes": 500}}) as v2:
        refused(put(v2, [record(ciphertext=blob(600))]), 413, "payload_too_large")


def test_a_record_over_its_cap_is_cloud_record_too_large(tmp_path):
    with Harness(tmp_path, overrides={"cloud": {"max_record_bytes": 100}}) as v2:
        ok(put(v2, [record(ciphertext=blob(100))]), "cloud_put")
        refused(put(v2, [record(ciphertext=blob(101))]), 413, "cloud_record_too_large")


def test_the_accounts_storage_cap_refuses_the_whole_batch_and_counts_replacements(tmp_path):
    with Harness(tmp_path, overrides={"cloud": {"account_bytes": 300}}) as v2:
        big = record(ciphertext=blob(200))
        ok(put(v2, [big]), "cloud_put")
        refused(put(v2, [record(ciphertext=blob(60)), record(ciphertext=blob(60))]), 413, "cloud_quota_exceeded")
        assert len(ok(changes(v2), "cloud_changes")["records"]) == 1   # nothing of the refused batch was kept
        ok(put(v2, [record(big["id"], ciphertext=blob(250))]), "cloud_put")   # a replacement frees the old 200
        ok(put(v2, [record(ciphertext=blob(50))]), "cloud_put")


@pytest.mark.parametrize("bad", [
    {"id": "not-a-uuid"}, {"deleted": "no"}, {"ciphertext": "not base64!"}, {"ciphertext": "YQ"},
    {"key_id": "0123"}, {"key_id": "0123456789abcdeg"}, {"extra": True}, {"ciphertext": 5},
])
def test_a_malformed_record_is_invalid_request(v2, bad):
    refused(put(v2, [record() | bad]), 400, "invalid_request")


@pytest.mark.parametrize("body", [[], {"records": {}}, {"records": [], "more": 1}, {"items": []}])
def test_a_malformed_batch_is_invalid_request(v2, body):
    refused(v2.http.put("/v2/cloud/records", json=body, headers=v2.headers()), 400, "invalid_request")


@pytest.mark.parametrize("query", ["since=-1", "since=abc", "since=1.5", "limit=0", "limit=501", "limit=x"])
def test_malformed_paging_is_invalid_request(v2, query):
    refused(v2.http.get(f"/v2/cloud/changes?{query}", headers=v2.headers()), 400, "invalid_request")


def test_an_empty_batch_answers_the_current_cursor(v2):
    ok(put(v2, [record()]), "cloud_put")
    assert ok(put(v2, []), "cloud_put") == {"cursor": 1}


def test_the_keycheck_is_404_until_set_and_upper_case_key_ids_are_folded(v2):
    refused(v2.http.get("/v2/cloud/keycheck", headers=v2.headers()), 404, "not_found")
    verifier = blob(44)
    response = v2.http.put("/v2/cloud/keycheck", json={"key_id": KEY_ID.upper(), "verifier": verifier},
                           headers=v2.headers())
    assert response.status_code == 204 and response.content == b""
    assert ok(v2.http.get("/v2/cloud/keycheck", headers=v2.headers()), "keycheck") == {"key_id": KEY_ID,
                                                                                        "verifier": verifier}


@pytest.mark.parametrize("body", [{"key_id": KEY_ID}, {"key_id": "xyz", "verifier": "AAAA"},
                                  {"key_id": KEY_ID, "verifier": "@@@"}, {"key_id": KEY_ID, "verifier": ""}])
def test_a_malformed_keycheck_is_invalid_request(v2, body):
    refused(v2.http.put("/v2/cloud/keycheck", json=body, headers=v2.headers()), 400, "invalid_request")


def test_turning_notch_cloud_off_wipes_records_and_keycheck_and_keeps_the_sequence(v2):
    ok(put(v2, [record(), record()]), "cloud_put")
    v2.http.put("/v2/cloud/keycheck", json={"key_id": KEY_ID, "verifier": blob()}, headers=v2.headers())
    assert v2.http.delete("/v2/cloud", headers=v2.headers()).status_code == 204
    assert ok(changes(v2), "cloud_changes") == {"records": [], "cursor": 0, "more": False}
    refused(v2.http.get("/v2/cloud/keycheck", headers=v2.headers()), 404, "not_found")
    assert v2.rows("SELECT count(*) AS n FROM cloud_records") == [{"n": 0}]
    assert ok(put(v2, [record()]), "cloud_put") == {"cursor": 3}   # no cursor ever goes backwards


def test_writes_need_the_feature_and_an_unblocked_account_but_reads_and_off_always_work(tmp_path):
    with Harness(tmp_path, overrides={"features": {"notch_cloud": False}}) as v2:
        refused(put(v2, [record()]), 503, "feature_disabled")
        refused(v2.http.put("/v2/cloud/keycheck", json={"key_id": KEY_ID, "verifier": blob()},
                            headers=v2.headers()), 503, "feature_disabled")
        ok(changes(v2), "cloud_changes")
        assert v2.http.delete("/v2/cloud", headers=v2.headers()).status_code == 204
    with Harness(tmp_path / "blocked") as v2:
        v2.meter.set_blocked(USER, "abuse")
        refused(put(v2, [record()]), 403, "account_blocked")
        ok(changes(v2), "cloud_changes")


def test_notch_cloud_needs_a_token_and_a_live_account(v2):
    refused(v2.http.get("/v2/cloud/changes", headers=v2.headers(auth=False)), 401, "unauthorized")
    v2.meter.delete_account(USER)
    refused(changes(v2), 403, "account_gone")


def test_the_server_keeps_the_ciphertext_as_bytes_and_nothing_readable(v2):
    raw = os.urandom(40)
    ok(put(v2, [record(ciphertext=base64.b64encode(raw).decode())]), "cloud_put")
    (row,) = v2.rows("SELECT ciphertext, key_id FROM cloud_records")
    assert row == {"ciphertext": raw, "key_id": KEY_ID}
