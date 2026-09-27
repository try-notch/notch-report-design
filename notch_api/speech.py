"""
speech.py — /v2/transcribe's audio work: measure the recording by decoding it, split a
long one at silence, and transcribe the pieces, a few at a time.

DURATION COMES FROM DECODING, never from the container header a client wrote. One ffmpeg
pass decodes the whole recording to nowhere (`-f null`) and reports how far it got
(`-progress`), finds the pauses on the way (`silencedetect`), and says what the stream
is, so a recording that is already 16 kHz mono AAC in MP4 (what the app records) is sent
as it is, with no second encode.

THE AUDIO IS A FILE, NOT A PIPE. MP4 keeps its index (the moov atom) wherever the
encoder put it, often at the end, so ffmpeg must seek, and a pipe cannot (audio.py says
the same). Every file lives in the request's own tempfile.TemporaryDirectory under
NOTCH_TMP, a RAM-backed tmpfs on the VPS, which the route removes in `finally`.

UNTRUSTED INPUT. ffmpeg reads the upload only through the demuxer its Content-Type names
(`-f`), and only from local files (`-protocol_whitelist file`), so an upload that is
really a playlist or a concat script cannot make ffmpeg open anything else.

LONG AUDIO. Past `split_over_seconds` (480) the recording is cut into
ceil(seconds / chunk_seconds) pieces of about `chunk_seconds` (300) each, every cut
moved to the middle of the nearest pause within a minute of where it would fall, and
the pieces are encoded and transcribed `parallel` (3) at a time, then joined in order.
A piece with no speech in it is only a quiet stretch; a recording with none anywhere is
TranscriptionFailed (422 no_speech).
"""

import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .audio import AudioUnreadable
from .openrouter import DeadlineExceeded, TranscriptionFailed

# Content-Type -> the ffmpeg demuxer allowed to read it.
DEMUXERS = {"audio/mp4": "mov", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/webm": "matroska",
            "audio/wav": "wav"}
SILENCE = "silencedetect=noise=-35dB:d=0.4"
CUT_WINDOW = 60.0            # seconds either side of a cut's target where a pause may move it
MIN_PIECE = 20.0             # never cut a sliver
FFMPEG_TIMEOUT = 90.0

_SILENCE_START = re.compile(r"silence_start: (-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end: (-?\d+(?:\.\d+)?)")
_OUT_TIME_US = re.compile(r"^out_time_(?:us|ms)=(\d+)$", re.M)
_INPUT_MP4 = re.compile(r"^Input #0, [^,]*mov,mp4", re.M)
_STREAM = re.compile(r"Stream #0:\d+.*?: Audio: (\w+).*?, (\d+) Hz, (\w+)")


@dataclass
class Probe:
    seconds: float                                   # decoded duration
    silences: list = field(default_factory=list)     # [(start, end)] seconds
    passthrough: bool = False                        # already 16 kHz mono AAC in MP4


def parse_probe(progress, log):
    """ffmpeg's -progress output and its stderr -> Probe. AudioUnreadable if it decoded nothing."""
    times = [int(m) for m in _OUT_TIME_US.findall(progress)]
    seconds = max(times) / 1_000_000 if times else 0.0
    if seconds <= 0:
        raise AudioUnreadable()
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    silences = [(max(0.0, s), e) for s, e in zip(starts, ends) if e > s]
    if len(starts) > len(ends):                      # a pause that runs to the end of the recording
        silences.append((max(0.0, starts[-1]), seconds))
    stream = _STREAM.search(log)
    passthrough = bool(_INPUT_MP4.search(log) and stream and stream[1] == "aac" and stream[2] == "16000"
                       and stream[3] == "mono")
    return Probe(seconds, silences, passthrough)


