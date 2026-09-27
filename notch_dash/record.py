"""
record.py — the read-only view of notch_api's SQLite file. One connection per snapshot,
opened mode=ro (never store.connect, which runs PRAGMAs), a few bounded queries, closed
before the snapshot is built. Instants come back as epoch seconds.
"""

import os
import pathlib
import sqlite3
from datetime import datetime, timezone

UNFINISHED = {"capture": ("queued", "transcribing", "analyzing"), "report": ("queued", "counting", "writing")}
HOUR, DAY = 3600, 86400

ROWS = """
SELECT e.id AS entry_id, j.id AS job_id, coalesce(j.state, e.analysis_state) AS state, e.recorded_at,
       coalesce(j.submitted_at, e.created_at) AS submitted_at, j.started_at,
       CASE WHEN j.id IS NOT NULL THEN j.finished_at
            WHEN e.analysis_state IN ('complete', 'failed') THEN e.updated_at END AS finished_at,
       coalesce(j.attempts, 0) AS attempts, coalesce(j.failure_code, e.analysis_failure_code) AS failure_code,
       e.duration_seconds, CASE WHEN e.raw_text IS NOT NULL THEN e.word_count END AS words
  FROM entries e LEFT JOIN capture_jobs j ON j.entry_id = e.id
 ORDER BY coalesce(j.submitted_at, e.created_at) DESC, e.id LIMIT 20"""

PENDING = """
SELECT 'capture' AS kind, state, submitted_at, started_at FROM capture_jobs
 WHERE state IN ('queued', 'transcribing', 'analyzing')
UNION ALL
SELECT 'report', state, submitted_at, NULL FROM report_jobs WHERE state IN ('queued', 'counting', 'writing')
LIMIT 500"""

FINISHED = """
SELECT kind, state, finished_at >= :hour AS last_hour, count(*) AS n FROM (
  SELECT 'capture' AS kind, state, finished_at FROM capture_jobs WHERE finished_at >= :day
  UNION ALL SELECT 'report', state, finished_at FROM report_jobs WHERE finished_at >= :day)
 GROUP BY 1, 2, 3"""


def connect_ro(path):
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def epoch(instant):
    return None if instant is None else datetime.fromisoformat(instant).timestamp()


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _states(rows, states):
    counts = dict.fromkeys(states, 0)
    for state, n in rows:
        if state in counts:
            counts[state] = n
    return counts


def read(path, now, audio_dir=None):
    """
    Everything a snapshot needs from the record, or sqlite3.Error. `audio_dir` None means
    the audio dir is unavailable, so a row's audio_on_disk is None.
    """
    at = {"now": _iso(now), "hour": _iso(now - HOUR), "day": _iso(now - DAY), "week": _iso(now - 7 * DAY)}
    conn = connect_ro(path)

    def q(sql, params=at):
        return conn.execute(sql, params).fetchall()

    try:
        rows = [dict(r) for r in q(ROWS, ())]
        keys = {}
        for r in q(f"SELECT entry_id, storage_key FROM audio_objects WHERE purged_at IS NULL AND entry_id IN "
                   f"({','.join('?' * len(rows))})", [r["entry_id"] for r in rows]):
            keys.setdefault(r["entry_id"], []).append(r["storage_key"])
        pending = [dict(r) for r in q(PENDING, ())]
        finished = q(FINISHED)
        counts_24h = q("SELECT coalesce(j.state, e.analysis_state), count(*) FROM entries e LEFT JOIN capture_jobs j"
                       " ON j.entry_id = e.id WHERE coalesce(j.submitted_at, e.created_at) >= :day GROUP BY 1")
        job_entries = dict(q("SELECT id, entry_id FROM capture_jobs WHERE updated_at >= :day LIMIT 2000"))
        entries = q("SELECT count(*), coalesce(sum(recorded_at >= :week), 0) FROM entries")[0]
        by_state = q("SELECT analysis_state, count(*) FROM entries GROUP BY 1", ())
        projects = q("SELECT count(*) FROM projects", ())[0][0]
        reports = q("SELECT count(*), max(generated_at) FROM reports", ())[0]
        report_jobs = q("SELECT state, count(*) FROM report_jobs GROUP BY 1", ())
        audio = q("SELECT count(*), coalesce(sum(byte_size), 0),"
                  " coalesce(sum(purge_after < :now AND purged_at IS NULL), 0) FROM audio_objects")[0]
    finally:
        conn.close()

    for r in rows:
        r["state"] = "queued" if r["state"] == "pending" else r["state"]
        r.update({k: epoch(r[k]) for k in ("recorded_at", "submitted_at", "started_at", "finished_at")})
        found = keys.get(r["entry_id"])
        r["audio_on_disk"] = None if audio_dir is None else bool(found) and all(
            os.path.exists(os.path.join(audio_dir, key)) for key in found)
    for j in pending:
        j.update(submitted_at=epoch(j["submitted_at"]), started_at=epoch(j["started_at"]))
    done = {kind: {"complete": 0, "failed": 0, "failed_1h": 0} for kind in UNFINISHED}
    for kind, state, last_hour, n in finished:
        if state in ("complete", "failed"):
            done[kind][state] += n
            done[kind]["failed_1h"] += n if state == "failed" and last_hour else 0
    counts = dict(counts_24h)
    return {
        "rows": rows, "pending": pending, "finished": done, "job_entries": job_entries,
        "counts_24h": {"notches": sum(counts.values()), "complete": counts.get("complete", 0),
                       "failed": counts.get("failed", 0), "in_flight": sum(j["kind"] == "capture" for j in pending)},
        "record": {
            "entries": {"total": entries[0], "last_7d": entries[1],
                        "by_state": _states(by_state, ("pending", "transcribing", "analyzing", "complete", "failed"))},
            "projects": projects,
            "reports": {"total": reports[0], "last_generated_at": epoch(reports[1])},
            "report_jobs": _states(report_jobs, ("queued", "counting", "writing", "complete", "failed")),
            "audio": {"objects": audio[0], "db_bytes": audio[1], "past_retention": audio[2]},
        },
    }
