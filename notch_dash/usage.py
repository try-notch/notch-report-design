"""
usage.py — the fleet view: how Notch is used across every account, read from the /v2 server's
meter database (notch_api/meter.py) and nothing else.

Counts, timings, costs and codes only. The meter holds no notch content by design, this reads
none of Notch Cloud's ciphertext, and an account appears only as the first eight characters of
its Supabase id: enough to follow one account's volume against the per-account daily cap, not
to name anyone.

Days are the meter's own: UTC. Every query is bounded to the last few weeks or counts rows by
an indexed column, and a read is cached for CACHE_S seconds, so a page left open costs the
server one small read every few seconds.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from notch_api import remote_config

DAYS = 14          # the daily table
WINDOW_DAYS = 7    # timings, problems, models, versions
CACHE_S = 10
KINDS = {"transcribe": "notches", "analyze": "analyses", "takeaways": "rewrites", "reports": "reports"}

_cache = {}
_lock = threading.Lock()


def read(path, now=None):
    """The usage snapshot for the meter at `path`, cached for CACHE_S; {"error": ...} if it can't be read."""
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get(path)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    try:
        value = _read(path, now)
    except sqlite3.Error as exc:
        value = {"error": f"Can't read the meter ({type(exc).__name__})."}
    with _lock:
        _cache[path] = (now, value)
    return value


def _day(instant):
    return datetime.fromtimestamp(instant, timezone.utc).date().isoformat()


def _days_back(today, count):
    """`count` UTC days ending today, newest first."""
    last = datetime.fromisoformat(today).date()
    return [(last - timedelta(days=i)).isoformat() for i in range(count)]


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _names(text):
    """A meter JSON array of models or providers -> "a, b", or "—"."""
    try:
        names = json.loads(text) if text else []
    except ValueError:
        return "—"
    return ", ".join(str(n) for n in names) or "—"


def _config(db):
    """The config the server runs: the newest valid row over the defaults."""
    for (body,) in db.execute("SELECT body FROM config ORDER BY version DESC LIMIT 20"):
        try:
            return remote_config.build(json.loads(body))
        except (ValueError, remote_config.ConfigInvalid, RecursionError):
            continue
    return remote_config.build({})


def _read(path, now):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        return _snapshot(db, now)
    finally:
        db.close()


