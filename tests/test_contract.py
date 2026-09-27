"""
contract.py: the schemas accept what the iOS doc says the server sends, and reject
the mistakes that would otherwise surface as a decode failure on a phone.
"""

import pytest

from notch_api.contract import ContractError, validate

# §5's GET /v1/entries/{id} example, verbatim apart from the elided ids.
DOC_ENTRY = {
    "id": "0F3B7A21-6B0D-4E11-9A73-0C5E8D2B7F41",
    "recorded_at": "2026-05-11T08:42:00Z",
    "duration_seconds": 180,
    "word_count": 412,
    "summary": "The part you'd been dreading went smoothly.",
    "transcript": "Shipped the Atlas demo to leadership this morning…",
    "takeaways": ["Atlas demo landed with leadership", "Priya unblocked the migration"],
    "tags": ["shipped", "pairing"],
    "project_id": "8C2A",
    "project": "project-atlas",
    "mood": "up",
    "is_milestone": True,
    "acknowledged_by": "Priya",
    "impact_note": "Green-lit the Q3 rollout",
    "mode": "catch_up",
    "catch_up_span": {"start": "2026-05-11", "end": "2026-05-17"},
    "analysis_state": "complete",
    "analysis_failure_code": None,
    "retryable_until": "2026-05-18T08:42:00Z",
    "updated_at": "2026-05-11T09:02:00Z",
}

REPORT = {
    "id": "R7K2", "type": "month", "range_start": "2026-05-01", "range_end": "2026-05-31",
    "range_label": "May 2026", "headline": "Shipping through the fear",
    "eyebrow": "Monthly report · May 2026", "lede": "May was the month the dreaded things turned out fine.",
    "body": "Four projects…", "generated_at": "2026-06-01T07:14:00Z",
    "counts": {"notches": 12, "projects": 4, "milestones": 1},
    "momentum": [{"date": "2026-05-01", "count": 3}],
    "momentum_granularity": "day",
    "project_breakdown": [{"name": "project-atlas", "notch_count": 6, "share": 50}],
    "highlights": [{"ordinal": 0, "title": "Atlas demo landed", "detail": "Leadership green-light",
                    "kind": "milestone", "source_entry_ids": ["0F3B"]}],
    "source_entry_ids": ["0F3B", "A914"],
    "themes": ["shipped", "win", "pairing"],
}


def test_the_docs_own_examples_validate():
    validate("entry", DOC_ENTRY)
    validate("report", REPORT)


@pytest.mark.parametrize("change, path", [
    ({"summary": None}, "summary"),                            # complete needs text to render
    ({"word_count": 0}, "word_count"),                         # complete needs words
    ({"mode": "daily"}, "catch_up_span"),                      # daily carries no span
    ({"catch_up_span": None}, "catch_up_span"),                # catch_up needs one
    ({"analysis_state": "failed"}, "analysis_failure_code"),   # failed names its code
    ({"categories": ["wins"]}, "<root>"),                      # internal, never on the wire
    ({"tags": ["#shipped"]}, "tags/0"),                        # tags travel bare
    ({"recorded_at": "2026-05-11T08:42:00+00:00"}, "recorded_at"),  # instants are Z
    ({"mode": "catchUp"}, "mode"),                             # the Swift raw value
])
def test_entry_rejects(change, path):
    with pytest.raises(ContractError, match=f"entry: {path}"):
        validate("entry", DOC_ENTRY | change)


def test_a_pending_entry_may_have_no_text_yet():
    pending = DOC_ENTRY | {"analysis_state": "pending", "summary": None, "transcript": None,
                           "mood": None, "word_count": 0, "takeaways": [], "tags": []}
    validate("entry", pending)


@pytest.mark.parametrize("job", [
    {"status": "processing", "entry_id": "e1", "poll_after_ms": 750},
    {"status": "processing", "report_id": "r1", "poll_after_ms": 750},
    {"status": "failed", "entry_id": "e1", "code": "transcription_failed",
     "message": "No speech detected.", "retryable_until": "2026-05-18T08:42:00Z"},
    {"status": "failed", "report_id": "r1", "code": "model_refused", "message": "x",
     "retryable_until": None},
    {"status": "complete", "entry_id": DOC_ENTRY["id"], "entry": DOC_ENTRY},
    {"status": "complete", "report_id": "r1"},
])
def test_job_accepts_each_status_shape(job):
    validate("job", job)


