"""
analysis.py: what the models' answers become on the entry, and the capture job that
gets it there. Every model call goes to FakeClient, and each upload starts with
TEXT_MARKER, so a test picks its transcript by picking its audio bytes. Jev's own
answers are parsed in test_classify.py; here it is either answering or down.
"""

import base64
import json
import subprocess

import pytest

import prompt_variants
import seed_db
from notch_api import analysis, audio, contract, store
from notch_api.audio import AudioUnreadable
from notch_api.config import DEV_USER_ID as DEV, MAX_UPLOAD_BYTES
from notch_api.fakes import TEXT_MARKER, FakeClient, fake_transcode, parse_label_message
from notch_api.openrouter import ModelRefused, ModelUnavailable

OTHER = "00000000-0000-4000-8000-000000000002"
SPOKEN = "Shipped the Front-End Refactor login page with Dana today."
AUDIO = TEXT_MARKER + SPOKEN.encode()


def _label(client, transcript=SPOKEN, projects=(), vocabulary=()):
    return analysis.analyze_text(client, transcript, project_names=list(projects), vocabulary=list(vocabulary))


def _messy(**fields):
    """A FakeClient whose label_entry answer has these fields replaced, unchecked."""
    return FakeClient(overrides={"label_entry": fields})


class _JevDown(FakeClient):
    """Jev fails after its retries; the chat model answers, messily if `fields` says so."""

    def __init__(self, **fields):
        super().__init__(overrides={"label_entry": fields})

    def decide(self, state, questions):
        self._record("decide", state=state, questions=questions)
        raise ModelUnavailable("OpenRouter unavailable after 5 attempts (HTTP 503).")


def _labels(client):
    """The label_entry calls a client received, in order: (system prompt, tool schema)."""
    return [(kw["system"], kw["parameters"]) for method, kw in client.calls if method == "tool_call"]


def _job(conn, job_id):
    return conn.execute("SELECT * FROM capture_jobs WHERE id = ?", (job_id,)).fetchone()


# ---------------------------------------------------------------------------
# analyze_text: the models' answers are untrusted
# ---------------------------------------------------------------------------

def test_jev_classifies_while_the_chat_model_writes():
    client = FakeClient()
    result = _label(client, projects=["Data Platform", "Front-End Refactor"])
    assert _labels(client) == [(analysis.SYSTEM_PROMPT, analysis.LABEL_ENTRY)]  # no fallback call
    assert (result["classified_by"], result["categories"], result["mood"], result["project_name"]) == (
        "jev", ["wins", "collaboration"], "up", "Front-End Refactor")
    assert set(result["category_scores"]) == set(analysis.CATEGORIES)
    assert result["summary"] == SPOKEN
    # §3.4: the project travels as project_id, and stops being mirrored into tags.
    assert result["tags"] and not {"front-end-refactor", "data-platform"} & set(result["tags"])


def test_messy_writing_is_cleaned_and_categories_never_become_tags():
    result = _label(_messy(
        tags=["#Wins", "Flaky Tests", "flaky-tests", "collaboration", "  ", "Shipped"],
        takeaways=["  ", " Fixed the flaky test. "], impact_note="  ", acknowledged_by="",
    ))
    assert result["tags"] == ["flaky-tests", "shipped"]
    assert result["takeaways"] == ["Fixed the flaky test."]
    assert result["impact_note"] is None and result["acknowledged_by"] is None


@pytest.mark.parametrize("client", [_JevDown(), FakeClient(overrides={"decide": {"mood": {"choice": "ecstatic"}}})],
                         ids=["jev-unavailable", "jev-answer-malformed"])
def test_when_jev_fails_the_chat_model_classifies_with_the_measured_prompt(client):
    result = _label(client, projects=["Front-End Refactor"])
    assert _labels(client) == [(analysis.SYSTEM_PROMPT, analysis.LABEL_ENTRY),
                               (analysis.FALLBACK_PROMPT, analysis.LABEL_ENTRY_FALLBACK)]
    assert (result["classified_by"], result["category_scores"]) == ("llm", None)
    assert (result["categories"], result["mood"], result["project_name"]) == (
        ["wins", "collaboration"], "up", "Front-End Refactor")
    assert result["summary"] == SPOKEN  # the writing is the first call's


