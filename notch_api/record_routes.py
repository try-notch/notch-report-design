"""
record_routes.py — the user's record beyond capture: the list, an edit, a delete, the
takeaways written again, and a report thrown away.

  GET    /v1/entries?limit=&cursor=     every entry the user has, in any analysis state,
                                        newest first by (recorded_at, id); keyset pages
  PATCH  /v1/entries/{id}               the Edit sheet's Save; an absent key is unchanged
  DELETE /v1/entries/{id}               hard delete, the stored audio first
  POST   /v1/entries/{id}/takeaways     a rewrite for the draft; writes nothing
  DELETE /v1/reports/{id}               Discard; the report's highlights and job go with it

THE CURSOR IS A KEYSET, NOT AN OFFSET: base64 of {"r": recorded_at, "i": id} of the last
row served, so a notch captured between two page reads cannot shift page two.

AN EDIT WAITS FOR THE ANALYSIS. apply_analysis overwrites tags, takeaways and the
project when a capture job completes, so a PATCH while the entry is still processing
would be silently undone; it is refused (409 entry_processing, retryable) instead.
"""

import base64
import json
import re
import sqlite3

from fastapi import Depends, Query, Response

from . import analysis, store, web
from .classify import CATEGORIES
from .openrouter import ModelError
from .store import ApiError

