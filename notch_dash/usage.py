"""
usage.py — the fleet view: how Notch is used across every account, read from the /v2 server's
meter database (notch_api/meter.py) and nothing else.

Counts, timings, costs and codes only. The meter holds no notch content by design, this reads
none of Notch Cloud's ciphertext, and an account appears only as the first eight characters of
its Supabase id: enough to follow one account's volume against the per-account daily cap, not
to name anyone. The row-level list (`recent`) carries a call's sizes, tokens, cost, models,
providers and versions, never its Idempotency-Key or body HMAC.

THE HEADLINE IS THE LAST 24 HOURS; THE TABLE AND THE CAPS ARE UTC DAYS. The meter's days are
UTC, and so are the limits it enforces, so the day table and "at the cap" stay UTC. A UTC
"today" is empty every evening in America, though, so the headline counts the 24 hours before
now. There an account is one that made a call: `active_days` records a config fetch by day,
with no time of day.

SMALL NUMBERS ARE SHOWN AS WHAT THEY ARE. A p95 needs P95_FROM calls; under that the page has
the typical call and the slowest. A version counts an account once, under the newest build it
ran on its latest day. A model is one row however many providers served it. The cost of a
notch is its transcription and write-up; reports and rewrites are priced apart.

MONEY IS SUMMED, THEN ROUNDED ONCE, so seven days can never come out above all time.

PROBLEMS HAVE THREE SOURCES: a call that started and failed, a call the meter refused (a limit,
a reused key), and a call turned away before the meter saw it (`refusals`: no token, an app
too old, a switch that is off, unreadable audio). A request to a path that does not exist, or
with a method its path does not take, is a probe: one count, never a row. A 404 from a real
route is neither: it is that route's answer (Notch Cloud's keycheck says it until a key is
set up). A meter from before `refusals` existed reads as having none.

Every query is bounded to the last few weeks or counts rows by an indexed column, and a read
is cached for CACHE_S seconds, so a page left open costs the server one small read every few
seconds.
"""

import json
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from notch_api import remote_config

V = 2              # the document's version: the page reloads when it changes
DAYS = 14          # how far back the day table looks
WINDOW_DAYS = 7    # timings, problems, models, versions, accounts
CACHE_S = 10
RECENT = 30        # calls in the row-level list
ACCOUNT_ROWS = 25
PROBLEM_ROWS = 20
P95_FROM = 20      # calls before a p95 says anything
ROWS = 100_000     # the most rows one aggregate reads into Python
DAY, HOUR = 86400, 3600
KINDS = {"transcribe": "notches", "analyze": "analyses", "takeaways": "rewrites", "reports": "reports"}
NO_ROUTE = "unmatched"   # refusals.py's route for a request the router matched to nothing
AUDITED = {"transcribe": ("recording", "transcribed"), "analyze": ("write-up", "classified")}

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


def _midnight(day):
    """The Unix time a UTC day starts."""
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


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


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _usd(value):
    return None if value is None else round(value, 6)


def _list(text):
    """A meter JSON array of models or providers -> its names."""
    try:
        names = json.loads(text) if text else []
    except ValueError:
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


def _build(version):
    """'1.0.10' -> (1, 0, 10), so builds compare as numbers; anything else sorts first."""
    try:
        return tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return ()


def _count(n, noun, plural=None):
    return f"{n:,} {noun if n == 1 else plural or noun + 's'}"


def _words(code):
    return (code or "no code").replace("_", " ")