def test_messy_fallback_classification_is_cleaned():
    result = _label(_JevDown(fixed_tags=["Wins", "made-up", "wins"], mood=" Up "))
    assert (result["categories"], result["mood"]) == (["wins"], "up")


def test_arrays_and_objects_sent_as_json_text_are_read_back():
    result = _label(_JevDown(tags='["shipped", "pairing"]', takeaways="Shipped it.", fixed_tags='["growth"]',
                             project_match='{"project_name": "Front-End Refactor", "confidence": "high"}'))
    assert result["tags"] == ["shipped", "pairing"]
    assert result["takeaways"] == ["Shipped it."]
    assert result["categories"] == ["growth"]
    assert result["project_name"] == "Front-End Refactor"


@pytest.mark.parametrize("match, expected", [
    ({"project_name": "Front-End Refactor", "confidence": "none"}, None),  # 'none' outranks a name
    ({"project_name": "  ", "confidence": "high"}, None),                  # blank is no match
    ({"project_name": " the refactor ", "confidence": "low"}, "the refactor"),  # low is still a guess worth folding
    ("Front-End Refactor", None),                                          # not an object at all
])
def test_fallback_project_guess(match, expected):
    assert _label(_JevDown(project_match=match))["project_name"] == expected


@pytest.mark.parametrize("client", [_messy(summary="   "), _JevDown(mood="ecstatic")], ids=["no-summary", "no-mood"])
def test_an_answer_that_cannot_complete_an_entry_is_refused(client):
    with pytest.raises(ModelRefused):
        _label(client)


@pytest.mark.parametrize("projects", [[], ["Data Platform", "Front-End Refactor"]])
def test_the_message_carries_transcript_projects_and_vocabulary(projects):
    client = _JevDown()
    _label(client, projects=projects, vocabulary=["shipped", "project-atlas"])
    users = [kw["user"] for method, kw in client.calls if method == "tool_call"]
    assert len(users) == 2 and len(set(users)) == 1  # the fallback sees what the writer saw
    assert parse_label_message(users[0]) == (SPOKEN, projects)
    assert "shipped, project-atlas" in users[0]
    (state,) = [kw["state"] for method, kw in client.calls if method == "decide"]
    assert list(state.values()) == [SPOKEN]


def test_the_measured_prompt_is_sent_verbatim_and_drift_fails_loudly():
    tail = prompt_variants._SHARED_TAIL
    catalog = prompt_variants.format_tag_catalog(
        [{"name": n, "explanation": e} for n, e in seed_db.TAG_CATALOG])
    measured = prompt_variants._SHARED_PREAMBLE + prompt_variants.V4_FIXED.replace("{tag_catalog}", catalog)
    assert analysis.FALLBACK_PROMPT.startswith(measured)
    assert analysis.FALLBACK_PROMPT.endswith(tail[tail.index("\nIMPACT NOTE\n"):])
    # The writer is asked for nothing its tool cannot carry: no categories, mood or project match.
    notes = tail[tail.index("\nIMPACT NOTE\n"):tail.index("\nPROJECT MATCH\n")]
    assert analysis.SYSTEM_PROMPT.endswith(notes)
    assert not [s for s in ("FIXED TAGS", "\nMOOD\n", "PROJECT MATCH") if s in analysis.SYSTEM_PROMPT]
    assert "AUTO TAGS" not in analysis.SYSTEM_PROMPT + analysis.FALLBACK_PROMPT  # replaced by TAGS
    with pytest.raises(RuntimeError, match="_SHARED_TAIL"):
        analysis._copied_sections(tail.replace("\nACKNOWLEDGED BY\n", "\nRECOGNITION\n"))


