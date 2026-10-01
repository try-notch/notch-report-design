"""
refusals.py — the answers the meter never saw, counted for the dashboard.

A processing call that reaches check-and-start leaves a `usage_events` row whatever
happens to it. One refused earlier leaves nothing: no token (401), an app older than
`min_app_version` (426), a feature switched off (503), a body too large (413), audio that
is not audio (415, 422), a path that does not exist (404). So a server turning every
phone away looked, on the dashboard, exactly like a quiet one.

privacy.Guard hands RefusalCounter.note() each finished request's log entry. A non-2xx
answer that no usage row covers (`metered` is unset: v2.V2.run sets it) is counted under
(the UTC hour, the route's template, the status, the code). No user, no path, no header:
the route is the template the router matched or 'unmatched', and the code is checked to
be a code. Anything else is counted as 'other'.

COUNTED IN MEMORY, WRITTEN IN BATCHES. note() only adds to a dict, so it costs a refused
request nothing and cannot fail it; a thread writes the dict to the meter every
`interval` seconds in one transaction (Meter.count_refusals), and once more at shutdown.
Writing per request would hand anyone without a token one SQLite commit per request,
against the same write lock every real call's metering needs. A write that fails keeps
its counts for the next one; a process killed outright loses at most `interval` seconds.
"""

import logging
import re
import threading
import time

from .wire_v2 import Refusal

log = logging.getLogger(__name__)

INTERVAL = 5.0
MAX_KEYS = 2000      # distinct (hour, route, status, code) held between writes; the rest count as 'other'
HOUR = 3600
_ROUTE = re.compile(r"/[A-Za-z0-9_/{}.:-]{0,80}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,40}")


class RefusalCounter:
    def __init__(self, meter, *, interval=INTERVAL, clock=time.time):
        self.meter, self.interval, self.clock = meter, interval, clock
        self._counts, self._lock = {}, threading.Lock()
        self._stop, self._thread = threading.Event(), None

    def note(self, entry):
        """One finished request (privacy.Guard's entry). Never raises: a counter must not change an answer."""
        try:
            status = entry.get("status")
            if type(status) is not int or status < 400 or entry.get("metered"):
                return
            route, code = entry.get("route"), entry.get("error_code")
            route = route if isinstance(route, str) and _ROUTE.fullmatch(route) else "unmatched"
            code = code if isinstance(code, str) and _CODE.fullmatch(code) else "other"
            now = self.clock()
            key = (int(now // HOUR) * HOUR, route, status, code)
            with self._lock:
                if key not in self._counts and len(self._counts) >= MAX_KEYS:
                    key = (key[0], "unmatched", status, "other")
                calls, _ = self._counts.get(key, (0, now))
                self._counts[key] = (calls + 1, now)
        except Exception:  # noqa: BLE001
            log.error("refusal_not_counted")

    def flush(self):
        """Write what has been counted since the last write; on failure, keep it for the next."""
        with self._lock:
            counts, self._counts = self._counts, {}
        try:
            self.meter.count_refusals(counts)
        except (Refusal, OSError):
            with self._lock:
                for key, (calls, last_at) in counts.items():
                    held, at = self._counts.get(key, (0, last_at))
                    self._counts[key] = (held + calls, max(at, last_at))
            log.error("refusals_not_written")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="notch-refusals", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 — whatever went wrong, the next write still gets its turn
                log.error("refusals_not_written")

    def stop(self):
        """Stop the thread and write whatever is left."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.flush()