def _snapshot(db, now):
    def rows(sql, *args):
        return db.execute(sql, args).fetchall()

    def one(sql, *args):
        return db.execute(sql, args).fetchone()[0]

    today = _day(now)
    days = _days_back(today, DAYS)
    window = _days_back(today, WINDOW_DAYS)
    since = window[-1]
    config = _config(db)
    notch_cap = config["limits"]["notches_per_day"]
    spend_caps = config["spend"]

    accounts = {
        "total": one("SELECT count(*) FROM accounts"),
        "new_7d": one("SELECT count(*) FROM accounts WHERE created_at >= ?", now - WINDOW_DAYS * 86400),
        "blocked": one("SELECT count(*) FROM accounts WHERE blocked_at IS NOT NULL"),
        "deleted": one("SELECT count(*) FROM deleted_accounts"),
        "cloud": one("SELECT count(*) FROM cloud_keycheck"),
    }
    active = {
        "today": one("SELECT count(DISTINCT user_id) FROM active_days WHERE day = ?", today),
        "7d": one("SELECT count(DISTINCT user_id) FROM active_days WHERE day >= ?", since),
        "30d": one("SELECT count(DISTINCT user_id) FROM active_days WHERE day >= ?", _days_back(today, 30)[-1]),
    }

    daily = {d: {"day": d, "active": 0, "notches": 0, "analyses": 0, "rewrites": 0, "reports": 0,
                 "failed": 0, "rejected": 0, "limited": 0, "cost_usd": 0.0} for d in days}
    for day, count in rows("SELECT day, count(DISTINCT user_id) FROM active_days WHERE day >= ? GROUP BY day",
                           days[-1]):
        if day in daily:
            daily[day]["active"] = count
    for day, kind, status, code, count in rows(
            "SELECT day, kind, status, error_code, count(*) FROM usage_events WHERE day >= ? "
            "GROUP BY day, kind, status, error_code", days[-1]):
        row = daily.get(day)
        if row is None:
            continue
        if status == "ok" and kind in KINDS:
            row[KINDS[kind]] += count
        elif status == "failed":
            row["failed"] += count
        elif status == "rejected":
            row["rejected"] += count
            if code == "quota_exceeded":
                row["limited"] += count
    for day, cost in rows("SELECT day, sum(cost_usd) FROM daily_spend WHERE day >= ? GROUP BY day", days[-1]):
        if day in daily:
            daily[day]["cost_usd"] = round(cost or 0.0, 6)
    week = [daily[d] for d in window]
    notches_7d = sum(r["notches"] for r in week)
    spend_7d = sum(r["cost_usd"] for r in week)
    totals = {
        "notches_today": daily[today]["notches"],
        "notches_7d": notches_7d,
        "reports_7d": sum(r["reports"] for r in week),
        "spend_today": daily[today]["cost_usd"],
        "spend_7d": round(spend_7d, 6),
        "spend_total": round(one("SELECT coalesce(sum(cost_usd), 0) FROM daily_spend"), 6),
        "cost_per_notch_7d": round(spend_7d / notches_7d, 6) if notches_7d else None,
        "failed_7d": sum(r["failed"] for r in week),
        "attempts_7d": sum(r["notches"] + r["analyses"] + r["rewrites"] + r["reports"] + r["failed"] for r in week),
    }

    durations, audio = {}, []
    for kind, took, seconds in rows(
            "SELECT kind, finished_at - started_at, audio_seconds FROM usage_events "
            "WHERE status = 'ok' AND finished_at IS NOT NULL AND day >= ? LIMIT 100000", since):
        durations.setdefault(kind, []).append(took)
        if kind == "transcribe" and seconds:
            audio.append(seconds)
    latency = [{"kind": kind, "calls": len(values), "p50_s": _quantile(values, 0.5),
                "p95_s": _quantile(values, 0.95), "max_s": max(values)}
               for kind, values in sorted(durations.items(), key=lambda kv: list(KINDS).index(kv[0])
                                          if kv[0] in KINDS else 99)]

    problems = [{"kind": kind, "status": status, "code": code or "—", "calls": count, "accounts": people}
                for kind, status, code, count, people in rows(
                    "SELECT kind, status, error_code, count(*), count(DISTINCT user_id) FROM usage_events "
                    "WHERE status IN ('failed', 'rejected') AND day >= ? "
                    "GROUP BY kind, status, error_code ORDER BY count(*) DESC LIMIT 12", since)]
    in_flight = {
        "now": one("SELECT count(*) FROM usage_events WHERE status = 'in_flight'"),
        "overdue": one("SELECT count(*) FROM usage_events WHERE status = 'in_flight' AND deadline_at < ?", now),
    }
    models = [{"kind": kind, "models": _names(names), "providers": _names(providers), "calls": count,
               "cost_usd": round(cost or 0.0, 6)}
              for kind, names, providers, count, cost in rows(
                  "SELECT kind, models, providers, count(*), sum(cost_usd) FROM usage_events "
                  "WHERE status = 'ok' AND day >= ? GROUP BY kind, models, providers "
                  "ORDER BY count(*) DESC LIMIT 12", since)]
    zdr = [{"kind": kind, "verdict": verdict, "calls": count}
           for kind, verdict, count in rows(
               "SELECT kind, zdr, count(*) FROM usage_events WHERE zdr IS NOT NULL AND day >= ? "
               "GROUP BY kind, zdr ORDER BY kind, zdr", since)]
    versions = [{"platform": platform or "?", "app_version": version or "?", "accounts": count}
                for platform, version, count in rows(
                    "SELECT platform, app_version, count(DISTINCT user_id) FROM active_days WHERE day >= ? "
                    "GROUP BY platform, app_version ORDER BY 3 DESC LIMIT 10", since)]
    top = [{"account": user_id[:8], "notches": notches, "calls": calls, "cost_usd": round(cost or 0.0, 6)}
           for user_id, notches, calls, cost in rows(
               "SELECT user_id, sum(kind = 'transcribe' AND status = 'ok'), count(*), sum(cost_usd) "
               "FROM usage_events WHERE day = ? GROUP BY user_id ORDER BY sum(cost_usd) DESC LIMIT 10", today)]
    at_cap = one("SELECT count(*) FROM (SELECT user_id FROM usage_events WHERE day = ? AND kind = 'transcribe' "
                 "AND status = 'ok' GROUP BY user_id HAVING count(*) >= ?)", today, notch_cap)

    return {
        "generated_at": now, "today": today, "window_days": WINDOW_DAYS,
        "accounts": accounts, "active": active, "totals": totals,
        "daily": [daily[d] for d in days],
        "latency": latency, "audio_p50_s": _quantile(audio, 0.5),
        "problems": problems, "in_flight": in_flight, "models": models, "zdr": zdr, "versions": versions,
        "top_accounts": top,
        "limits": {"notches_per_day": notch_cap, "accounts_at_cap_today": at_cap,
                   "account_usd_per_day": spend_caps["account_usd_per_day"],
                   "global_usd_per_day": spend_caps["global_usd_per_day"]},
    }