# ---------------------------------------------------------------------------
# apply_analysis and the context it is given
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("  FRONT-END refactor ", "p-mine"),  # folded match within this user
    ("Atlas", None),                      # only another user has an Atlas
])
def test_projects_are_matched_by_folded_name_and_never_created(conn, capture, add_user, add_project,
                                                               name, expected):
    add_user(OTHER)
    add_project("p-mine", "Front-End Refactor")
    add_project("p-theirs", "Atlas", user_id=OTHER)
    capture("e1", AUDIO)
    result = _label(FakeClient()) | {"project_name": name}
    with conn:
        analysis.apply_analysis(conn, DEV, "e1", SPOKEN, result)
    assert store.load_entry(conn, DEV, "e1")["project_id"] == expected
    assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 2


def test_user_context_is_this_users_projects_and_most_used_tags(conn, add_user, add_project, add_entry):
    add_user(OTHER)
    add_project("p1", "Front-End Refactor")
    add_project("p2", "Data Platform")
    add_project("p3", "Secret Plans", user_id=OTHER)
    add_entry("e1", tags=["shipped", "pairing"])
    add_entry("e2", tags=["pairing"])
    add_entry("e3", tags=["secret-tag"], user_id=OTHER)
    assert analysis.user_context(conn, DEV) == (["Data Platform", "Front-End Refactor"], ["pairing", "shipped"])


# ---------------------------------------------------------------------------
# run_capture_job
# ---------------------------------------------------------------------------

def test_a_capture_job_completes_with_an_entry_the_contract_accepts(conn, db_path, audio_dir, capture,
                                                                    add_project):
    add_project("p-fer", "Front-End Refactor")
    job_id = capture("e1", AUDIO)
    analysis.run_capture_job(db_path, job_id, client=FakeClient(), transcode=fake_transcode, audio_dir=audio_dir)

    job = _job(conn, job_id)
    assert (job["state"], job["failure_code"], job["attempts"]) == ("complete", None, 1)
    assert job["started_at"] <= job["finished_at"]
    entry = store.load_entry(conn, DEV, "e1")
    contract.validate("entry", entry)
    assert entry["analysis_state"] == "complete" and entry["transcript"] == SPOKEN
    assert entry["word_count"] == len(SPOKEN.split())
    assert (entry["project_id"], entry["project"]) == ("p-fer", "Front-End Refactor")
    assert "front-end-refactor" not in entry["tags"] and entry["retryable_until"] is not None
    # The classification is stored for reports and the eval, and nowhere on the wire.
    stored = conn.execute("SELECT categories, category_scores, classified_by FROM entries WHERE id = 'e1'").fetchone()
    assert json.loads(stored["categories"]) == ["wins", "collaboration"] and stored["classified_by"] == "jev"
    assert json.loads(stored["category_scores"])["wins"] >= 0.5


def test_an_18_minute_catch_up_goes_to_speech_to_text_well_inside_the_request_cap(conn, db_path, audio_dir,
                                                                                   capture, tmp_path):
    # §5 designs catch-ups up to 18 minutes. Noise is the hardest case for the encoder.
    wav = tmp_path / "long.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=r=8000:d=1080",
                    "-ac", "1", str(wav)], check=True)
    client = FakeClient()
    analysis.run_capture_job(db_path, capture("e1", wav.read_bytes()), client=client, transcode=audio.to_m4a_16k,
                             audio_dir=audio_dir)

    (sent,) = [kw for method, kw in client.calls if method == "transcribe"]
    assert sent["fmt"] == "m4a"
    assert len(base64.b64encode(sent["audio"])) < MAX_UPLOAD_BYTES  # ~5.8 MB; as 16-bit WAV it was ~46 MB
    assert store.load_entry(conn, DEV, "e1")["analysis_state"] == "complete"


def _undecodable(data):
    raise AudioUnreadable("ffmpeg could not decode the recording")


