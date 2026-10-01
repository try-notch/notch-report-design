"""
account_routes.py — the account: who the user is, their settings, the numbers on the
home surfaces, and the development reset.

  GET    /v1/stats?tz=   streak, this week, this year's leaves and branches, all time, 91 days
  GET    /v1/me          the profile and settings
  PATCH  /v1/me          any subset of them, nested settings included; answers the whole object
  DELETE /v1/me          the development reset: an empty record and default settings, same user

DAYS ARE LOCAL. A notch's day is its recorded_at in the request's `tz`, else in
users.time_zone (which the app keeps current through PATCH /v1/me). Weeks start on
Monday whatever the locale, as Calendar.mondayFirst does on the phone.

THE RESET IS THE NEW-USER STATE. There is no real auth, so "delete my account" empties
the one dev user instead of removing it: stored audio first (audio_objects is the only
record of where it is), then every row the user owns, then the profile and settings
columns back to their defaults, all under one write lock so a capture landing meanwhile
is either swept with the rest or kept whole. The users row stays, so the next capture
still has its foreign key.
"""

from datetime import timedelta

from fastapi import Depends

from . import store, web
from .store import ApiError

DAYS = 91                          # the widget's grid: 13 weeks, the last cell today
GOALS = (0, 2, 3, 4, 5, 6, 7)      # 0 is "No goal"; 1 is absent by design (schema.sql's CHECK)
_PROFILE = ("display_name", "role", "industry", "years_experience")
_SETTINGS = ("weekly_goal", "reminder", "notify_week_recap", "notify_report_finished", "time_zone")
_REMINDER = ("enabled", "hour", "minute", "weekdays")
# What DELETE /v1/me resets each column to: schema.sql's defaults (a test holds the two together).
# Kept here rather than read from the table, because a database created before a default
# changed (reminder_enabled was 1) would otherwise reset to the old one.
USER_DEFAULTS = {
    "display_name": None, "role": None, "industry": None, "years_experience": None, "time_zone": "UTC",
    "weekly_goal": 5, "reminder_enabled": 0, "reminder_hour": 20, "reminder_minute": 30,
    "reminder_weekdays": "[1,2,3,4,5]", "notify_week_recap": 1, "notify_report_finished": 1,
}
# Children before parents, so no foreign key is ever left dangling mid-transaction.
_OWNED_TABLES = ("audio_objects", "capture_jobs", "entries", "report_highlights", "report_jobs", "reports", "projects")


def compute_stats(notches, today, goal):
    """
    [(local date, is_milestone)], one per complete notch, and today's local date -> the
    stats object (§5 GET /v1/stats, with register S3's calendar year as the tree window).

    streak counts back over consecutive logged days from today, or from yesterday when
    today has none yet, so it does not break at midnight; it is 0 when neither has one.
    """
    logged = {day for day, _ in notches}
    this_year = [milestone for day, milestone in notches if day.year == today.year]
    monday = today - timedelta(days=today.weekday())
    streak, day = 0, today if today in logged else today - timedelta(days=1)
    while day in logged:
        streak, day = streak + 1, day - timedelta(days=1)
    return {"streak": streak, "total": len(this_year), "record_total": len(notches), "branches": sum(this_year),
            "this_week": sum(day >= monday for day, _ in notches), "goal": goal,
            "days": [today - timedelta(days=n) in logged for n in range(DAYS - 1, -1, -1)]}


def me_to_wire(row):
    return {
        "id": row["id"], "display_name": row["display_name"], "email": None, "role": row["role"],
        "industry": row["industry"], "years_experience": row["years_experience"],
        "settings": {
            "weekly_goal": row["weekly_goal"],
            "reminder": {"enabled": bool(row["reminder_enabled"]), "hour": row["reminder_hour"],
                         "minute": row["reminder_minute"], "weekdays": store.json_list(row["reminder_weekdays"])},
            "notify_week_recap": bool(row["notify_week_recap"]),
            "notify_report_finished": bool(row["notify_report_finished"]),
            "time_zone": row["time_zone"],
        },
    }


def _bool(value, name):
    if not isinstance(value, bool):
        raise web.bad(f"{name} must be true or false.")
    return int(value)


def _int(value, allowed, message):
    if type(value) is not int or value not in allowed:  # type(), so true and 2.0 are not numbers here
        raise web.bad(message)
    return value