PAGE_LIMIT = 100
_TEXT = web.json_body(web.TEXT_BODY_LIMIT)  # a PATCH or a rewrite may carry a long transcript
_PROCESSING = ("pending", "transcribing", "analyzing")
_PATCH_FIELDS = ("takeaways", "tags", "project_id", "transcript", "is_milestone")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def encode_cursor(row):
    key = json.dumps({"r": row["recorded_at"], "i": row["id"]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def decode_cursor(cursor):
    """A cursor this server minted -> (recorded_at, id), else 400."""
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        recorded_at, entry_id = key["r"], key["i"]
    except (ValueError, TypeError, KeyError):
        recorded_at = entry_id = None
    if not isinstance(recorded_at, str) or not _INSTANT.fullmatch(recorded_at) or not isinstance(entry_id, str):
        raise web.bad("cursor is not one this server returned.")
    return recorded_at, entry_id


def _entry_state(conn, user, entry_id):
    """The caller's entry's analysis_state; someone else's entry, or none, is 404."""
    row = conn.execute("SELECT analysis_state FROM entries WHERE id = ? AND user_id = ?", (entry_id, user)).fetchone()
    if row is None:
        raise ApiError("not_found", 404, "No such entry.")
    return row["analysis_state"]


def _strings(value, name):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise web.bad(f"{name} must be a list of strings.")
    return value


def _tags(conn, user, value):
    """Normalised and de-duplicated. A project's handle or a category name is not a tag (§3.4): 400."""
    tags = [store.normalize_tag(t) for t in _strings(value, "tags")]
    if "" in tags:
        raise web.bad("Every tag needs a letter or a digit.")
    reserved = set(CATEGORIES) | {store.normalize_tag(r["name"]) for r in
                                  conn.execute("SELECT name FROM projects WHERE user_id = ?", (user,))}
    clash = next((t for t in tags if t in reserved), None)
    if clash:
        raise web.bad(f"#{clash} names a project or a report category, not a tag.")
    return list(dict.fromkeys(tags))


def _changes(conn, user, body):
    """A PATCH body -> {column: value}. `in`, not .get(): "project_id": null unassigns, absent leaves it."""
    out = {}
    if "takeaways" in body:
        out["takeaways"] = store.json_dump([t.strip() for t in _strings(body["takeaways"], "takeaways") if t.strip()])
    if "tags" in body:
        out["tags"] = store.json_dump(_tags(conn, user, body["tags"]))
    if "project_id" in body:
        project_id = body["project_id"]
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise web.bad("project_id must be a non-empty string or null.")
        if project_id is not None and not conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND user_id = ?", (project_id, user)).fetchone():
            raise ApiError("not_found", 404, "No such project.")
        out["project_id"] = project_id
    if "transcript" in body:
        text = body["transcript"].strip() if isinstance(body["transcript"], str) else ""
        if not text:
            raise web.bad("transcript must be text with at least one word.")
        # The user's correction; raw_text (the Original) is never touched, and nothing is re-analysed.
        out |= {"corrected_text": text, "word_count": len(text.split())}
    if "is_milestone" in body:
        if not isinstance(body["is_milestone"], bool):
            raise web.bad("is_milestone must be true or false.")
        out["is_milestone"] = int(body["is_milestone"])
    return out


def register(app, *, db, audio_dir):
    @app.get("/v1/entries")
    def list_entries(limit: int = Query(PAGE_LIMIT, ge=1, le=PAGE_LIMIT), cursor: str | None = None,
                     user: str = Depends(web.user), conn=Depends(db)):
        after, before_id = decode_cursor(cursor) if cursor is not None else (None, None)
        rows = conn.execute(
            store.ENTRY_SELECT + " WHERE e.user_id = ? AND (? IS NULL OR (e.recorded_at, e.id) < (?, ?))"
            " ORDER BY e.recorded_at DESC, e.id DESC LIMIT ?", (user, after, after, before_id, limit + 1)).fetchall()
        # No filters yet, so the entries matched are all the user's: one count serves both fields.
        total = conn.execute("SELECT count(*) FROM entries WHERE user_id = ?", (user,)).fetchone()[0]
        page = rows[:limit]
        return web.reply("entry_list", {
            "entries": [store.entry_row_to_wire(r) for r in page],
            "next_cursor": encode_cursor(page[-1]) if len(rows) > limit else None,
            "matched": total, "total": total})

    @app.patch("/v1/entries/{entry_id}")
    def patch_entry(entry_id: str, body=Depends(_TEXT), user: str = Depends(web.user), conn=Depends(db)):
        body = web.json_object(body, _PATCH_FIELDS)
        if _entry_state(conn, user, entry_id) in _PROCESSING:
            raise ApiError("entry_processing", 409, "This notch is still being analysed; edit it once it is done.",
                           retryable=True)
        changes = _changes(conn, user, body)
        if changes:
            try:
                with conn:
                    conn.execute(f"UPDATE entries SET {', '.join(f'{c} = ?' for c in changes)}, updated_at = ? "
                                 "WHERE id = ? AND user_id = ?", (*changes.values(), store.now(), entry_id, user))
            except sqlite3.IntegrityError:  # the project went between the check and the write
                raise ApiError("not_found", 404, "No such project.") from None
        entry = store.load_entry(conn, user, entry_id)
        if entry is None:  # deleted between the check and the read
            raise ApiError("not_found", 404, "No such entry.")
        return web.reply("entry", entry)

    @app.delete("/v1/entries/{entry_id}", status_code=204)
    def delete_entry(entry_id: str, user: str = Depends(web.user), conn=Depends(db)):
        _entry_state(conn, user, entry_id)
        store.remove_audio(conn, audio_dir, user, entry_id)
        with conn:  # cascades to its capture job and audio rows; reports keep their frozen ids
            conn.execute("DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, user))
        return Response(status_code=204)

    @app.post("/v1/entries/{entry_id}/takeaways")
    def rewrite_takeaways(entry_id: str, body=Depends(_TEXT), user: str = Depends(web.user), conn=Depends(db)):
        transcript = web.json_object(body, ("transcript",)).get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise web.bad("transcript must be text with at least one word.")
        _entry_state(conn, user, entry_id)
        project_names, vocabulary = analysis.user_context(conn, user)
        try:
            written = analysis.write_takeaways(app.state.runner.client, transcript,
                                               project_names=project_names, vocabulary=vocabulary)
        except ModelError as exc:
            if exc.code == "model_refused":
                raise ApiError("model_refused", 502, "Couldn't write these.") from None
            raise ApiError("model_unavailable", 503, "The model service is unavailable. Try again later.",
                           retryable=True) from None
        return web.reply("takeaways", written)

    @app.delete("/v1/reports/{report_id}", status_code=204)
    def delete_report(report_id: str, user: str = Depends(web.user), conn=Depends(db)):
        with conn:  # cascades to its highlights and its job; a job mid-write finds it gone and stops
            gone = conn.execute("DELETE FROM reports WHERE id = ? AND user_id = ?", (report_id, user)).rowcount
        if not gone:
            raise ApiError("not_found", 404, "No such report.")
        return Response(status_code=204)
