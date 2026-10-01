"""
analysis.py — the capture-time analysis, and the job that runs it.

Two model calls per notch, run side by side, turn a transcript into everything the
entry needs:

  - the chat model's `label_entry` call WRITES: the hashtag-style tags the app
    shows, the summary and takeaways the cards run on (iOS E2), and E5's impact
    note and acknowledgement;
  - Jev DECIDES (classify.py): the five report categories, the mood (E2), and the
    project, each as a probability we threshold.

If Jev fails, the chat model is asked once more with the extended `label_entry`
(the measured v4 category prompt plus mood and project match) and the
classification is taken from that. `classified_by` records which path decided
(the E2E reports the split); like the categories, it never reaches the wire.

THE PROMPTS REUSE WHAT WAS MEASURED. They live in prompts.py as named variants
(LABEL_V4 here): the fallback's category policy is tagger.py's winning v4 text with the
seed catalog, and IMPACT NOTE / ACKNOWLEDGED BY / PROJECT MATCH are the measured
prompt_variants._SHARED_TAIL sections, copied there and held equal to their source by
tests/test_prompts.py. Only the rest is new: TAGS replaces AUTO TAGS, because wire tags
are now the app's hashtags (2-5, never a project or category name: §3.4 stops mirroring
the project into tags), and SUMMARY, TAKEAWAYS and MOOD are the fields a person reads.

THE MODELS' ANSWERS ARE UNTRUSTED. openrouter.py does not check the arguments
against the tool schema, so analyze_text() cleans up what can safely be cleaned
(tags normalised, blanks dropped, categories filtered to the five, which never
become tags) and refuses anything that would break the entry contract (no summary,
an unknown mood): the chat model is asked once more, then it is ModelRefused.
Categories are stored in the internal `categories` column for report facts, beside
Jev's `category_scores`, and are never sent to the app.

THE JOB FOLLOWS §3.5's MAP. Each state change writes the capture_jobs row and
the entry's analysis_state in one transaction, so the two can never disagree.
Every ModelError / AudioUnreadable fails both rows with its own closed-set code,
and anything unexpected fails them as `model_unavailable`, so a job never stays
stuck in 'processing'. A project is matched by folded name and never created:
no match leaves the notch unassigned, which is better than a wrong project.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from . import classify, prompts, store
from .audio import AudioUnreadable
from .classify import CATEGORIES, MOODS
from .openrouter import ModelError, ModelRefused

log = logging.getLogger(__name__)

MAX_TAGS = 5
MAX_TAKEAWAYS = 3
MAX_TOKENS = 1500  # labels plus two short prose fields
VOCABULARY_LIMIT = 100  # most-used first, so a long history cannot crowd the prompt


def _copied_sections(tail):
    """
    (IMPACT NOTE + ACKNOWLEDGED BY, PROJECT MATCH) sliced from the measured tail, or fail
    loudly if it changed. tests/test_prompts.py holds prompts.py's copies to this slice.
    """
    marks = [tail.find(f"\n{name}\n") for name in ("IMPACT NOTE", "ACKNOWLEDGED BY", "PROJECT MATCH")]
    if -1 in marks or marks != sorted(marks):
        raise RuntimeError("prompt_variants._SHARED_TAIL no longer has IMPACT NOTE, ACKNOWLEDGED BY and "
                           "PROJECT MATCH in that order; update analysis.py's slice.")
    return tail[marks[0]:marks[2]], tail[marks[2]:]


# The v1 names for the one variant v1 runs (prompts.LABEL_V4).
SYSTEM_PROMPT = prompts.LABEL_V4.system
FALLBACK_PROMPT = prompts.LABEL_V4.fallback_system
LABEL_ENTRY = prompts.LABEL_V4.schema
LABEL_ENTRY_FALLBACK = prompts.LABEL_V4.fallback_schema


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _user_message(transcript, project_names, vocabulary):
    """
    tagger.tag_entry's message plus the project list. The vocabulary goes here, not in
    the system prompt, because it changes per call. fakes.parse_label_message reads this layout.
    """
    projects = "".join(f"\n- {name}" for name in project_names) or " none"
    return (f"Label this entry:\n\n{transcript.strip()}\n\n"
            f"Active projects (copy a name verbatim):{projects}\n\n"
            "Tags already in use for this user. Reuse one verbatim wherever it fits rather than "
            f"coining a near-synonym: {', '.join(vocabulary) or 'none yet'}")


def _text(value):
    """A stripped string, or None when blank or not a string."""
    return (value.strip() or None) if isinstance(value, str) else None


def _strings(value):
    """A list of strings from an array field. A lone string counts as a list of one."""
    value = store.loose_json(value)
    value = [value] if isinstance(value, str) else value
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def analyze_text(client, transcript, *, project_names, vocabulary, labels=prompts.LABEL_V4, classifier="jev",
                 max_tokens=MAX_TOKENS, thresholds=None, project_confidence=None):
    """
    The chat model's writing and a classification, side by side -> a normalised result:
    {tags, summary, takeaways, impact_note, acknowledged_by, categories, category_scores,
    mood, project_name, classified_by}.

    classifier "jev" (v1, and /v2 when remote config says so): Jev decides; if Jev
    fails, the chat model classifies instead (category_scores None, classified_by
    'llm'). classifier "chat": the chat model classifies from the start, beside the
    writing, with no Jev call at all. Raises ModelRefused when the writing has no
    summary, or the chat classification an unknown mood, since a complete entry must
    have both; everything else is cleaned rather than refused. Any other ModelError
    from the chat model propagates.
    """
    user = _user_message(transcript, project_names, vocabulary)
    chat = dict(labels=labels, max_tokens=max_tokens)
    if classifier == "chat":
        with ThreadPoolExecutor(1) as pool:
            decided = pool.submit(_label_fallback, client, user, **chat)
            written = _write(client, user, project_names, **chat)
            return written | decided.result() | {"category_scores": None, "classified_by": "llm"}
    with ThreadPoolExecutor(1) as pool:
        decided = pool.submit(classify.classify, client, transcript, project_names=project_names,
                              thresholds=thresholds, project_confidence=project_confidence)
        written = _write(client, user, project_names, **chat)
        try:
            return written | decided.result() | {"classified_by": "jev"}
        except ModelError as exc:
            # The code only: a ModelError's message can quote a provider's reply.
            log.warning("Jev could not classify (%s); asking the chat model", exc.code)
    fallback = _label_fallback(client, user, **chat)
    return written | fallback | {"category_scores": None, "classified_by": "llm"}


def classify_by_chat(client, transcript, *, project_names=(), vocabulary=(), labels=prompts.LABEL_V4,
                     max_tokens=MAX_TOKENS):
    """
    The chat model's classification with the measured v4 prompt -> {categories, mood,
    project_name}. analyze_text's fallback when Jev fails; eval_categories scores it.
    """
    return _label_fallback(client, _user_message(transcript, project_names, vocabulary),
                           labels=labels, max_tokens=max_tokens)


def _label_fallback(client, user, *, labels, max_tokens):
    return _label(client, user, labels.fallback_system, labels.fallback_schema, _classified, max_tokens)


def _label(client, user, system, parameters, parse, max_tokens=MAX_TOKENS):
    """label_entry, read by `parse`; the client asks once more if `parse` refuses the answer."""
    return client.tool_call(
        system=system, user=user, tool_name="label_entry",
        description="Label one Notch journal entry and write its summary.", parameters=parameters,
        # Labelling wants the single most likely answer, as in tagger.py.
        temperature=0.0, max_tokens=max_tokens, parse=parse)


def _written(raw):
    """label_entry's writing, cleaned."""
    summary = _text(raw.get("summary"))
    if summary is None:
        raise ModelRefused("label_entry answered without a summary.")
    return {
        "tags": [t for t in store.normalize_tags(_strings(raw.get("tags"))) if t not in CATEGORIES][:MAX_TAGS],
        "summary": summary,
        "takeaways": [t for t in map(_text, _strings(raw.get("takeaways"))) if t][:MAX_TAKEAWAYS],
        "impact_note": _text(raw.get("impact_note")),
        "acknowledged_by": _text(raw.get("acknowledged_by")),
    }


