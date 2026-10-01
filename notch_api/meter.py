"""
meter.py — the only database /v2 keeps: usage metering, accounts, remote config rows and
Notch Cloud ciphertext, in one SQLite file (NOTCH_METER_DB) on the VPS.

NO CONTENT COLUMNS. Every column is an id, a code, a count, a size, a cost, a time, a
model or provider name or a version, except `cloud_records.ciphertext` and
`cloud_keycheck.verifier`, which the server cannot read. tests/test_no_content_at_rest.py
reads this file back after pushing a canary through every route.

EVERY PROCESSING CALL IS CHECK-AND-START, CALL, SETTLE.
  start()   one BEGIN IMMEDIATE transaction (SQLite's write lock, so concurrent calls
            serialize and a check can never race the insert it guards) that refuses the
            call (in the order below) or inserts its `in_flight` row. A refusal writes a
            `rejected` row with no attempt number, so it never uses an attempt up.
  settle()  one transaction: the row's outcome, tokens, cost, models and providers, and
            the user-less `daily_spend` that drives the global breaker. The route
            settles BEFORE it writes its response.
A call the process never settled counts as ended once its deadline has passed.

COUNTING. Quotas count distinct Idempotency-Keys per UTC day among `ok` rows and live
`in_flight` ones, so a retried call is charged once and a failed one not at all. Cost
counts every attempt, whatever its outcome, because every attempt was paid for.

ANSWERS THE METER NEVER SAW. A call refused before check-and-start (no token, an app
too old, a feature switched off, audio that will not decode) has no `usage_events` row.
`refusals` counts those by hour, route template, status and code, with no user, so the
dashboard can show them; refusals.py batches the writes.

Connections are opened per operation with isolation_level=None, so a transaction is
exactly the BEGIN IMMEDIATE ... COMMIT written here (Python's default would open a
deferred one of its own before the first write). Instants are Unix seconds; days are
UTC dates, 'YYYY-MM-DD'.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .wire_v2 import Refusal

KINDS = ("transcribe", "analyze", "takeaways", "reports")
RATE_LIMIT_RETRY_SECONDS = 5
REFUSALS_KEPT_SECONDS = 90 * 86400   # hourly rows older than this go as new ones are written

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    user_id      TEXT PRIMARY KEY NOT NULL,          -- the Supabase user id (the JWT's sub)
    plan         TEXT NOT NULL DEFAULT 'free',
    flags        TEXT NOT NULL DEFAULT '{}',          -- per-account feature overrides, JSON
    created_at   REAL NOT NULL,
    blocked_at   REAL,
    blocked_code TEXT,                                -- a code, never free text
    cloud_seq    INTEGER NOT NULL DEFAULT 0           -- the last Notch Cloud sequence number handed out
) STRICT;

CREATE TABLE IF NOT EXISTS deleted_accounts (
    user_id    TEXT PRIMARY KEY NOT NULL,
    deleted_at REAL NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY,
    user_id           TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('transcribe', 'analyze', 'takeaways', 'reports')),
    request_key       TEXT NOT NULL,                  -- the Idempotency-Key, a lowercase uuid
    attempt           INTEGER,                        -- NULL on a rejected row: it never started
    status            TEXT NOT NULL CHECK (status IN ('in_flight', 'ok', 'failed', 'rejected')),
    error_code        TEXT,
    body_hmac         BLOB,                           -- HMAC-SHA256 of the body under the server's key
    day               TEXT NOT NULL,                  -- the UTC day it started
    started_at        REAL NOT NULL,
    deadline_at       REAL NOT NULL,
    finished_at       REAL,
    reached_model     INTEGER NOT NULL DEFAULT 0,
    request_bytes     INTEGER,
    audio_seconds     REAL,                           -- decoded at start; what the provider billed at settle
    chunks            INTEGER,
    input_chars       INTEGER,
    entry_count       INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          REAL NOT NULL DEFAULT 0,
    models            TEXT,                           -- JSON array of model ids
    providers         TEXT,                           -- JSON array of provider names
    zdr               TEXT CHECK (zdr IN ('hit', 'miss', 'unknown')),
    config_version    INTEGER,
    prompt_version    TEXT,
    app_version       TEXT,
    platform          TEXT,
    CHECK ((status = 'rejected') = (attempt IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS usage_by_user_day ON usage_events (user_id, day);
CREATE INDEX IF NOT EXISTS usage_by_key ON usage_events (user_id, kind, request_key);
CREATE INDEX IF NOT EXISTS usage_in_flight ON usage_events (status, deadline_at);
CREATE INDEX IF NOT EXISTS usage_by_day ON usage_events (day);   -- the dashboard reads by window

CREATE TABLE IF NOT EXISTS refusals (                -- non-2xx answers with no usage_events row; no user
    hour    INTEGER NOT NULL,                         -- the UTC hour it was answered in, as Unix seconds
    route   TEXT NOT NULL,                            -- the route's template, or 'unmatched': never a path
    status  INTEGER NOT NULL,
    code    TEXT NOT NULL,                            -- the error envelope's code
    calls   INTEGER NOT NULL DEFAULT 0,
    last_at REAL NOT NULL,
    PRIMARY KEY (hour, route, status, code)
) STRICT;

CREATE TABLE IF NOT EXISTS daily_spend (             -- no user: it survives account deletion
    day      TEXT NOT NULL,
    kind     TEXT NOT NULL,
    calls    INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
) STRICT;

CREATE TABLE IF NOT EXISTS active_days (
    user_id     TEXT NOT NULL,
    day         TEXT NOT NULL,
    app_version TEXT,
    platform    TEXT,
    PRIMARY KEY (user_id, day)
) STRICT;

CREATE TABLE IF NOT EXISTS config (                  -- append-only; the newest valid row wins
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    body       TEXT NOT NULL,
    note       TEXT,
    created_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_records (
    user_id    TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    deleted    INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    ciphertext BLOB,
    key_id     TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, record_id)
) STRICT;
CREATE INDEX IF NOT EXISTS cloud_by_seq ON cloud_records (user_id, seq);

CREATE TABLE IF NOT EXISTS cloud_keycheck (
    user_id    TEXT PRIMARY KEY NOT NULL,
    key_id     TEXT NOT NULL,
    verifier   BLOB NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
"""


