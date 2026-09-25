"""
live.py — keeping sources current off the request path. A Tail follows one log file; a
Poller calls one probe on an interval. Each keeps what it last read and why the last read
failed, worded for the page. `run` drives either on a daemon thread; tests call .tick() and
.refresh() themselves.
"""

import os
import threading
import time
from collections import deque

DAY = 86400
CHUNK = 8 << 20  # the most one tick reads, so a big backlog is taken in steps


class Unreadable(Exception):
    """A read that failed, in words fit for the page: never an exception's own text, which can hold a URL."""


def describe(exc):
    if isinstance(exc, Unreadable):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "it isn’t there"
    if isinstance(exc, PermissionError):
        return "not allowed to read it"
    return "it couldn’t be read" if isinstance(exc, OSError) else type(exc).__name__


class Tail:
    """
    Follows `path`, turning each complete line into an item with `parse(line, now)` (None
    skips it). It remembers its offset and inode, starts again from byte 0 when the file is
    replaced or gets shorter, and keeps the last 24 h of items (at most `maxlen`).
    """

    def __init__(self, path, parse, *, interval, maxlen=None, clock=time.time):
        self.path, self.parse, self.interval, self.clock = path, parse, interval, clock
        self._items, self._lock = deque(maxlen=maxlen), threading.Lock()
        self._inode, self._offset, self._rest = None, 0, b""
        self.read_at = self.error = None

    def tick(self):
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_ino != self._inode or st.st_size < self._offset:
                    self._inode, self._offset, self._rest = st.st_ino, 0, b""
                f.seek(self._offset)
                data = f.read(CHUNK)
            self._offset += len(data)
            *lines, self._rest = (self._rest + data).split(b"\n")
            self._rest = self._rest[-CHUNK:]
            now = self.clock()
            with self._lock:
                for line in lines:
                    item = self.parse(line.decode("utf-8", "replace").rstrip("\r"), now)
                    if item is not None:
                        self._items.append(item)
                while self._items and self._items[0].at < now - DAY:
                    self._items.popleft()
        except Exception as exc:
            self.error = describe(exc)
        else:
            self.read_at, self.error = now, None

    def items(self):
        with self._lock:
            return list(self._items)


class Poller:
    """Calls `fn` on refresh(); a failure keeps the last good value and records why."""

    def __init__(self, fn, *, interval, max_age, watched_only=False, clock=time.time):
        self.fn, self.interval, self.max_age, self.watched_only = fn, interval, max_age, watched_only
        self.clock = clock
        self.value = self.read_at = self.error = None

    def refresh(self):
        try:
            value = self.fn()
        except Exception as exc:
            self.error = describe(exc)
        else:
            self.value, self.read_at, self.error = value, self.clock(), None

    def fresh(self, now):
        return self.read_at is not None and now - self.read_at <= self.max_age

    def current(self, now):
        """The last good value while it is younger than max_age, else None."""
        return self.value if self.fresh(now) else None


def run(step, interval, stop, watching=None):
    """Call `step` every `interval` seconds until `stop` is set; with `watching`, only while it says so."""
    due = 0.0
    while not stop.is_set():
        if time.monotonic() >= due and (watching is None or watching()):
            due = time.monotonic() + interval
            step()
        stop.wait(1.0)
