-- schema.sql — notch-ios-dev §3 (docs/data-and-backend-integration.md), ported to SQLite.
--
-- Same tables, column names, NOT NULLs, CHECKs and defaults as the Postgres DDL, so a
-- row here is a row there. The substitutions, applied everywhere:
--   uuid, timestamptz, date  -> TEXT  (instants 'YYYY-MM-DDTHH:MM:SSZ' UTC, dates 'YYYY-MM-DD')
--   text[], smallint[]       -> TEXT NOT NULL DEFAULT '[]'  (a JSON array)
--   jsonb                    -> TEXT  (JSON)
--   boolean                  -> INTEGER CHECK (x IN (0, 1))
--   now()                    -> strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
--   CHECK (tags = normalize_tags(tags)) -> CHECK (tags_normalized(tags)), a Python function
--     store.connect() registers on every connection; a write that is not already
--     normalised is rejected, never silently rewritten (§3.4).
-- STRICT makes SQLite enforce column types the way Postgres would.
-- Every statement is IF NOT EXISTS: store.init_db() applies this file on every start.
--
-- Not ported: `deletions` and `device_tokens` (no delta sync or push yet), `search_vector`
-- and the GIN indexes (no search in scope), and the `reminder_weekdays <@ 0..6` CHECK
-- (SQLite cannot put a subquery in a CHECK).