def _classified(raw):
    """The fallback label_entry's categories, mood and project guess, cleaned."""
    mood = (_text(raw.get("mood")) or "").lower()
    if mood not in MOODS:
        raise ModelRefused("label_entry answered with an unknown mood.")
    fixed = {tag.strip().lower() for tag in _strings(raw.get("fixed_tags"))}
    match = store.loose_json(raw.get("project_match"))
    match = match if isinstance(match, dict) else {}
    return {
        "categories": [c for c in CATEGORIES if c in fixed],
        "mood": mood,
        "project_name": None if match.get("confidence") == "none" else _text(match.get("project_name")),
    }


def _write(client, user, project_names, parse=_written, *, labels=prompts.LABEL_V4, max_tokens=MAX_TOKENS):
    """label_entry's writing, less any tag that is a project's name (§3.4: the project is not a tag)."""
    written = _label(client, user, labels.system, labels.schema, parse, max_tokens)
    projects = {store.normalize_tag(name) for name in project_names}
    return written | {"tags": [t for t in written["tags"] if t not in projects]}


def _rewritten(raw):
    """_written, refusing an answer with no takeaway: a rewrite must replace the draft whole or not at all."""
    written = _written(raw)
    if not written["takeaways"]:
        raise ModelRefused("label_entry answered without a takeaway.")
    return written