@pytest.mark.parametrize("job", [
    {"status": "processing", "entry_id": "e1", "report_id": "r1", "poll_after_ms": 750},
    {"status": "processing", "poll_after_ms": 750},
    {"status": "failed", "entry_id": "e1", "code": "boom", "message": "x", "retryable_until": None},
    {"status": "complete", "entry_id": "e1", "entry": DOC_ENTRY | {"summary": None}},
    {"status": "complete", "entry_id": "e1"},
    {"status": "queued", "entry_id": "e1", "poll_after_ms": 750},  # a DB state, not a wire status
])
def test_job_rejects(job):
    with pytest.raises(ContractError):
        validate("job", job)


@pytest.mark.parametrize("change", [
    {"momentum": {"granularity": "day", "start": "2026-05-01", "counts": [0, 3]}},  # §5 example shape
    {"highlights": [REPORT["highlights"][0] | {"entry_id": "0F3B"}]},              # scalar provenance
    {"themes": ["#shipped"]},
    {"counts": {"notches": 12, "projects": 4}},
    {"type": "last7days"},
])
def test_report_rejects(change):
    with pytest.raises(ContractError):
        validate("report", REPORT | change)


def test_small_objects():
    validate("entry_accepted", {"job_id": "j", "entry_id": "e"})
    validate("report_accepted", {"job_id": "j", "report_id": "r"})
    validate("project_list", {"projects": [{"id": "p", "name": "Atlas", "notch_count": 3, "share": 100}]})
    validate("report_list", {"reports": [{"id": "r", "range_label": "May 2026", "headline": None,
                                          "type": "month", "generated_at": "2026-06-01T07:14:00Z"}],
                             "next_cursor": None})
    validate("error", {"error": {"code": "unauthorized", "message": "x", "retryable": False}})
    with pytest.raises(ContractError):
        validate("error", {"error": {"code": "unauthorized", "message": "x"}})
    with pytest.raises(ContractError):
        validate("project", {"id": "p", "name": "Atlas", "notch_count": 3, "share": 101})
    with pytest.raises(ContractError):
        validate("entry_accepted", {"job_id": "j", "source_entry_ids": ["e"]})  # §5's malformed example


STATS = {"streak": 4, "total": 34, "record_total": 412, "branches": 4, "this_week": 4, "goal": 5,
         "days": [False] * 90 + [True]}
ME = {"id": "u1", "display_name": "Jordan Kim", "email": None, "role": "Software Engineer",
      "industry": None, "years_experience": "5",
      "settings": {"weekly_goal": 5, "reminder": {"enabled": True, "hour": 20, "minute": 30, "weekdays": [1, 2, 3, 4, 5]},
                   "notify_week_recap": True, "notify_report_finished": True, "time_zone": "Europe/London"}}


def test_the_record_and_account_kinds_accept_what_the_routes_send():
    pending = DOC_ENTRY | {"analysis_state": "pending", "summary": None, "transcript": None, "mood": None,
                           "word_count": 0, "takeaways": [], "tags": []}
    validate("entry_list", {"entries": [DOC_ENTRY, pending], "next_cursor": "eyJyIjoi", "matched": 2, "total": 2})
    validate("entry_list", {"entries": [], "next_cursor": None, "matched": 0, "total": 0})
    validate("takeaways", {"takeaways": ["A demo you dreaded went clean"], "tags": ["shipped"]})
    validate("stats", STATS)
    validate("me", ME)
    validate("deleted", {"deleted": True})


@pytest.mark.parametrize("kind, obj", [
    ("stats", STATS | {"days": [False] * 90}),                          # the widget grid is exactly 91
    ("stats", STATS | {"goal": 1}),                                     # 1 is not a goal
    ("me", ME | {"settings": ME["settings"] | {"reminder": ME["settings"]["reminder"] | {"weekdays": [7]}}}),
    ("me", ME | {"settings": ME["settings"] | {"reminder": ME["settings"]["reminder"] | {"time": "20:30"}}}),
    ("takeaways", {"takeaways": [], "tags": []}),                       # a rewrite never empties the draft
    ("takeaways", {"takeaways": ["x"], "tags": ["#shipped"]}),          # tags travel bare
    ("entry_list", {"entries": [], "next_cursor": None, "matched": 0}),  # matched and total are both sent
])
def test_the_record_and_account_kinds_reject(kind, obj):
    with pytest.raises(ContractError):
        validate(kind, obj)