CREATE TABLE IF NOT EXISTS users (
  id                     TEXT    NOT NULL PRIMARY KEY,
  display_name           TEXT,
  role                   TEXT,
  industry               TEXT,                         -- register A1, decided Sep 1
  years_experience       TEXT,                         -- register A1
  time_zone              TEXT    NOT NULL DEFAULT 'UTC',
  weekly_goal            INTEGER NOT NULL DEFAULT 5 CHECK (weekly_goal IN (0,2,3,4,5,6,7)),
  reminder_enabled       INTEGER NOT NULL DEFAULT 0 CHECK (reminder_enabled IN (0,1)),  -- off until onboarding turns it on, as in the app
  reminder_hour          INTEGER NOT NULL DEFAULT 20 CHECK (reminder_hour BETWEEN 0 AND 23),
  reminder_minute        INTEGER NOT NULL DEFAULT 30 CHECK (reminder_minute BETWEEN 0 AND 59),
  reminder_weekdays      TEXT    NOT NULL DEFAULT '[1,2,3,4,5]',   -- Sunday-indexed 0..6
  notify_week_recap      INTEGER NOT NULL DEFAULT 1 CHECK (notify_week_recap IN (0,1)),
  notify_report_finished INTEGER NOT NULL DEFAULT 1 CHECK (notify_report_finished IN (0,1)),
  created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS projects (
  id         TEXT NOT NULL PRIMARY KEY,                -- client-minted
  user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  name       TEXT NOT NULL CHECK (trim(name) <> ''),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (id, user_id)                                 -- target of entries' composite FK
) STRICT;

-- Folded uniqueness per owner. SQLite's lower() folds ASCII only; Postgres folds Unicode.
CREATE UNIQUE INDEX IF NOT EXISTS projects_owner_folded_name ON projects (user_id, lower(name));
CREATE INDEX IF NOT EXISTS projects_user_id ON projects (user_id);

CREATE TABLE IF NOT EXISTS entries (
  id                    TEXT    NOT NULL PRIMARY KEY,  -- client-minted
  user_id               TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,

  -- Recording facts. Client-owned; the server never recomputes them.
  recorded_at           TEXT    NOT NULL,
  duration_seconds      REAL    NOT NULL DEFAULT 0 CHECK (duration_seconds >= 0),
  capture_mode          TEXT    NOT NULL DEFAULT 'daily' CHECK (capture_mode IN ('daily','catch_up')),
  span_start            TEXT,
  span_end              TEXT,

  -- Text. raw_text is server-written (E4); corrected_text is the user's edit.
  raw_text              TEXT,
  corrected_text        TEXT,
  word_count            INTEGER NOT NULL DEFAULT 0 CHECK (word_count >= 0),

  -- Narrative (E2). Nullable because the row predates the analysis.
  summary               TEXT,
  takeaways             TEXT    NOT NULL DEFAULT '[]',
  mood                  TEXT    CHECK (mood IN ('up','flat','down')),

  -- Classification. `categories` (the five report categories), `category_scores` (Jev's
  -- probability per category, JSON; NULL when the chat model classified) and
  -- `classified_by` are internal and never on the wire.
  tags                  TEXT    NOT NULL DEFAULT '[]' CHECK (tags_normalized(tags)),
  categories            TEXT    NOT NULL DEFAULT '[]',
  category_scores       TEXT,
  classified_by         TEXT    CHECK (classified_by IN ('jev','llm')),
  project_id            TEXT,
  is_milestone          INTEGER NOT NULL DEFAULT 0 CHECK (is_milestone IN (0,1)),

  -- E5. Report input only.
  acknowledged_by       TEXT,
  impact_note           TEXT,

  -- A projection of capture_jobs.state (§3.5's map), written in the same transaction.
  analysis_state        TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (analysis_state IN ('pending','transcribing','analyzing','complete','failed')),
  analysis_failure_code TEXT,

  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  -- The cross-user guard as a foreign key (§3.9). No delete action: SQLite cannot
  -- SET NULL (project_id) alone, and deleting a project is out of scope.
  FOREIGN KEY (project_id, user_id) REFERENCES projects (id, user_id),
  UNIQUE (id, user_id),

  CHECK ((capture_mode = 'daily'    AND span_start IS NULL AND span_end IS NULL)
      OR (capture_mode = 'catch_up' AND span_start IS NOT NULL AND span_end >= span_start)),
  CHECK ((analysis_state = 'failed') = (analysis_failure_code IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS entries_owner_recorded_at ON entries (user_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS entries_project_id ON entries (project_id) WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS capture_jobs (
  id           TEXT    NOT NULL PRIMARY KEY,           -- server-minted uuid4
  user_id      TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  entry_id     TEXT    NOT NULL UNIQUE,                -- THE idempotency mechanism (§3.5)
  state        TEXT    NOT NULL DEFAULT 'queued'
                       CHECK (state IN ('queued','transcribing','analyzing','complete','failed')),
  failure_code TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  submitted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  started_at   TEXT,
  finished_at  TEXT,
  updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  FOREIGN KEY (entry_id, user_id) REFERENCES entries (id, user_id) ON DELETE CASCADE,
  UNIQUE (id, user_id),                                -- target for audio_objects' FK

  CHECK ((state = 'failed') = (failure_code IS NOT NULL)),
  CHECK (finished_at IS NULL OR started_at IS NOT NULL)
) STRICT;

CREATE INDEX IF NOT EXISTS capture_jobs_pending
  ON capture_jobs (state, submitted_at) WHERE state IN ('queued','transcribing','analyzing');

CREATE TABLE IF NOT EXISTS audio_objects (
  id               TEXT    NOT NULL PRIMARY KEY,       -- server-minted uuid4
  user_id          TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  capture_job_id   TEXT    NOT NULL,
  entry_id         TEXT    NOT NULL,
  bucket           TEXT    NOT NULL DEFAULT 'capture-audio',
  storage_key      TEXT    NOT NULL UNIQUE,
  segment_ordinal  INTEGER NOT NULL DEFAULT 0 CHECK (segment_ordinal >= 0),
  content_type     TEXT    NOT NULL DEFAULT 'audio/mp4',
  byte_size        INTEGER NOT NULL CHECK (byte_size > 0),
  duration_seconds REAL    CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
  uploaded_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  -- E3: a flat seven days from upload, whatever the outcome. The default only satisfies
  -- NOT NULL; the trigger below sets the real value from uploaded_at, never the worker.
  purge_after      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now','+7 days')),
  purged_at        TEXT,

  FOREIGN KEY (capture_job_id, user_id) REFERENCES capture_jobs (id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (entry_id, user_id) REFERENCES entries (id, user_id) ON DELETE CASCADE,
  UNIQUE (capture_job_id, segment_ordinal),

  -- '<user_id>/<entry_id>/<NNN>' — derivable from the row, no file extension.
  CHECK (storage_key = user_id || '/' || entry_id || '/' || printf('%03d', segment_ordinal))
) STRICT;

CREATE TRIGGER IF NOT EXISTS audio_objects_purge_after_ai
  AFTER INSERT ON audio_objects FOR EACH ROW
BEGIN
  UPDATE audio_objects
     SET purge_after = strftime('%Y-%m-%dT%H:%M:%SZ', NEW.uploaded_at, '+7 days')
   WHERE id = NEW.id;
END;

CREATE INDEX IF NOT EXISTS audio_objects_sweep ON audio_objects (purge_after) WHERE purged_at IS NULL;

CREATE TABLE IF NOT EXISTS reports (
  id                   TEXT    NOT NULL PRIMARY KEY,   -- client-minted
  user_id              TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  type                 TEXT    NOT NULL DEFAULT 'month'
                               CHECK (type IN ('week','month','quarter','year','custom')),
  -- Dates, inclusive at both ends, so a one-day range has start = end (Postgres: end > start
  -- over instants).
  range_start          TEXT    NOT NULL,
  range_end            TEXT    NOT NULL CHECK (range_end >= range_start),
  project_id           TEXT,                           -- request scope; frozen, no FK
  tag                  TEXT,                           -- request scope
  generated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  -- Two different titles. Do not merge them.
  range_label          TEXT    NOT NULL,               -- "May 2026"
  headline             TEXT,                           -- the model's sentence
  eyebrow              TEXT,                           -- "Monthly report · May 2026"
  lede                 TEXT,
  body                 TEXT,
  themes               TEXT    NOT NULL DEFAULT '[]' CHECK (tags_normalized(themes)),

  -- Provenance and numbers, frozen at acceptance. Deliberately no FK.
  source_entry_ids     TEXT    NOT NULL DEFAULT '[]',
  notch_count          INTEGER NOT NULL DEFAULT 0,
  project_count        INTEGER NOT NULL DEFAULT 0,
  milestone_count      INTEGER NOT NULL DEFAULT 0,
  project_breakdown    TEXT,                           -- [{name, notch_count, share}]
  momentum             TEXT    NOT NULL DEFAULT '[]',  -- [{date, count}], the wire object
  momentum_granularity TEXT    NOT NULL DEFAULT 'day' CHECK (momentum_granularity IN ('day','week','month')),

  created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (id, user_id)
) STRICT;

CREATE INDEX IF NOT EXISTS reports_owner_generated_at ON reports (user_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS report_highlights (
  id               TEXT    NOT NULL PRIMARY KEY,       -- server-minted uuid4
  report_id        TEXT    NOT NULL,
  user_id          TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  ordinal          INTEGER NOT NULL CHECK (ordinal >= 0),
  title            TEXT    NOT NULL,
  detail           TEXT    NOT NULL,
  kind             TEXT    NOT NULL DEFAULT 'note'
                           CHECK (kind IN ('milestone','shipped','collaboration','note')),
  source_entry_ids TEXT    NOT NULL DEFAULT '[]',      -- deliberately no FK

  FOREIGN KEY (report_id, user_id) REFERENCES reports (id, user_id) ON DELETE CASCADE,
  UNIQUE (report_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS report_jobs (
  id           TEXT    NOT NULL PRIMARY KEY,           -- server-minted uuid4
  user_id      TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  report_id    TEXT    NOT NULL UNIQUE,
  state        TEXT    NOT NULL DEFAULT 'queued'
                       CHECK (state IN ('queued','counting','writing','complete','failed')),
  failure_code TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  submitted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  finished_at  TEXT,
  updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  FOREIGN KEY (report_id, user_id) REFERENCES reports (id, user_id) ON DELETE CASCADE,

  CHECK ((state = 'failed') = (failure_code IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS report_jobs_pending
  ON report_jobs (state, submitted_at) WHERE state IN ('queued','counting','writing');