def _config(db):
    """The config the server runs: ({version, note, created_by, created_at}, its merged body)."""
    for version, body, note, created_by, created_at in db.execute(
            "SELECT version, body, note, created_by, created_at FROM config ORDER BY version DESC LIMIT 20"):
        try:
            built = remote_config.build(json.loads(body))
        except (ValueError, remote_config.ConfigInvalid, RecursionError):
            continue
        return {"version": version, "note": note, "created_by": created_by, "created_at": created_at}, built
    return {"version": 0, "note": None, "created_by": None, "created_at": None}, remote_config.build({})


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
    since, oldest, day_ago = window[-1], days[-1], now - DAY
    meta, config = _config(db)
    notch_cap, spend_caps = config["limits"]["notches_per_day"], config["spend"]

    # -- answers the meter never saw: probes are a count, the rest are problems ------------
    refusals = []
    if one("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'refusals'"):
        refusals = rows("SELECT hour, route, status, code, calls, last_at FROM refusals WHERE hour >= ? LIMIT ?",
                        int(_midnight(oldest)), ROWS)
    probes = [r for r in refusals if r[1] == NO_ROUTE or r[3] == "method_not_allowed"]
    turned = [r for r in refusals if r[1] != NO_ROUTE and r[3] not in ("method_not_allowed", "not_found")]
    week_from = _midnight(since)

    # -- accounts ---------------------------------------------------------------------------
    accounts = {
        "total": one("SELECT count(*) FROM accounts"),
        "new_7d": one("SELECT count(*) FROM accounts WHERE created_at >= ?", now - WINDOW_DAYS * DAY),
        "blocked": one("SELECT count(*) FROM accounts WHERE blocked_at IS NOT NULL"),
        "deleted": one("SELECT count(*) FROM deleted_accounts"),
        "cloud": one("SELECT count(*) FROM cloud_keycheck"),
        "notched": one("SELECT count(DISTINCT user_id) FROM usage_events WHERE kind = 'transcribe' AND status = 'ok'"),
    }
    # Active: the app fetched its config signed in (active_days) or made a processing call that
    # day. Either alone undercounts: a phone with a fresh config processes without asking again.
    seen = ("(SELECT user_id, day, platform, app_version FROM active_days WHERE day >= ?1 "
            "UNION SELECT user_id, day, platform, app_version FROM usage_events WHERE day >= ?1)")

    def active_since(day):
        return one(f"SELECT count(DISTINCT user_id) FROM {seen}", day)

    # -- day by day (UTC), from the first day anything happened ---------------------------------
    daily = {d: {"day": d, "active": 0, "notches": 0, "analyses": 0, "rewrites": 0, "reports": 0,
                 "failed": 0, "refused": 0, "turned_away": 0, "cost_usd": 0.0} for d in days}
    for day, count in rows(f"SELECT day, count(DISTINCT user_id) FROM {seen} GROUP BY day", oldest):
        if day in daily:
            daily[day]["active"] = count
    for day, kind, status, count in rows(
            "SELECT day, kind, status, count(*) FROM usage_events WHERE day >= ? GROUP BY day, kind, status", oldest):
        row = daily.get(day)
        if row is None:
            continue
        if status == "ok" and kind in KINDS:
            row[KINDS[kind]] += count
        elif status == "failed":
            row["failed"] += count
        elif status == "rejected":
            row["refused"] += count
    for hour, _, _, _, calls, _ in turned:
        if _day(hour) in daily:
            daily[_day(hour)]["turned_away"] += calls
    for day, cost in rows("SELECT day, sum(cost_usd) FROM daily_spend WHERE day >= ? GROUP BY day", oldest):
        if day in daily:
            daily[day]["cost_usd"] = cost or 0.0
    busy = [d for d in days if any(daily[d][k] for k in daily[d] if k != "day")]
    shown = days[:days.index(busy[-1]) + 1] if busy else []

    # -- the last 7 days and all time ----------------------------------------------------------
    week = [daily[d] for d in window]
    by_kind = {kind: (cost or 0.0, done) for kind, cost, done in rows(
        "SELECT kind, sum(cost_usd), sum(status = 'ok') FROM usage_events WHERE day >= ? GROUP BY kind", since)}
    notches_7d, reports_7d = sum(r["notches"] for r in week), sum(r["reports"] for r in week)
    ever = dict(rows("SELECT kind, count(*) FROM usage_events WHERE status = 'ok' GROUP BY kind"))
    spend_7d = sum(r["cost_usd"] for r in week)
    # Two sums of the same rows in a different order can differ in the last bit: all time is never the smaller.
    spend_total = max(one("SELECT coalesce(sum(cost_usd), 0) FROM daily_spend"), spend_7d)

    # -- the last 24 hours, on the clock ---------------------------------------------------------
    last = {"accounts": set(), "notches": 0, "reports": 0, "rewrites": 0, "spend_usd": 0.0, "failed": 0,
            "refused": 0, "turned_away": sum(r[4] for r in turned if r[0] + HOUR > day_ago), "calls": 0}
    failed_codes = Counter()
    for user_id, kind, status, code, cost in rows(
            "SELECT user_id, kind, status, error_code, cost_usd FROM usage_events WHERE day >= ? AND started_at >= ? "
            "LIMIT ?", _day(day_ago), day_ago, ROWS):
        last["accounts"].add(user_id)
        last["calls"] += 1
        last["spend_usd"] += cost or 0.0
        if status == "ok" and kind in ("transcribe", "reports", "takeaways"):
            last[{"transcribe": "notches", "reports": "reports", "takeaways": "rewrites"}[kind]] += 1
        elif status == "failed":
            last["failed"] += 1
            failed_codes[code] += 1
        elif status == "rejected":
            last["refused"] += 1
    last["accounts"], last["calls"] = len(last["accounts"]), last["calls"] + last["turned_away"]
    turned_codes = Counter()
    for hour, _, _, code, calls, _ in turned:
        if hour + HOUR > day_ago:
            turned_codes[code] += calls

    # -- each kind of call, by the prompt that wrote it ------------------------------------------
    groups, audio = {}, []
    for kind, prompt, status, took, cost, tokens_in, tokens_out, seconds in rows(
            "SELECT kind, prompt_version, status, finished_at - started_at, cost_usd, prompt_tokens, "
            "completion_tokens, audio_seconds FROM usage_events WHERE day >= ? AND status IN ('ok', 'failed') "
            "LIMIT ?", since, ROWS):
        group = groups.setdefault((kind, prompt), {"took": [], "cost": [], "in": [], "out": [], "failed": 0})
        if status == "failed":
            group["failed"] += 1
            continue
        group["took"].append(took)
        group["cost"].append(cost)
        group["in"].append(tokens_in)
        group["out"].append(tokens_out)
        if kind == "transcribe" and seconds:
            audio.append(seconds)
    order = list(KINDS)
    # `calls` are the ones that finished: every time, cost and token count beside it is theirs.
    calls = [{"kind": kind, "prompt_version": prompt, "calls": len(g["took"]), "failed": g["failed"],
              "p50_s": _quantile(g["took"], 0.5),
              "p95_s": _quantile(g["took"], 0.95) if len(g["took"]) >= P95_FROM else None,
              "max_s": max(g["took"]) if g["took"] else None, "cost_usd": _usd(_mean(g["cost"])),
              "prompt_tokens": _mean(g["in"]), "completion_tokens": _mean(g["out"])}
             for (kind, prompt), g in sorted(groups.items(), key=lambda kv: (
                 order.index(kv[0][0]) if kv[0][0] in order else 99, kv[0][1] or ""))]

    # -- the newest calls, one row each ------------------------------------------------------------
    recent = [{"at": started, "kind": kind, "account": user_id[:8], "status": status, "code": code,
               "overdue": status == "in_flight" and deadline < now,
               "took_s": finished - started if finished is not None and status in ("ok", "failed") else None,
               "cost_usd": _usd(cost), "prompt_tokens": tokens_in, "completion_tokens": tokens_out,
               "audio_seconds": seconds, "input_chars": chars, "entry_count": entries, "models": _list(models),
               "providers": _list(providers), "prompt_version": prompt, "config_version": config_version,
               "app_version": app_version, "platform": platform, "attempt": attempt, "zdr": zdr}
              for (user_id, kind, attempt, status, code, started, deadline, finished, seconds, chars, entries,
                   tokens_in, tokens_out, cost, models, providers, zdr, config_version, prompt, app_version,
                   platform) in rows(
                  "SELECT user_id, kind, attempt, status, error_code, started_at, deadline_at, finished_at, "
                  "audio_seconds, input_chars, entry_count, prompt_tokens, completion_tokens, cost_usd, models, "
                  "providers, zdr, config_version, prompt_version, app_version, platform FROM usage_events "
                  "ORDER BY id DESC LIMIT ?", RECENT)]

    # -- problems: failed, refused by the meter, turned away before it -------------------------------
    problems = [{"where": kind, "outcome": "failed" if status == "failed" else "refused", "status": None,
                 "code": code or "unknown", "calls": count, "accounts": people, "last_at": last_at}
                for kind, status, code, count, people, last_at in rows(
                    "SELECT kind, status, error_code, count(*), count(DISTINCT user_id), max(started_at) "
                    "FROM usage_events WHERE status IN ('failed', 'rejected') AND day >= ? "
                    "GROUP BY kind, status, error_code", since)]
    early = {}
    for hour, route, status, code, count, last_at in turned:
        if hour >= week_from:
            held = early.setdefault((route, status, code), [0, last_at])
            held[0], held[1] = held[0] + count, max(held[1], last_at)
    problems += [{"where": route, "outcome": "turned_away", "status": status, "code": code, "calls": count,
                  "accounts": None, "last_at": last_at} for (route, status, code), (count, last_at) in early.items()]
    problems.sort(key=lambda p: (-p["calls"], -(p["last_at"] or 0)))
    in_flight = {
        "now": one("SELECT count(*) FROM usage_events WHERE status = 'in_flight'"),
        "overdue": one("SELECT count(*) FROM usage_events WHERE status = 'in_flight' AND deadline_at < ?", now),
    }

    # -- models, with who served them -----------------------------------------------------------------
    served = {}
    for kind, names, providers, cost in rows(
            "SELECT kind, models, providers, cost_usd FROM usage_events WHERE status = 'ok' AND day >= ? LIMIT ?",
            since, ROWS):
        model = served.setdefault((kind, ", ".join(_list(names)) or "—"), {"calls": 0, "cost": 0.0, "by": Counter()})
        model["calls"] += 1
        model["cost"] += cost or 0.0
        model["by"].update(_list(providers))
    models = [{"kind": kind, "model": name, "calls": m["calls"], "cost_usd": _usd(m["cost"]),
               "providers": [{"name": p, "calls": n}
                             for p, n in sorted(m["by"].items(), key=lambda pn: (-pn[1], pn[0]))]}
              for (kind, name), m in sorted(served.items(), key=lambda kv: (
                  order.index(kv[0][0]) if kv[0][0] in order else 99, -kv[1]["calls"]))]

    # -- zero data retention: what the audit could and could not confirm ---------------------------------
    verdicts = {}
    for kind, verdict, count in rows(
            "SELECT kind, zdr, count(*) FROM usage_events WHERE zdr IS NOT NULL AND day >= ? GROUP BY kind, zdr",
            since):
        verdicts.setdefault(kind, {"kind": kind, "hit": 0, "miss": 0, "unknown": 0})[verdict] = count
    zdr = {key: sum(v[key] for v in verdicts.values()) for key in ("hit", "miss", "unknown")}
    zdr |= {"audited": sum(zdr.values()), "kinds": sorted(verdicts.values(), key=lambda v: v["kind"])}

    # -- builds and accounts: an account counts once, under the newest build of its latest day ----------
    sightings = {}
    for user_id, day, platform, app_version in rows(f"SELECT user_id, day, platform, app_version FROM {seen} LIMIT ?2",
                                                    since, ROWS):
        held = sightings.setdefault(user_id, {"day": day, "builds": set()})
        if day > held["day"]:
            held["day"], held["builds"] = day, set()
        if day == held["day"] and app_version:
            held["builds"].add((_build(app_version), app_version, platform or "?"))
    latest = {user_id: max(s["builds"])[1:] for user_id, s in sightings.items() if s["builds"]}
    versions = [{"platform": platform, "app_version": app_version, "accounts": count}
                for (app_version, platform), count in sorted(
                    Counter(latest.values()).items(), key=lambda kv: (-kv[1], tuple(-n for n in _build(kv[0][0]))))]
    used = {user_id: (last_call, notches, reports, cost, today_notches) for
            user_id, last_call, notches, reports, cost, today_notches in rows(
                "SELECT user_id, max(started_at), sum(kind = 'transcribe' AND status = 'ok'), "
                "sum(kind = 'reports' AND status = 'ok'), sum(cost_usd), "
                "sum(day = ?2 AND kind = 'transcribe' AND status = 'ok') FROM usage_events WHERE day >= ?1 "
                "GROUP BY user_id LIMIT ?3", since, today, ROWS)}

    def last_seen(user_id):
        return max(used.get(user_id, (0,))[0] or 0, _midnight(sightings[user_id]["day"]))

    account_rows = []
    for user_id in sorted(sightings, key=last_seen, reverse=True)[:ACCOUNT_ROWS]:
        last_call, notches, reports, cost, today_notches = used.get(user_id, (None, 0, 0, 0.0, 0))
        app_version, platform = latest.get(user_id, (None, None))
        account_rows.append({"account": user_id[:8], "last_call_at": last_call,
                             "last_active_day": sightings[user_id]["day"], "platform": platform,
                             "app_version": app_version, "notches_7d": notches, "reports_7d": reports,
                             "spend_7d_usd": _usd(cost or 0.0), "notches_today": today_notches})
    at_cap = one("SELECT count(*) FROM (SELECT user_id FROM usage_events WHERE day = ? AND kind = 'transcribe' "
                 "AND status = 'ok' GROUP BY user_id HAVING count(*) >= ?)", today, notch_cap)

    spend_today = daily[today]["cost_usd"]
    last["spend_usd"] = _usd(last["spend_usd"])
    return {
        "v": V, "generated_at": now, "today": today, "resets_at": _midnight(today) + DAY,
        "window_days": WINDOW_DAYS, "daily_days": DAYS,
        "attention": _attention(verdicts, in_flight, failed_codes, turned_codes, spend_today,
                                spend_caps["global_usd_per_day"]),
        "last_24h": last,
        "week": {"active": active_since(since), "active_30d": active_since(_days_back(today, 30)[-1]),
                 "notches": notches_7d, "reports": reports_7d, "rewrites": sum(r["rewrites"] for r in week),
                 "spend_usd": _usd(spend_7d),
                 "notch_cost_usd": _usd((by_kind.get("transcribe", (0.0, 0))[0] + by_kind.get("analyze", (0.0, 0))[0])
                                        / notches_7d) if notches_7d else None,
                 "report_cost_usd": _usd(by_kind["reports"][0] / reports_7d) if reports_7d else None,
                 "failed": sum(r["failed"] for r in week),
                 "attempts": sum(r["notches"] + r["analyses"] + r["rewrites"] + r["reports"] + r["failed"]
                                 for r in week)},
        "all_time": {"notches": ever.get("transcribe", 0), "reports": ever.get("reports", 0),
                     "spend_usd": _usd(spend_total),
                     "since": one("SELECT min(day) FROM (SELECT min(day) AS day FROM daily_spend UNION ALL "
                                  "SELECT min(day) FROM active_days UNION ALL SELECT min(day) FROM usage_events)")},
        "accounts": accounts, "account_rows": account_rows,
        "account_rows_more": max(0, len(sightings) - len(account_rows)),
        "daily": [daily[d] | {"cost_usd": _usd(daily[d]["cost_usd"])} for d in shown],
        "calls": calls, "audio_p50_s": _quantile(audio, 0.5),
        "recent": recent,
        "problems": problems[:PROBLEM_ROWS],
        "probes_7d": sum(r[4] for r in probes if r[0] >= week_from),
        "in_flight": in_flight, "models": models, "zdr": zdr, "versions": versions,
        "config": meta | {"prompts": config["prompts"], "classifier": config["classifier"], "models": config["models"]},
        "limits": {"notches_per_day": notch_cap, "accounts_at_cap_today": at_cap,
                   "account_usd_per_day": spend_caps["account_usd_per_day"],
                   "global_usd_per_day": spend_caps["global_usd_per_day"], "spend_today_usd": _usd(spend_today)},
    }


