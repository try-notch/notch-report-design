"""
meter.py's check-and-start and settle, straight against the SQLite file: every quota
boundary (N allowed, N+1 refused; a retry at N still passes; two calls racing at N-1,
exactly one wins), key reuse, the attempts window, the in-flight cap, stale rows, the
account's cost cap, the global breaker and what survives an account's deletion.
"""

import threading

import pytest

from notch_api.meter import Meter
from notch_api.wire_v2 import Refusal
from tests.v2kit import MIDNIGHT, NOON, WallClock, config, hmac_of, key

USER, OTHER = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
BODY = hmac_of("the body")
DEADLINE = 60


@pytest.fixture
def clock():
    return WallClock()


@pytest.fixture
def meter(tmp_path, clock):
    return Meter(str(tmp_path / "meter.db"), clock=clock)


def start(meter, cfg, *, kind="transcribe", request_key=None, body=BODY, user=USER):
    return meter.start(user_id=user, kind=kind, key=request_key or key(), body_hmac=body,
                       deadline_seconds=DEADLINE, config=cfg, sizes={"request_bytes": 10},
                       versions={"config_version": cfg.version, "app_version": "1.0.0", "platform": "ios"})


def refused(code, call):
    with pytest.raises(Refusal) as refusal:
        call()
    assert refusal.value.code == code, refusal.value.code
    return refusal.value


def ok(meter, started, cost=0.001, reached=True):
    meter.settle(started, ok=True, totals={"cost": cost, "models": ["m"], "providers": ["p"]}, reached_model=reached)


def rows(meter, sql, *args):
    from notch_api.meter import connect
    conn = connect(meter.path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


SMALL = config(limits={"notches_per_day": 2, "reports_per_day": 1, "rewrites_per_day": 3})  # backstops 4, 2, 6


def test_the_default_backstops_are_twice_the_phones_limits():
    assert {kind: config().backstop(kind) for kind in ("transcribe", "analyze", "reports", "takeaways")} == {
        "transcribe": 20, "analyze": 20, "reports": 10, "takeaways": 40}


def test_a_call_starts_in_flight_and_settles_with_its_cost_and_the_global_spend(meter):
    started = start(meter, SMALL)
    assert started.attempt == 1
    (row,) = rows(meter, "SELECT * FROM usage_events")
    assert (row["status"], row["attempt"], row["day"], row["deadline_at"]) == ("in_flight", 1, "2026-09-26",
                                                                              NOON + DEADLINE)
    meter.settle(started, ok=True, totals={"cost": 0.25, "prompt_tokens": 7, "completion_tokens": 3,
                                           "models": ["a/b"], "providers": ["P"]}, reached_model=True,
                 audio_seconds=12.5, chunks=1)
    (row,) = rows(meter, "SELECT * FROM usage_events")
    assert (row["status"], row["cost_usd"], row["reached_model"], row["audio_seconds"], row["providers"]) == (
        "ok", 0.25, 1, 12.5, '["P"]')
    assert rows(meter, "SELECT * FROM daily_spend") == [{"day": "2026-09-26", "kind": "transcribe", "calls": 1,
                                                         "cost_usd": 0.25}]


@pytest.mark.parametrize("kind, backstop", [("transcribe", 4), ("analyze", 4), ("reports", 2), ("takeaways", 6)])
def test_n_distinct_keys_are_allowed_and_the_next_one_is_refused_until_midnight(meter, kind, backstop):
    keys = [key() for _ in range(backstop)]
    for request_key in keys:
        ok(meter, start(meter, SMALL, kind=kind, request_key=request_key))
    refusal = refused("quota_exceeded", lambda: start(meter, SMALL, kind=kind))
    assert refusal.status == 429 and refusal.retryable
    assert refusal.resets_at == "2026-09-27T00:00:00Z" and refusal.retry_after == MIDNIGHT - NOON
    # A retry of a key already counted today is charged once, so it still passes at N.
    again = start(meter, SMALL, kind=kind, request_key=keys[0])
    assert again.attempt == 2
    # The refusal left a rejected row that used no attempt.
    (rejected,) = rows(meter, "SELECT attempt, error_code FROM usage_events WHERE status = 'rejected'")
    assert rejected == {"attempt": None, "error_code": "quota_exceeded"}


def test_each_kind_counts_on_its_own(meter):
    for _ in range(4):
        ok(meter, start(meter, SMALL, kind="transcribe"))
    ok(meter, start(meter, SMALL, kind="takeaways"))


def test_two_calls_racing_at_n_minus_one_let_exactly_one_through(meter):
    for _ in range(3):
        ok(meter, start(meter, SMALL))
    gate, results = threading.Barrier(2), []

    def race():
        gate.wait()
        try:
            results.append(start(meter, SMALL).row_id)
        except Refusal as refusal:
            results.append(refusal.code)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, results))[-1] == "quota_exceeded"
    assert sum(isinstance(r, int) for r in results) == 1


