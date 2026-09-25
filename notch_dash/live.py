"""
live.py — keeping sources current off the request path. A Tail follows one log file; a
Poller calls one probe on an interval. Each keeps what it last read and why the last read
failed, worded for the page. `run` drives either on a daemon thread; tests call .tick() and
.refresh() themselves.
"""

import os
import threading
import time
from collections import defaultdict, deque

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
    skips it). It keeps the file open between ticks; when the name points at a new file (a
    roll) it finishes the old one first, and when the file gets shorter it starts again from
    byte 0. It keeps the last 24 h of items: at most `maxlen` of each `part(item)`, so a flood
    of one kind never pushes out another.
    """

    def __init__(self, path, parse, *, interval, maxlen=None, part=lambda item: None, clock=time.time):
        self.path, self.parse, self.part, self.interval, self.clock = path, parse, part, interval, clock
        self._parts, self._lock = defaultdict(lambda: deque(maxlen=maxlen)), threading.Lock()
        self._file, self._rest = None, b""
        self.read_at = self.error = None

    def _read(self):
        """(the rolled file's last bytes or None, what the current file gained since the last read)"""
        st, rolled = os.stat(self.path), None
        if self._file and os.fstat(self._file.fileno()).st_ino != st.st_ino:
            rolled = self._file.read(CHUNK)
            self._file.close()
            self._file = None
        if self._file is None:
            self._file = open(self.path, "rb")
        elif st.st_size < self._file.tell():  # truncated in place
            self._file.seek(0)
            self._rest = b""
        return rolled, self._file.read(CHUNK)

    def tick(self):
        try:
            rolled, data = self._read()
            lines = []
            if rolled is not None:  # its whole lines; a partial last one has no end to wait for
                *lines, _ = (self._rest + rolled).split(b"\n")
                self._rest = b""
            *more, self._rest = (self._rest + data).split(b"\n")
            lines, self._rest = lines + more, self._rest[-CHUNK:]
            now = self.clock()
            with self._lock:
                for line in lines:
                    item = self.parse(line.decode("utf-8", "replace").rstrip("\r"), now)
                    if item is not None:
                        self._parts[self.part(item)].append(item)
                for items in self._parts.values():
                    while items and items[0].at < now - DAY:
                        items.popleft()
        except Exception as exc:
            self.error = describe(exc)
        else:
            self.read_at, self.error = now, None

    def items(self):
        with self._lock:
            return [item for items in self._parts.values() for item in items]


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