def _me_changes(body):
    """A PATCH /v1/me body -> {column: value}. `id` and `email` are read-only and ignored."""
    body = web.json_object(body, (*_PROFILE, "settings", "id", "email"))
    out = {}
    for key in _PROFILE:
        if key in body:
            if body[key] is not None and not isinstance(body[key], str):
                raise web.bad(f"{key} must be text or null.")
            out[key] = (body[key] or "").strip() or None  # blank clears it
    settings = web.json_object(body.get("settings", {}), _SETTINGS, "settings")
    if "weekly_goal" in settings:
        out["weekly_goal"] = _int(settings["weekly_goal"], GOALS, "settings.weekly_goal must be 0 (no goal) or 2-7.")
    reminder = web.json_object(settings.get("reminder", {}), _REMINDER, "settings.reminder")
    if "enabled" in reminder:
        out["reminder_enabled"] = _bool(reminder["enabled"], "settings.reminder.enabled")
    if "hour" in reminder:
        out["reminder_hour"] = _int(reminder["hour"], range(24), "settings.reminder.hour must be 0-23.")
    if "minute" in reminder:
        out["reminder_minute"] = _int(reminder["minute"], range(60), "settings.reminder.minute must be 0-59.")
    if "weekdays" in reminder:
        days = reminder["weekdays"]
        if not isinstance(days, list) or not all(type(d) is int and 0 <= d <= 6 for d in days):
            raise web.bad("settings.reminder.weekdays must be a list of days 0-6, Sunday being 0.")
        out["reminder_weekdays"] = store.json_dump(sorted(set(days)))
    for key in ("notify_week_recap", "notify_report_finished"):
        if key in settings:
            out[key] = _bool(settings[key], f"settings.{key}")
    if "time_zone" in settings:
        try:
            store.zone(settings["time_zone"])
        except ValueError:
            raise web.bad("settings.time_zone must be an IANA zone name, like Europe/London.") from None
        out["time_zone"] = settings["time_zone"]
    return out


def _load_me(conn, user):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user,)).fetchone()
    if row is None:
        raise ApiError("not_found", 404, "No such user.")
    return me_to_wire(row)


def register(app, *, db, audio_dir, clock):
    @app.get("/v1/stats")
    def get_stats(tz: str | None = None, user: str = Depends(web.user), conn=Depends(db)):
        try:
            zone = store.user_zone(conn, user) if tz is None else store.zone(tz)
        except ValueError:
            raise web.bad("tz must be an IANA zone name, like Europe/London.") from None
        rows = conn.execute("SELECT recorded_at, is_milestone FROM entries WHERE user_id = ? "
                            "AND analysis_state = 'complete'", (user,)).fetchall()
        goal = conn.execute("SELECT weekly_goal FROM users WHERE id = ?", (user,)).fetchone()
        return web.reply("stats", compute_stats(
            [(store.local_date(r["recorded_at"], zone), bool(r["is_milestone"])) for r in rows],
            clock().astimezone(zone).date(), goal[0] if goal else USER_DEFAULTS["weekly_goal"]))

    @app.get("/v1/me")
    def get_me(user: str = Depends(web.user), conn=Depends(db)):
        return web.reply("me", _load_me(conn, user))

    @app.patch("/v1/me")
    def patch_me(body=Depends(web.json_body()), user: str = Depends(web.user), conn=Depends(db)):
        changes = _me_changes(body)
        if changes:
            with conn:
                conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in changes)}, updated_at = ? WHERE id = ?",
                             (*changes.values(), store.now(), user))
        return web.reply("me", _load_me(conn, user))

    @app.delete("/v1/me")
    def reset(user: str = Depends(web.user), conn=Depends(db)):
        with conn:
            # One write lock from the sweep's read to the delete's commit: a capture committed before it is
            # swept whole, one committed after it survives whole, and none can leave a file with no row.
            conn.execute("BEGIN IMMEDIATE")
            store.remove_audio(conn, audio_dir, user)
            for table in _OWNED_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user,))
            conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in USER_DEFAULTS)}, updated_at = ? "
                         "WHERE id = ?", (*USER_DEFAULTS.values(), store.now(), user))
        return web.reply("deleted", {"deleted": True})