def test_a_failed_call_is_not_counted_but_its_cost_is(meter):
    for _ in range(4):
        meter.settle(start(meter, SMALL), ok=False, error_code="model_unavailable", totals={"cost": 0.01},
                     reached_model=True)
    ok(meter, start(meter, SMALL), cost=0.0)
    (spent,) = rows(meter, "SELECT sum(cost_usd) AS c FROM usage_events")
    assert spent["c"] == pytest.approx(0.04)


def test_the_quota_is_per_utc_day(meter, clock):
    for _ in range(4):
        ok(meter, start(meter, SMALL))
    refused("quota_exceeded", lambda: start(meter, SMALL))
    clock.now = MIDNIGHT
    assert start(meter, SMALL).day == "2026-09-27"


def test_a_key_that_comes_back_with_another_body_is_refused_on_any_day(meter, clock):
    request_key = key()
    ok(meter, start(meter, SMALL, request_key=request_key))
    clock.advance(3 * 86400)
    refusal = refused("idempotency_key_reused",
                      lambda: start(meter, SMALL, request_key=request_key, body=hmac_of("another body")))
    assert (refusal.status, refusal.retryable) == (409, False)
    # The same key on another kind is another request: the notch id keys transcribe and analyze.
    start(meter, SMALL, kind="analyze", request_key=request_key, body=hmac_of("another body"))


def test_a_key_still_running_is_request_in_flight_until_its_deadline(meter, clock):
    request_key = key()
    start(meter, SMALL, request_key=request_key)
    clock.advance(20)
    refusal = refused("request_in_flight", lambda: start(meter, SMALL, request_key=request_key))
    assert refusal.retry_after == DEADLINE - 20 and refusal.status == 409
    clock.advance(DEADLINE)  # the first call never settled: past its deadline it has ended
    assert start(meter, SMALL, request_key=request_key).attempt == 2


def test_the_attempts_window_counts_attempts_that_reached_a_model(meter, clock):
    request_key = key()
    for n in range(5):
        meter.settle(start(meter, SMALL, request_key=request_key), ok=False, error_code="model_unavailable",
                     reached_model=True)
        clock.advance(60)
    refusal = refused("attempts_exhausted", lambda: start(meter, SMALL, request_key=request_key))
    assert (refusal.status, refusal.retryable) == (429, False)
    assert refusal.retry_after == pytest.approx(3600 - 5 * 60)  # when the first one leaves the hour
    clock.advance(3600 - 5 * 60)
    assert start(meter, SMALL, request_key=request_key).attempt == 6


def test_attempts_that_never_reached_a_model_do_not_count(meter):
    request_key = key()
    for _ in range(7):
        meter.settle(start(meter, SMALL, request_key=request_key), ok=False, error_code="deadline_exceeded",
                     reached_model=False)
    start(meter, SMALL, request_key=request_key)


def test_at_most_three_calls_run_at_once(meter, clock):
    for kind in ("transcribe", "analyze", "reports"):
        start(meter, SMALL, kind=kind)
    refusal = refused("rate_limited", lambda: start(meter, SMALL, kind="takeaways"))
    assert refusal.retryable and refusal.retry_after
    start(meter, SMALL, kind="takeaways", user=OTHER)  # per account
    clock.advance(DEADLINE + 1)  # stale rows are not running
    start(meter, SMALL, kind="takeaways")


def test_the_accounts_daily_cost_cap_counts_every_attempt(meter, clock):
    ok(meter, start(meter, SMALL), cost=0.6)
    meter.settle(start(meter, SMALL), ok=False, error_code="model_refused", totals={"cost": 0.45},
                 reached_model=True)
    refusal = refused("quota_exceeded", lambda: start(meter, SMALL))
    assert refusal.resets_at == "2026-09-27T00:00:00Z"
    start(meter, SMALL, user=OTHER)
    clock.now = MIDNIGHT + 1
    start(meter, SMALL)


def test_the_global_breaker_pauses_everyone_and_survives_account_deletion(meter, clock):
    spenders = [f"3333333{n}-3333-4333-8333-333333333333" for n in range(4)]
    for spender in spenders:  # each under its own $1 cap when it starts, $5 once settled
        ok(meter, start(meter, config(), user=spender), cost=5.0)
    for spender in spenders:
        meter.delete_account(spender)
    refusal = refused("processing_paused", lambda: start(meter, config(), user=OTHER))
    assert (refusal.status, refusal.retryable, refusal.retry_after) == (503, True, MIDNIGHT - NOON)
    assert rows(meter, "SELECT sum(cost_usd) AS c FROM daily_spend") == [{"c": 20.0}]
    clock.now = MIDNIGHT
    start(meter, config(), user=OTHER)


