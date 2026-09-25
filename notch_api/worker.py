"""
worker.py — where capture and report jobs run, off the request that queued them.

A POST answers 202 as soon as its rows are committed; the model work happens here,
on a small thread pool, and GET /v1/jobs reads the outcome from the job row. So a
job's result never travels back through this module: run_capture_job and
run_report_job write their own terminal state, and a job that dies with the
process is still 'queued' or mid-flight in the database, where resume_pending()
finds it on the next start.

inline=True runs each job in the caller instead, so a test's POST returns with the
job already finished.

The model client is built lazily (LazyClient): the server boots, serves reads and
queues work without OPENROUTER_API_KEY, and a job that then needs a model fails
`model_unavailable` like any other outage, instead of the process refusing to start.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from . import analysis, metrics, reports, store
from .openrouter import ModelUnavailable, OpenRouterClient

log = logging.getLogger(__name__)


class LazyClient:
    """OpenRouterClient.from_env() on first use; a missing key is ModelUnavailable, re-checked each call."""

    def __init__(self):
        self._client = None

    def __getattr__(self, name):
        if self._client is None:
            try:
                self._client = OpenRouterClient.from_env()
            except RuntimeError as exc:
                raise ModelUnavailable(str(exc)) from None
        return getattr(self._client, name)


class _JobClient:
    """
    The model client, tagging each call with its job for metrics.py. The tag is set around
    the call itself, so it holds on any thread this object is handed to (analysis.py asks
    Jev on a pool of its own) and never outlives the call on a reused worker thread.
    """

    def __init__(self, client, tags):
        self._client, self._tags = client, tags

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def tagged(*args, **kwargs):
            token = metrics.job.set(self._tags)
            try:
                return attr(*args, **kwargs)
            finally:
                metrics.job.reset(token)
        return tagged


class JobRunner:
    def __init__(self, db_path, *, client, transcode, audio_dir, workers=4, inline=False):
        self.db_path, self.client, self.transcode, self.audio_dir = db_path, client, transcode, audio_dir
        self._pool = None if inline else ThreadPoolExecutor(workers, thread_name_prefix="notch-job")

    def submit_capture(self, job_id):
        client = _JobClient(self.client, {"job": "capture", "job_id": job_id})
        self._run(analysis.run_capture_job, job_id, client=client, transcode=self.transcode, audio_dir=self.audio_dir)

    def submit_report(self, job_id):
        self._run(reports.run_report_job, job_id, client=_JobClient(self.client, {"job": "report", "job_id": job_id}))

    def resume_pending(self):
        """Re-submit every job a previous process left unfinished, oldest first. Returns how many."""
        conn = store.connect(self.db_path)
        try:
            capture = [r[0] for r in conn.execute(
                "SELECT id FROM capture_jobs WHERE state IN ('queued','transcribing','analyzing') "
                "ORDER BY submitted_at")]
            report = [r[0] for r in conn.execute(
                "SELECT id FROM report_jobs WHERE state IN ('queued','counting','writing') ORDER BY submitted_at")]
        finally:
            conn.close()
        for job_id in capture:
            self.submit_capture(job_id)
        for job_id in report:
            self.submit_report(job_id)
        return len(capture) + len(report)

    def shutdown(self):
        """Let running jobs finish their writes; drop queued ones (they are resumed on the next start)."""
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def _run(self, job, job_id, **kwargs):
        def run():
            # The jobs record model failures themselves; this only fires if even that write failed.
            try:
                job(self.db_path, job_id, **kwargs)
            except Exception:
                log.exception("job %s could not record its outcome", job_id)

        if self._pool is None:
            run()
        else:
            self._pool.submit(run)