def write_takeaways(client, transcript, *, project_names, vocabulary, labels=prompts.LABEL_V4,
                    max_tokens=MAX_TOKENS):
    """
    POST /v1/entries/{id}/takeaways and /v2/takeaways: the writing half of analyze_text
    alone, the same call and the same cleaning, -> {takeaways, tags}. Jev is not asked:
    nothing it decides is rewritten. Any ModelError propagates, and nothing is written anywhere.
    """
    written = _write(client, _user_message(transcript, project_names, vocabulary), project_names, _rewritten,
                     labels=labels, max_tokens=max_tokens)
    return {"takeaways": written["takeaways"], "tags": written["tags"]}


def apply_analysis(conn, user_id, entry_id, transcript, result):
    """
    Write an analyze_text result onto the entry and mark it complete. Does not commit:
    the caller owns the transaction (run_capture_job commits it with the job's
    'complete'; a direct caller wraps it in `with conn:`).

    raw_text is written only if unset, so the recorded "Original" never changes.
    project_id always comes from result["project_name"], matched by folded name
    within this user; no match means unassigned, and a project is never created.
    """
    name = result["project_name"]
    project_id = store.find_project_id(conn, user_id, name) if name else None
    scores = result["category_scores"]
    cursor = conn.execute(
        """
        UPDATE entries
           SET raw_text = coalesce(raw_text, ?), word_count = ?, summary = ?, takeaways = ?, mood = ?,
               tags = ?, categories = ?, category_scores = ?, classified_by = ?, impact_note = ?,
               acknowledged_by = ?, project_id = ?,
               analysis_state = 'complete', analysis_failure_code = NULL, updated_at = ?
         WHERE id = ? AND user_id = ?
        """,
        (transcript, len(transcript.split()), result["summary"], store.json_dump(result["takeaways"]),
         result["mood"], store.json_dump(store.normalize_tags(result["tags"])),
         store.json_dump(result["categories"]), None if scores is None else store.json_dump(scores),
         result["classified_by"], result["impact_note"], result["acknowledged_by"],
         project_id, store.now(), entry_id, user_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"no entry {entry_id!r} for this user")


def user_context(conn, user_id):
    """(project names, tag vocabulary most-used first) for this user's next analysis."""
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM projects WHERE user_id = ? ORDER BY name", (user_id,))]
    vocabulary = [r["value"] for r in conn.execute(
        """
        SELECT t.value FROM entries e, json_each(e.tags) t
         WHERE e.user_id = ?
         GROUP BY t.value ORDER BY count(*) DESC, t.value LIMIT ?
        """, (user_id, VOCABULARY_LIMIT))]
    return names, vocabulary


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def _transition(conn, job, state, code=None):
    """
    One step of §3.5's map: the job row and the entry's projection of it. The caller
    holds the transaction. The worker never writes 'queued', and every other job state
    has the same token on the entry. -> the entries matched: 0 once the notch is
    deleted (its job row goes with it), which ends the job.
    """
    params = {"state": state, "code": code, "at": store.now(), "job": job["id"],
              "entry": job["entry_id"], "user": job["user_id"]}
    conn.execute(
        """
        UPDATE capture_jobs
           SET state = :state, failure_code = :code, updated_at = :at,
               started_at = CASE WHEN :state = 'transcribing' THEN :at ELSE coalesce(started_at, :at) END,
               finished_at = CASE WHEN :state IN ('complete', 'failed') THEN :at END,
               attempts = attempts + (:state = 'transcribing')
         WHERE id = :job
        """, params)
    return conn.execute("UPDATE entries SET analysis_state = :state, analysis_failure_code = :code, "
                        "updated_at = :at WHERE id = :entry AND user_id = :user", params).rowcount


def _transcribe(conn, job, *, client, transcode, audio_dir):
    """The job's stored audio, segment by segment, as one transcript."""
    rows = conn.execute("SELECT storage_key FROM audio_objects WHERE capture_job_id = ? AND user_id = ?"
                        " AND purged_at IS NULL ORDER BY segment_ordinal", (job["id"], job["user_id"])).fetchall()
    if not rows:
        raise AudioUnreadable("The recording is no longer stored.")
    parts = []
    for row in rows:
        with open(os.path.join(audio_dir, row["storage_key"]), "rb") as f:
            parts.append(client.transcribe(transcode(f.read()), fmt="m4a"))
    return " ".join(parts)


def run_capture_job(db_path, job_id, *, client, transcode, audio_dir):
    """
    Run one capture job to 'complete' or 'failed'. queued -> transcribing -> analyzing ->
    complete, each step one transaction over the job and its entry.

    A job that is already complete or failed, or no longer exists, is left alone, so a
    second submit costs nothing. A job resumed after a crash starts again, but skips
    transcription when the entry already has its raw_text. Audio is read from
    `<audio_dir>/<storage_key>`.

    A notch deleted mid-job (DELETE /v1/entries/{id} or /v1/me) ends it quietly: the next
    step finds no entry, so no further model call is made, and whatever the delete broke
    in flight is not logged as a failure.
    """
    conn = store.connect(db_path)
    try:
        job = conn.execute("SELECT id, user_id, entry_id, state FROM capture_jobs WHERE id = ?",
                           (job_id,)).fetchone()
        if job is None or job["state"] in ("complete", "failed"):
            return
        user_id, entry_id = job["user_id"], job["entry_id"]
        try:
            with conn:  # read under the transition's write lock, so no delete lands in between
                if not _transition(conn, job, "transcribing"):
                    return
                transcript = conn.execute("SELECT raw_text FROM entries WHERE id = ? AND user_id = ?",
                                          (entry_id, user_id)).fetchone()["raw_text"]
            if transcript is None:
                transcript = _transcribe(conn, job, client=client, transcode=transcode, audio_dir=audio_dir)
            # The transcript, and the word count derived from it, are kept even if the analysis then fails.
            with conn:
                conn.execute("UPDATE entries SET raw_text = coalesce(raw_text, ?), word_count = ? "
                             "WHERE id = ? AND user_id = ?", (transcript, len(transcript.split()), entry_id, user_id))
                if not _transition(conn, job, "analyzing"):
                    return  # deleted while it was transcribed: no analysis for a notch that is gone
            project_names, vocabulary = user_context(conn, user_id)
            result = analyze_text(client, transcript, project_names=project_names, vocabulary=vocabulary)
            with conn:
                apply_analysis(conn, user_id, entry_id, transcript, result)
                _transition(conn, job, "complete")
        except (ModelError, AudioUnreadable) as exc:
            with conn:
                if _transition(conn, job, "failed", exc.code):  # else a delete, not a failure
                    log.warning("capture job %s failed: %s (%s)", job_id, exc.code, exc.message)
        except Exception:
            with conn:
                if _transition(conn, job, "failed", "model_unavailable"):  # else a delete broke it mid-call
                    log.exception("capture job %s crashed; failing it as model_unavailable", job_id)
    finally:
        conn.close()