def test_a_deleted_account_is_gone_and_gets_no_rows_back(meter):
    ok(meter, start(meter, SMALL))
    meter.delete_account(USER)
    assert rows(meter, "SELECT * FROM usage_events") == []
    refused("account_gone", lambda: start(meter, SMALL))
    refused("account_gone", lambda: meter.touch_account(USER))
    assert rows(meter, "SELECT * FROM usage_events") == rows(meter, "SELECT * FROM accounts") == []
    assert meter.is_deleted(USER)


def test_a_blocked_account_is_refused_and_unblocking_lets_it_back(meter):
    meter.set_blocked(USER, "abuse")
    refusal = refused("account_blocked", lambda: start(meter, SMALL))
    assert refusal.status == 403
    meter.set_blocked(USER, None)
    start(meter, SMALL)


def test_a_call_settling_after_its_account_was_deleted_still_adds_to_the_global_spend(meter):
    started = start(meter, SMALL)
    meter.delete_account(USER)
    meter.settle(started, ok=True, totals={"cost": 0.3}, reached_model=True)
    assert rows(meter, "SELECT cost_usd FROM daily_spend") == [{"cost_usd": 0.3}]


def test_a_late_reply_adds_its_cost_to_the_row_and_the_day(meter):
    started = start(meter, SMALL)
    meter.settle(started, ok=False, error_code="deadline_exceeded", totals={"cost": 0.1}, reached_model=True)
    meter.add_late_cost(started, 0.05)
    assert rows(meter, "SELECT cost_usd FROM usage_events") == [{"cost_usd": pytest.approx(0.15)}]
    assert rows(meter, "SELECT calls, cost_usd FROM daily_spend") == [{"calls": 1, "cost_usd": pytest.approx(0.15)}]


def test_rows_a_dead_process_left_in_flight_are_expired_at_startup(meter, clock):
    start(meter, SMALL)
    clock.advance(DEADLINE + 1)
    assert meter.expire_stale() == 1
    assert rows(meter, "SELECT status, error_code FROM usage_events") == [{"status": "failed",
                                                                          "error_code": "abandoned"}]


def test_usage_today_counts_what_the_backstops_count(meter):
    ok(meter, start(meter, SMALL))
    started = start(meter, SMALL)  # in flight: counted
    meter.settle(start(meter, SMALL, kind="takeaways"), ok=False, error_code="model_refused")  # failed: not counted
    ok(meter, start(meter, SMALL, kind="reports"))
    assert meter.usage_today(USER) == {"day_utc": "2026-09-26", "notches": 2, "reports": 1, "rewrites": 0}
    assert started.attempt == 1


def test_active_days_are_one_row_per_account_per_utc_day(meter, clock):
    meter.mark_active(USER, "1.0.0", "ios")
    meter.mark_active(USER, "1.0.1", "ios")
    clock.now = MIDNIGHT
    meter.mark_active(USER, "1.0.1", "ios")
    assert rows(meter, "SELECT day, app_version FROM active_days ORDER BY day") == [
        {"day": "2026-09-26", "app_version": "1.0.1"}, {"day": "2026-09-27", "app_version": "1.0.1"}]


def test_the_meter_has_no_content_columns(meter):
    """Every column is metadata, except the two Notch Cloud blobs the server cannot read."""
    allowed = {
        "accounts": {"user_id", "plan", "flags", "created_at", "blocked_at", "blocked_code", "cloud_seq"},
        "deleted_accounts": {"user_id", "deleted_at"},
        "usage_events": {"id", "user_id", "kind", "request_key", "attempt", "status", "error_code", "body_hmac", "day",
                         "started_at", "deadline_at", "finished_at", "reached_model", "request_bytes",
                         "audio_seconds", "chunks", "input_chars", "entry_count", "prompt_tokens",
                         "completion_tokens", "cost_usd", "models", "providers", "zdr", "config_version",
                         "prompt_version", "app_version", "platform"},
        "daily_spend": {"day", "kind", "calls", "cost_usd"},
        "refusals": {"hour", "route", "status", "code", "calls", "last_at"},
        "active_days": {"user_id", "day", "app_version", "platform"},
        "config": {"version", "body", "note", "created_by", "created_at"},
        "cloud_records": {"user_id", "record_id", "seq", "deleted", "ciphertext", "key_id", "updated_at"},
        "cloud_keycheck": {"user_id", "key_id", "verifier", "updated_at"},
    }
    tables = {r["name"] for r in rows(meter, "SELECT name FROM sqlite_master WHERE type = 'table' "
                                             "AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(allowed)
    for table, columns in allowed.items():
        assert {r["name"] for r in rows(meter, f"PRAGMA table_info({table})")} == columns, table