def utc_day(instant):
    return datetime.fromtimestamp(instant, timezone.utc).date().isoformat()


def next_midnight(instant):
    """(the next UTC midnight as Unix seconds, as an ISO instant)."""
    day = datetime.fromtimestamp(instant, timezone.utc).date() + timedelta(days=1)
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return midnight.timestamp(), midnight.strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")     # a Notch Cloud write the phone was told about survives a crash
    conn.execute("PRAGMA secure_delete = ON")     # a deleted account's rows are overwritten, not left in free pages
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class Account:
    def __init__(self, row):
        self.user_id = row["user_id"]
        self.blocked = row["blocked_code"] is not None
        self.blocked_code = row["blocked_code"]
        try:
            flags = json.loads(row["flags"])
        except ValueError:
            flags = {}
        self.flags = flags if isinstance(flags, dict) else {}


class Started:
    """A call past check-and-start: its row and attempt number."""

    def __init__(self, row_id, attempt, day, kind):
        self.row_id, self.attempt, self.day, self.kind = row_id, attempt, day, kind


class Meter:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = path, clock
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        conn = connect(path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _read(self):
        conn = connect(self.path)
        try:
            yield conn
        except sqlite3.Error:
            raise Refusal("unavailable") from None
        finally:
            conn.close()

    @contextmanager
    def _write(self, *, durable=True):
        """
        One BEGIN IMMEDIATE transaction: committed if the block returns, rolled back if it raises.
        `durable=False` commits without waiting for the disk (synchronous NORMAL): for counts a
        crash may lose, never for metering or Notch Cloud.
        """
        conn = connect(self.path)
        try:
            if not durable:
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.Error:
            raise Refusal("unavailable") from None
        finally:
            conn.close()

    # -- accounts -----------------------------------------------------------

    def touch_account(self, user_id):
        """
        The caller's account, created on its first authenticated call. A deleted one is
        403 account_gone, and is never created again. Read first: only a first call writes.
        """
        with self._read() as conn:
            if conn.execute("SELECT 1 FROM deleted_accounts WHERE user_id = ?", (user_id,)).fetchone():
                raise Refusal("account_gone")
            row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            return Account(row)
        now = self.clock()
        with self._write() as conn:
            return self._account(conn, user_id, now)

    def _account(self, conn, user_id, now):
        if conn.execute("SELECT 1 FROM deleted_accounts WHERE user_id = ?", (user_id,)).fetchone():
            raise Refusal("account_gone")
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, created_at) VALUES (?, ?)", (user_id, now))
        return Account(conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone())

    def is_deleted(self, user_id):
        with self._read() as conn:
            return conn.execute("SELECT 1 FROM deleted_accounts WHERE user_id = ?", (user_id,)).fetchone() is not None

    def set_blocked(self, user_id, code):
        """Block (a code) or unblock (None) an account, creating its row if it has none yet."""
        now = self.clock()
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO accounts (user_id, created_at) VALUES (?, ?)", (user_id, now))
            conn.execute("UPDATE accounts SET blocked_code = ?, blocked_at = ? WHERE user_id = ?",
                         (code, now if code else None, user_id))

    def delete_account(self, user_id):
        """Every row the account owns, then the id recorded as deleted. daily_spend has no user and stays."""
        with self._write() as conn:
            for table in ("usage_events", "active_days", "cloud_records", "cloud_keycheck", "accounts"):
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            conn.execute("INSERT OR IGNORE INTO deleted_accounts (user_id, deleted_at) VALUES (?, ?)",
                         (user_id, self.clock()))

    # -- remote config rows ---------------------------------------------------

    def latest_config_version(self):
        with self._read() as conn:
            return conn.execute("SELECT max(version) FROM config").fetchone()[0]

    def config_rows_newest_first(self):
        with self._read() as conn:
            return [(r["version"], r["body"]) for r in
                    conn.execute("SELECT version, body FROM config ORDER BY version DESC")]

    def append_config(self, body, *, note=None, created_by=None):
        with self._write() as conn:
            return conn.execute("INSERT INTO config (body, note, created_by, created_at) VALUES (?, ?, ?, ?)",
                                (body, note, created_by, self.clock())).lastrowid

    # -- activity -------------------------------------------------------------

    def mark_active(self, user_id, app_version, platform):
        """One row per account per UTC day, with the build it was last seen on."""
        now = self.clock()
        with self._write() as conn:
            conn.execute("INSERT INTO active_days (user_id, day, app_version, platform) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (user_id, day) DO UPDATE SET app_version = excluded.app_version, "
                         "platform = excluded.platform", (user_id, utc_day(now), app_version, platform))

    def usage_today(self, user_id):
        """GET /v2/config's usage block: what the backstops have counted today."""
        now = self.clock()
        day = utc_day(now)
        with self._read() as conn:
            def counted(kind):
                return conn.execute(_COUNTED, (user_id, kind, day, now)).fetchone()[0]
            return {"day_utc": day, "notches": counted("transcribe"), "reports": counted("reports"),
                    "rewrites": counted("takeaways")}

    # -- check-and-start ------------------------------------------------------

    def start(self, *, user_id, kind, key, body_hmac, deadline_seconds, config, sizes=None, versions=None):
        """
        Check the call and start it, in one transaction -> Started, or Refusal:
          403 account_gone / account_blocked, 503 processing_paused (the global breaker),
          409 idempotency_key_reused (the key came with another body), 409 request_in_flight
          (the key is running), 429 attempts_exhausted (the key reached a model
          `attempts_per_hour` times in the last hour), 429 rate_limited (`max_in_flight`
          calls running), 429 quota_exceeded (the kind's backstop of distinct keys today,
          or the account's cost today).
        A Refusal that left a `rejected` row carries `metered = True`, so the route does not
        count it again in `refusals`.
        """
        now = self.clock()
        day = utc_day(now)
        midnight, resets_at = next_midnight(now)
        sizes, versions = sizes or {}, versions or {}
        with self._write() as conn:
            refusal = self._check(conn, user_id, kind, key, body_hmac, config, now, day, midnight, resets_at)
            if refusal is None:
                attempt = 1 + (conn.execute("SELECT coalesce(max(attempt), 0) FROM usage_events "
                                            "WHERE user_id = ? AND kind = ? AND request_key = ?",
                                            (user_id, kind, key)).fetchone()[0])
                row_id = self._insert(conn, user_id, kind, key, body_hmac, attempt, "in_flight", None, day, now,
                                      now + deadline_seconds, sizes, versions)
                return Started(row_id, attempt, day, kind)
            if refusal.code != "account_gone":  # a deleted account gets no rows back
                self._insert(conn, user_id, kind, key, body_hmac, None, "rejected", refusal.code, day, now, now,
                             sizes, versions, finished=now)
                refusal.metered = True
        raise refusal  # after the rejected row has committed

    def _check(self, conn, user_id, kind, key, body_hmac, config, now, day, midnight, resets_at):
        try:
            account = self._account(conn, user_id, now)
        except Refusal as gone:
            return gone
        if account.blocked:
            return Refusal("account_blocked")
        spent = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM daily_spend WHERE day = ?", (day,)).fetchone()[0]
        if spent >= config["spend"]["global_usd_per_day"]:
            return Refusal("processing_paused", retry_after=midnight - now)
        mine = conn.execute("SELECT status, body_hmac, deadline_at, started_at, reached_model FROM usage_events "
                            "WHERE user_id = ? AND kind = ? AND request_key = ? AND status != 'rejected'",
                            (user_id, kind, key)).fetchall()
        if any(row["body_hmac"] != body_hmac for row in mine):
            return Refusal("idempotency_key_reused")
        live = [row["deadline_at"] for row in mine if row["status"] == "in_flight" and row["deadline_at"] > now]
        if live:
            return Refusal("request_in_flight", retry_after=max(live) - now)
        # An attempt that reached a model, or a stale in_flight row that may have: it was paid for.
        tried = sorted(row["started_at"] for row in mine if row["started_at"] > now - 3600
                       and (row["reached_model"] or row["status"] == "in_flight"))
        if len(tried) >= config["attempts_per_hour"]:
            return Refusal("attempts_exhausted", retry_after=tried[len(tried) - config["attempts_per_hour"]] + 3600 - now)
        running = conn.execute("SELECT count(*) FROM usage_events WHERE user_id = ? AND status = 'in_flight' "
                               "AND deadline_at > ?", (user_id, now)).fetchone()[0]
        if running >= config["max_in_flight"]:
            return Refusal("rate_limited", retry_after=RATE_LIMIT_RETRY_SECONDS)
        others = conn.execute(_COUNTED + " AND request_key != ?", (user_id, kind, day, now, key)).fetchone()[0]
        if others >= config.backstop(kind):
            return Refusal("quota_exceeded", retry_after=midnight - now, resets_at=resets_at)
        cost = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM usage_events WHERE user_id = ? AND day = ?",
                            (user_id, day)).fetchone()[0]
        if cost >= config["spend"]["account_usd_per_day"]:
            return Refusal("quota_exceeded", retry_after=midnight - now, resets_at=resets_at)
        return None

    def _insert(self, conn, user_id, kind, key, body_hmac, attempt, status, code, day, started, deadline_at, sizes,
                versions, finished=None):
        return conn.execute(
            "INSERT INTO usage_events (user_id, kind, request_key, attempt, status, error_code, body_hmac, day, "
            "started_at, deadline_at, finished_at, request_bytes, audio_seconds, input_chars, entry_count, "
            "config_version, prompt_version, app_version, platform) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, kind, key, attempt, status, code, body_hmac, day, started, deadline_at, finished,
             sizes.get("request_bytes"), sizes.get("audio_seconds"), sizes.get("input_chars"),
             sizes.get("entry_count"), versions.get("config_version"), versions.get("prompt_version"),
             versions.get("app_version"), versions.get("platform"))).lastrowid

    # -- settle -----------------------------------------------------------------

    def settle(self, started, *, ok, error_code=None, totals=None, reached_model=False, audio_seconds=None,
               chunks=None):
        """
        The call's outcome, what it cost and who served it, and the day's global spend, in
        one transaction. The row may be gone (its account deleted mid-call): the spend is
        still added, since it was still paid for.
        """
        totals = totals or {}
        cost = float(totals.get("cost") or 0.0)
        with self._write() as conn:
            conn.execute(
                "UPDATE usage_events SET status = ?, error_code = ?, finished_at = ?, reached_model = ?, "
                "cost_usd = cost_usd + ?, prompt_tokens = ?, completion_tokens = ?, models = ?, providers = ?, "
                "audio_seconds = coalesce(?, audio_seconds), chunks = coalesce(?, chunks) WHERE id = ?",
                ("ok" if ok else "failed", error_code, self.clock(), int(bool(reached_model)), cost,
                 totals.get("prompt_tokens"), totals.get("completion_tokens"),
                 json.dumps(totals.get("models") or []), json.dumps(totals.get("providers") or []),
                 audio_seconds, chunks, started.row_id))
            self._spend(conn, started.day, started.kind, cost, calls=1)

    def add_late_cost(self, started, cost):
        """A reply that landed after its call settled (the deadline gave up on it): its cost still counts."""
        if not cost:
            return
        with self._write() as conn:
            conn.execute("UPDATE usage_events SET cost_usd = cost_usd + ? WHERE id = ?", (cost, started.row_id))
            self._spend(conn, started.day, started.kind, cost, calls=0)

    def _spend(self, conn, day, kind, cost, calls):
        conn.execute("INSERT INTO daily_spend (day, kind, calls, cost_usd) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (day, kind) DO UPDATE SET calls = calls + excluded.calls, "
                     "cost_usd = cost_usd + excluded.cost_usd", (day, kind, calls, cost))

    def record_zdr(self, row_id, verdict, providers):
        """zdr.py's verdict on a settled call, with the providers the generation lookups named."""
        with self._write() as conn:
            row = conn.execute("SELECT providers FROM usage_events WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                return
            known = set(json.loads(row["providers"] or "[]")) | {p for p in providers if p}
            conn.execute("UPDATE usage_events SET zdr = ?, providers = ? WHERE id = ?",
                         (verdict, json.dumps(sorted(known)), row_id))

    def expire_stale(self):
        """At startup: in_flight rows a previous process never settled, past their deadline, become failed."""
        now = self.clock()
        with self._write() as conn:
            return conn.execute("UPDATE usage_events SET status = 'failed', error_code = 'abandoned', "
                                "finished_at = deadline_at WHERE status = 'in_flight' AND deadline_at <= ?",
                                (now,)).rowcount

    # -- answers with no usage row ------------------------------------------------

    def count_refusals(self, counts):
        """
        refusals.py's batch, {(hour, route, status, code): (calls, last_at)}, added to the
        hourly rows in one transaction, which also drops rows past REFUSALS_KEPT_SECONDS.
        Not a durable write: these are counts for the dashboard, and the disk sync a metering
        row needs would be paid for nothing.
        """
        if not counts:
            return
        rows = [(hour, route, status, code, calls, last_at)
                for (hour, route, status, code), (calls, last_at) in counts.items()]
        with self._write(durable=False) as conn:
            conn.executemany(
                "INSERT INTO refusals (hour, route, status, code, calls, last_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (hour, route, status, code) DO UPDATE SET calls = calls + excluded.calls, "
                "last_at = max(last_at, excluded.last_at)", rows)
            conn.execute("DELETE FROM refusals WHERE hour < ?", (int(self.clock() - REFUSALS_KEPT_SECONDS),))

    # -- Notch Cloud ------------------------------------------------------------

    def cloud_put(self, user_id, records, *, account_bytes):
        """
        Store a batch -> the account's cursor after it. Each record gets the next number in
        the account's sequence; a record id seen twice in one batch keeps its last write.
        The whole batch is refused (413 cloud_quota_exceeded) if it would take the account
        past `account_bytes` of ciphertext.
        """
        latest = {}
        for record in records:
            latest.pop(record["id"], None)
            latest[record["id"]] = record
        now = self.clock()
        with self._write() as conn:
            account = self._account(conn, user_id, now)
            seq = conn.execute("SELECT cloud_seq FROM accounts WHERE user_id = ?", (account.user_id,)).fetchone()[0]
            stored = conn.execute("SELECT coalesce(sum(length(ciphertext)), 0) FROM cloud_records WHERE user_id = ?",
                                  (user_id,)).fetchone()[0]
            for record_id, record in latest.items():
                old = conn.execute("SELECT length(ciphertext) FROM cloud_records WHERE user_id = ? AND record_id = ?",
                                   (user_id, record_id)).fetchone()
                stored += len(record["ciphertext"] or b"") - ((old[0] or 0) if old else 0)
            if stored > account_bytes:
                raise Refusal("cloud_quota_exceeded")
            for record_id, record in latest.items():
                seq += 1
                conn.execute(
                    "INSERT INTO cloud_records (user_id, record_id, seq, deleted, ciphertext, key_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, record_id) DO UPDATE SET seq = excluded.seq, "
                    "deleted = excluded.deleted, ciphertext = excluded.ciphertext, key_id = excluded.key_id, "
                    "updated_at = excluded.updated_at",
                    (user_id, record_id, seq, int(record["deleted"]), record["ciphertext"], record["key_id"], now))
            conn.execute("UPDATE accounts SET cloud_seq = ? WHERE user_id = ?", (seq, user_id))
            return seq

    def cloud_changes(self, user_id, since, limit, page_bytes):
        """Records written after `since`, oldest first -> (records, cursor, more). A page stops at `page_bytes`."""
        with self._read() as conn:
            rows = conn.execute("SELECT record_id, deleted, ciphertext, key_id, seq FROM cloud_records "
                                "WHERE user_id = ? AND seq > ? ORDER BY seq LIMIT ?", (user_id, since, limit + 1))
            page, size, more = [], 0, False
            for row in rows:
                weight = len(row["ciphertext"] or b"")
                if len(page) == limit or (page and size + weight > page_bytes):
                    more = True
                    break
                page.append(dict(row))
                size += weight
        return page, page[-1]["seq"] if page else since, more

    def keycheck(self, user_id):
        with self._read() as conn:
            row = conn.execute("SELECT key_id, verifier FROM cloud_keycheck WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def set_keycheck(self, user_id, key_id, verifier):
        now = self.clock()
        with self._write() as conn:
            self._account(conn, user_id, now)
            conn.execute("INSERT INTO cloud_keycheck (user_id, key_id, verifier, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (user_id) DO UPDATE SET key_id = excluded.key_id, verifier = excluded.verifier, "
                         "updated_at = excluded.updated_at", (user_id, key_id, verifier, now))

    def cloud_wipe(self, user_id):
        """Notch Cloud off: every record and the keycheck. The sequence keeps counting, so no cursor goes back."""
        with self._write() as conn:
            conn.execute("DELETE FROM cloud_records WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM cloud_keycheck WHERE user_id = ?", (user_id,))


# Distinct keys of one kind a day's backstop has counted: ok rows and in_flight rows still inside their deadline.
_COUNTED = ("SELECT count(DISTINCT request_key) FROM usage_events WHERE user_id = ? AND kind = ? AND day = ? "
            "AND (status = 'ok' OR (status = 'in_flight' AND deadline_at > ?))")
