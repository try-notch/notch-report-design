"""
zdr.py — speech-to-text and Jev take no per-request routing, so after each such call the
server checks who served it against OpenRouter's zero-data-retention list.

Chat calls need no audit: every one carries `provider: {zdr: true, data_collection:
"deny"}` and OpenRouter will not route it anywhere else. Transcription and the Jev
decisions endpoint ignore that block, so after a call settles, for each STT and Jev
reply in it, the auditor:
  1. looks the generation up (GET /api/v1/generation?id=), a few times over some
     seconds, since OpenRouter records a generation a moment after answering;
  2. checks the provider that served it, for that model, against the ZDR endpoint list
     (GET /api/v1/endpoints/zdr, cached for `zdr.list_ttl_seconds`);
  3. records the verdict and the provider on the call's usage row: hit, miss or unknown.
On a MISS it turns that path off in remote config, by appending a new version:
`features.capture` off for speech-to-text, `classifier` back to "chat" for Jev; and it
logs an alert line. The call's own result has already been returned, because the
content was already sent. A provider it could not learn, or a list it could not fetch,
is `unknown` and an alert, but switches nothing off: an outage at OpenRouter's metadata
endpoints must not stop capture.

It runs on its own two threads after the response, so a lookup's wait never adds to a
call's latency; `inline=True` runs it in the caller, for tests.
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import privacy
from .openrouter import ModelError
from .remote_config import merge

log = logging.getLogger(__name__)

AUDITED = ("stt", "classify")
LOOKUP_WAITS = (0.5, 2.0, 5.0, 10.0)    # seconds before each generation lookup
_DATED = re.compile(r"-\d{8}$")


def _base(model):
    """A model id without its date suffix: typesafe/jev-1.13-20260917 -> typesafe/jev-1.13."""
    return _DATED.sub("", model.strip().lower()) if isinstance(model, str) else None


class ZdrAuditor:
    def __init__(self, directory, meter, remote, *, inline=False, waits=LOOKUP_WAITS, sleep=time.sleep,
                 clock=time.monotonic):
        self.directory, self.meter, self.remote = directory, meter, remote
        self.waits, self.sleep, self.clock = waits, sleep, clock
        self._pool = None if inline else ThreadPoolExecutor(2, thread_name_prefix="notch-zdr")
        self._lock = threading.Lock()
        self._listing, self._listed_at = None, None

    def submit(self, row_id, calls, models):
        """After a call settled: audit its STT and Jev replies. `models` is remote config's {stt, classifier}."""
        audited = [dict(c) for c in calls if c.get("kind") in AUDITED and c.get("generation_id")]
        if not audited:
            return
        if self._pool is None:
            self._audit(row_id, audited, models)
        else:
            self._pool.submit(self._guarded, row_id, audited, models)

    def shutdown(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _guarded(self, row_id, calls, models):
        try:
            self._audit(row_id, calls, models)
        except Exception:  # noqa: BLE001 — an audit that crashes must not take its thread down with it
            log.exception("zdr_audit_crashed")

    def _audit(self, row_id, calls, models):
        verdicts, providers = [], []
        for call in calls:
            served = self._generation(call["generation_id"])
            provider = served.get("provider") if served else None
            model = (served or {}).get("model") or call.get("model") or models.get(
                "stt" if call["kind"] == "stt" else "classifier")
            providers.append(provider)
            listed = self._listed(provider, model) if provider else None
            verdict = "hit" if listed else "miss" if listed is False else "unknown"
            verdicts.append(verdict)
            if verdict == "miss":
                self._switch_off(call["kind"], provider, model)
            elif verdict == "unknown":
                privacy.alert("zdr_unverified", kind=call["kind"], model=model, provider=provider)
        overall = "miss" if "miss" in verdicts else "unknown" if "unknown" in verdicts else "hit"
        self.meter.record_zdr(row_id, overall, providers)

    def _generation(self, generation_id):
        for wait in self.waits:
            self.sleep(wait)
            try:
                served = self.directory.generation(generation_id)
            except ModelError:
                served = None
            if served and served.get("provider"):
                return served
        return None

    def _listed(self, provider, model):
        """True if OpenRouter lists `provider` as a zero-retention endpoint for `model`; None if it cannot say."""
        listing = self._zdr_list()
        if listing is None:
            return None
        name, base = provider.strip().lower(), _base(model)
        for endpoint in listing:
            if endpoint["provider"].strip().lower() != name:
                continue
            if endpoint["model"] is None or base is None or _base(endpoint["model"]) == base:
                return True
        return False

    def _zdr_list(self):
        ttl = self.remote.current()["zdr"]["list_ttl_seconds"]
        with self._lock:
            if self._listing is not None and self.clock() - self._listed_at < ttl:
                return self._listing
        try:
            listing = self.directory.zdr_endpoints()
        except ModelError:
            return None
        with self._lock:
            self._listing, self._listed_at = listing, self.clock()
        return listing

    def _switch_off(self, kind, provider, model):
        """Append a config version with this path off, unless it already is; alert either way."""
        with self._lock:
            current = self.remote.current()
            if kind == "stt":
                change, already = {"features": {"capture": False}}, not current["features"]["capture"]
            else:
                change, already = {"classifier": "chat"}, current["classifier"] == "chat"
            version = None
            if not already:
                version = self.remote.push(merge(current.overrides, change), note=f"zdr miss: {kind}",
                                           created_by="zdr-audit")
        privacy.alert("zdr_miss", kind=kind, model=model, provider=provider, config_version=version)