def _attention(verdicts, in_flight, failed_codes, turned_codes, spend_today, spend_cap):
    """
    What someone should look at, worded for the page: [{level: "critical" | "warn", text}],
    the critical ones first. Everything here is also in its own section; this is the page's
    one banner, so a quiet page really is quiet.
    """
    def codes(counter):
        named = ", ".join(f"{_words(code)} ×{n:,}" for code, n in counter.most_common(6))
        return named + (f" and {_count(len(counter) - 6, 'other code')}" if len(counter) > 6 else "")

    critical, warn = [], []
    for kind, v in verdicts.items():
        noun, verb = AUDITED.get(kind, ("call", "served"))
        if v["miss"]:
            critical.append(f"{_count(v['miss'], noun)} in the last 7 days went to a provider that isn’t on "
                            "OpenRouter’s zero-retention list.")
        if v["unknown"]:
            audited = _count(v["hit"] + v["miss"] + v["unknown"], noun)
            warn.append(f"Zero retention couldn’t be confirmed for {v['unknown']:,} of {audited} in the last 7 days: "
                        f"OpenRouter didn’t say which provider {verb} them.")
    if spend_cap and spend_today >= spend_cap:
        critical.append(f"Today’s spend has reached the ${spend_cap:g} daily cap, so processing is paused until the "
                        "UTC day ends.")
    elif spend_cap and spend_today >= 0.8 * spend_cap:
        warn.append(f"Today’s spend is at {round(100 * spend_today / spend_cap)}% of the ${spend_cap:g} daily cap.")
    if in_flight["overdue"]:
        n = in_flight["overdue"]
        warn.append(f"{_count(n, 'call')} {'is' if n == 1 else 'are'} past {'its' if n == 1 else 'their'} deadline "
                    "and still marked in flight.")
    if failed_codes:
        warn.append(f"{_count(sum(failed_codes.values()), 'call')} failed in the last 24 hours: "
                    f"{codes(failed_codes)}.")
    if turned_codes:
        n = sum(turned_codes.values())
        warn.append(f"{_count(n, 'call')} {'was' if n == 1 else 'were'} turned away before processing in the last "
                    f"24 hours: {codes(turned_codes)}.")
    return [{"level": "critical", "text": t} for t in critical] + [{"level": "warn", "text": t} for t in warn]