class FFmpeg:
    """The real audio tool: ffmpeg subprocesses over files in the request's temp directory."""

    def __init__(self, binary="ffmpeg"):
        self.binary = binary

    def _run(self, args, deadline):
        timeout = FFMPEG_TIMEOUT if deadline is None else min(FFMPEG_TIMEOUT, max(deadline.remaining(), 0.1))
        try:
            # One thread each: the VPS has two cores, and a transcription may run three of these at once.
            return subprocess.run([self.binary, "-nostdin", "-hide_banner", "-threads", "1",
                                   "-protocol_whitelist", "file", *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if deadline is not None and deadline.expired():
                raise DeadlineExceeded("The deadline ran out while ffmpeg worked.") from None
            raise AudioUnreadable() from None

    def probe(self, path, demuxer, deadline=None):
        """Decode the whole recording once -> Probe. Its stderr is read here and dropped, never logged."""
        done = self._run(["-f", demuxer, "-i", path, "-map", "0:a:0", "-vn", "-af", SILENCE, "-f", "null",
                          "-progress", "pipe:1", "-nostats", "-"], deadline)
        if done.returncode != 0:
            raise AudioUnreadable()
        return parse_probe(done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def encode(self, path, demuxer, out, start=None, end=None, deadline=None):
        """The recording, or [start, end) of it, as 16 kHz mono AAC in MP4 -> bytes (and `out` removed)."""
        window = [] if start is None else ["-ss", f"{start:.3f}"]
        length = [] if end is None else ["-t", f"{end - (start or 0.0):.3f}"]
        done = self._run(["-loglevel", "error", *window, "-f", demuxer, "-i", path, *length, "-map", "0:a:0", "-vn",
                          "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", "-f", "mp4", "-y", out],
                         deadline)
        try:
            if done.returncode != 0 or not os.path.exists(out):
                raise AudioUnreadable()
            with open(out, "rb") as f:
                data = f.read()
        finally:
            if os.path.exists(out):
                os.remove(out)
        if not data:
            raise AudioUnreadable()
        return data


def plan(seconds, silences, *, split_over, chunk):
    """
    [(start, end)] pieces covering [0, seconds]: one piece up to `split_over`, else
    ceil(seconds / chunk) of about equal length, each cut moved to the middle of the
    nearest pause within CUT_WINDOW of its target.
    """
    if seconds <= split_over:
        return [(0.0, seconds)]
    count = math.ceil(seconds / chunk)
    cuts, previous = [], 0.0
    for n in range(1, count):
        target = seconds * n / count
        pauses = [(s + e) / 2 for s, e in silences if abs((s + e) / 2 - target) <= CUT_WINDOW]
        cut = min(pauses, key=lambda p: abs(p - target)) if pauses else target
        if cut - previous < MIN_PIECE or seconds - cut < MIN_PIECE:
            cut = target
        cuts.append(cut)
        previous = cut
    edges = [0.0, *cuts, seconds]
    return list(zip(edges, edges[1:]))


def transcribe(client, path, demuxer, probe, *, work, tool, stt, language, deadline):
    """
    The recording at `path` -> (transcript, pieces). `client` is the request's bound model
    client; `work` the request's temp directory; `stt` remote config's stt block.
    """
    pieces = plan(probe.seconds, probe.silences, split_over=stt["split_over_seconds"], chunk=stt["chunk_seconds"])
    whole = len(pieces) == 1

    def one(n, start, end):
        if whole and probe.passthrough:
            with open(path, "rb") as f:
                audio = f.read()
        else:
            audio = tool.encode(path, demuxer, os.path.join(work, f"piece-{n}.m4a"),
                                None if whole else start, None if whole else end, deadline)
        try:
            return client.transcribe(audio, fmt="m4a", language=language)
        except TranscriptionFailed:
            return ""

    with ThreadPoolExecutor(min(stt["parallel"], len(pieces)), thread_name_prefix="notch-stt") as pool:
        futures = [pool.submit(one, n, start, end) for n, (start, end) in enumerate(pieces)]
        try:
            texts = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    transcript = " ".join(text.strip() for text in texts if text.strip())
    if not transcript:
        raise TranscriptionFailed("No speech detected.")
    return transcript, len(pieces)