@pytest.mark.parametrize("case, code", [
    ({"audio": TEXT_MARKER + b"   "}, "transcription_failed"),                          # the model heard silence
    ({"transcode": _undecodable}, "audio_unreadable"),                                 # ffmpeg could not read it
    ({"purged": True}, "audio_unreadable"),                                            # the stored audio is gone
    ({"overrides": {"label_entry": {"summary": " "}}}, "model_refused"),                # transcribed, label unusable
    ({"fail_with": RuntimeError("OPENROUTER_API_KEY is not set")}, "model_unavailable"),  # anything unexpected
], ids=["silence", "undecodable", "purged", "bad-label", "crash"])
def test_each_failure_fails_the_job_and_its_entry_with_the_same_code(conn, db_path, audio_dir, capture,
                                                                     case, code):
    job_id = capture("e1", case.get("audio", AUDIO))
    if case.get("purged"):
        with conn:
            conn.execute("UPDATE audio_objects SET purged_at = ?", (store.now(),))
    client = FakeClient(fail_with=case.get("fail_with"), overrides=case.get("overrides"))
    analysis.run_capture_job(db_path, job_id, client=client, transcode=case.get("transcode", fake_transcode),
                             audio_dir=audio_dir)

    job = _job(conn, job_id)
    assert (job["state"], job["failure_code"]) == ("failed", code) and job["finished_at"]
    entry = store.load_entry(conn, DEV, "e1")
    contract.validate("entry", entry)
    assert (entry["analysis_state"], entry["analysis_failure_code"]) == ("failed", code)
    # Words heard before the failure are kept for a retry; nothing else is written.
    assert entry["transcript"] == (SPOKEN if code == "model_refused" else None)
    assert entry["word_count"] == len((entry["transcript"] or "").split())
    assert entry["summary"] is None and entry["tags"] == []


def test_a_finished_job_is_left_alone(conn, db_path, audio_dir, capture):
    job_id = capture("e1", AUDIO)
    client = FakeClient()
    analysis.run_capture_job(db_path, job_id, client=client, transcode=fake_transcode, audio_dir=audio_dir)
    before = (dict(_job(conn, job_id)), len(client.calls))
    analysis.run_capture_job(db_path, job_id, client=client, transcode=fake_transcode, audio_dir=audio_dir)
    assert (dict(_job(conn, job_id)), len(client.calls)) == before


def test_a_resumed_job_analyses_the_saved_transcript_instead_of_transcribing_again(conn, db_path, audio_dir,
                                                                                   capture):
    job_id = capture("e1", AUDIO)
    saved = "Paired with Dana on the flaky tests before the crash."
    with conn:  # the state a crash mid-analysis leaves behind
        conn.execute("UPDATE capture_jobs SET state = 'analyzing', started_at = ? WHERE id = ?", (store.now(), job_id))
        conn.execute("UPDATE entries SET analysis_state = 'analyzing', raw_text = ? WHERE id = 'e1'", (saved,))
    client = FakeClient()
    analysis.run_capture_job(db_path, job_id, client=client, transcode=fake_transcode, audio_dir=audio_dir)
    assert sorted(method for method, _ in client.calls) == ["decide", "tool_call"]
    entry = store.load_entry(conn, DEV, "e1")
    assert (entry["analysis_state"], entry["transcript"]) == ("complete", saved)


@pytest.mark.parametrize("deleted_during, calls", [
    ("transcribe", ["transcribe"]),                         # nothing more is asked about a notch that is gone
    ("tool_call", ["decide", "tool_call", "transcribe"]),   # the analysis in flight ends, and nothing is written
])
def test_a_notch_deleted_mid_job_stops_it_quietly(conn, db_path, audio_dir, capture, caplog, deleted_during, calls):
    job_id = capture("e1", AUDIO)

    class Deleting(FakeClient):
        def _record(self, method, **kwargs):
            super()._record(method, **kwargs)
            if method == deleted_during:
                with conn:  # DELETE /v1/entries/e1 lands mid-call; its job and audio rows go with it
                    conn.execute("DELETE FROM entries WHERE id = 'e1'")

    client = Deleting()
    analysis.run_capture_job(db_path, job_id, client=client, transcode=fake_transcode, audio_dir=audio_dir)
    assert sorted(method for method, _ in client.calls) == calls
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]  # a delete is not a crash
