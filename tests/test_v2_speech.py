"""
speech.py: the duration measured by decoding, the pauses found on the way, the split plan
for long audio, and transcription of the pieces three at a time. The ffmpeg tests run the
real binary on tone-and-silence audio made with lavfi, in a temp root that must be empty
afterwards.
"""

import os
import subprocess
import tempfile
import threading

import pytest

from notch_api import speech
from notch_api.audio import AudioUnreadable
from notch_api.openrouter import TranscriptionFailed
from notch_api.speech import FFmpeg, Probe, parse_probe, plan

STT = {"split_over_seconds": 480, "chunk_seconds": 300, "parallel": 3, "language": "en"}


def _make(path, parts, *, rate=16000, codec=("-c:a", "aac", "-b:a", "32k"), fmt="mp4", channels=1):
    """Audio of ("tone", seconds) and ("gap", seconds) parts, in order, written with ffmpeg."""
    inputs, labels = [], []
    for n, (kind, seconds) in enumerate(parts):
        source = (f"sine=frequency={440 + 110 * n}:duration={seconds}:sample_rate={rate}" if kind == "tone"
                  else f"anullsrc=r={rate}:cl=mono:d={seconds}")
        inputs += ["-f", "lavfi", "-i", source]
        labels.append(f"[{n}:a]")
    graph = "".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", graph,
                    "-map", "[out]", "-ac", str(channels), "-ar", str(rate), *codec, "-f", fmt, "-y", path],
                   check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture
def tmp_root(tmp_path):
    root = tmp_path / "tmpfs"
    root.mkdir()
    return str(root)


class Recorder:
    """A bound-client stand-in: records each piece it is sent and says how big it was."""

    def __init__(self, silent=()):
        self.pieces, self.lock, self.silent = [], threading.Lock(), set(silent)

    def transcribe(self, audio, *, fmt, language):
        with self.lock:
            n = len(self.pieces)
            self.pieces.append(audio)
        if n in self.silent:
            raise TranscriptionFailed("No speech detected.")
        return f"piece{n}"


# ---------------------------------------------------------------------------
# Pure: the probe's output and the plan.
# ---------------------------------------------------------------------------

def test_the_probe_reads_the_decoded_length_the_pauses_and_the_format():
    progress = "out_time_us=1000000\nprogress=continue\nout_time_us=22013000\nout_time_ms=22013000\nprogress=end\n"
    log = ("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'in':\n"
           "  Stream #0:0[0x1](und): Audio: aac (LC) (mp4a / 0x6134706D), 16000 Hz, mono, fltp, 32 kb/s\n"
           "[silencedetect @ 0x1] silence_start: 9.99\n[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 2\n"
           "[silencedetect @ 0x1] silence_start: 21.5\n")
    assert parse_probe(progress, log) == Probe(22.013, [(9.99, 12.0), (21.5, 22.013)], True)


@pytest.mark.parametrize("log", [
    "Input #0, mp3, from 'in':\n  Stream #0:0: Audio: mp3, 16000 Hz, mono, fltp, 32 kb/s\n",
    "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'in':\n  Stream #0:0: Audio: aac (LC), 44100 Hz, mono, fltp\n",
    "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'in':\n  Stream #0:0: Audio: aac (LC), 16000 Hz, stereo, fltp\n",
])
def test_anything_but_16k_mono_aac_in_mp4_is_encoded_again(log):
    assert parse_probe("out_time_us=5000000\n", log).passthrough is False


def test_a_probe_that_decoded_nothing_is_unreadable():
    with pytest.raises(AudioUnreadable):
        parse_probe("out_time_us=0\nprogress=end\n", "")


def test_up_to_the_split_threshold_the_recording_is_one_piece():
    assert plan(480, [(100, 101)], split_over=480, chunk=300) == [(0.0, 480)]


def test_a_long_recording_is_cut_at_the_pause_nearest_each_target():
    pieces = plan(600, [(100, 102), (280, 284), (350, 352)], split_over=480, chunk=300)
    assert pieces == [(0.0, 282.0), (282.0, 600)]


def test_a_cut_with_no_pause_nearby_falls_on_its_target_and_pieces_stay_even():
    pieces = plan(1800, [(10, 11)], split_over=480, chunk=300)
    assert [round(end - start) for start, end in pieces] == [300] * 6
    assert pieces[0][0] == 0.0 and pieces[-1][1] == 1800


def test_pieces_cover_the_recording_without_gaps():
    silences = [(t, t + 1.0) for t in range(30, 1790, 47)]
    pieces = plan(1790.5, silences, split_over=480, chunk=300)
    assert all(a[1] == b[0] for a, b in zip(pieces, pieces[1:]))
    assert pieces[0][0] == 0.0 and pieces[-1][1] == 1790.5
    assert all(240 <= end - start <= 360 for start, end in pieces)


# ---------------------------------------------------------------------------
# The real ffmpeg.
# ---------------------------------------------------------------------------

def test_ffmpeg_measures_a_16k_mono_aac_recording_and_passes_it_through(tmp_path):
    path = _make(str(tmp_path / "rec.m4a"), [("tone", 10), ("gap", 2), ("tone", 10)])
    probe = FFmpeg().probe(path, "mov")
    assert probe.seconds == pytest.approx(22.0, abs=0.1)
    assert probe.passthrough
    ((start, end),) = probe.silences
    assert start == pytest.approx(10, abs=0.2) and end == pytest.approx(12, abs=0.2)


def test_ffmpeg_reencodes_other_formats_to_16k_mono_aac(tmp_path):
    path = _make(str(tmp_path / "rec.mp3"), [("tone", 6)], rate=44100, codec=("-c:a", "libmp3lame"), fmt="mp3",
                 channels=2)
    probe = FFmpeg().probe(path, "mp3")
    assert not probe.passthrough and probe.seconds == pytest.approx(6.0, abs=0.1)
    encoded = FFmpeg().encode(path, "mp3", str(tmp_path / "out.m4a"))
    assert not os.path.exists(tmp_path / "out.m4a")
    again = tmp_path / "again.m4a"
    again.write_bytes(encoded)
    reprobe = FFmpeg().probe(str(again), "mov")
    assert reprobe.passthrough and reprobe.seconds == pytest.approx(6.0, abs=0.15)


def test_the_duration_is_decoded_not_read_from_the_header(tmp_path):
    """A WAV whose header claims far more data than it holds is measured by what decodes."""
    path = _make(str(tmp_path / "rec.wav"), [("tone", 3)], codec=("-c:a", "pcm_s16le"), fmt="wav")
    data = bytearray(open(path, "rb").read())
    size_at = data.index(b"data") + 4
    data[size_at:size_at + 4] = (2_000_000_000).to_bytes(4, "little")    # the data chunk now claims ~17 hours
    lying = tmp_path / "lying.wav"
    lying.write_bytes(bytes(data))
    assert FFmpeg().probe(str(lying), "wav").seconds == pytest.approx(3.0, abs=0.1)


@pytest.mark.parametrize("content", [b"", b"this is not audio at all", os.urandom(4096)])
def test_bytes_that_are_not_audio_are_unreadable(tmp_path, content):
    path = tmp_path / "junk"
    path.write_bytes(content)
    with pytest.raises(AudioUnreadable):
        FFmpeg().probe(str(path), "mov")


def test_an_upload_that_is_really_a_playlist_cannot_make_ffmpeg_open_another_file(tmp_path):
    real = _make(str(tmp_path / "real.m4a"), [("tone", 5)])
    playlist = tmp_path / "upload"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\n{real}\n#EXT-X-ENDLIST\n")
    for demuxer in speech.DEMUXERS.values():
        with pytest.raises(AudioUnreadable):
            FFmpeg().probe(str(playlist), demuxer)


def test_a_long_recording_is_split_at_its_pause_and_transcribed_in_order(tmp_root):
    with tempfile.TemporaryDirectory(dir=tmp_root) as work:
        path = _make(os.path.join(work, "input"), [("tone", 290), ("gap", 2), ("tone", 308)])
        tool = FFmpeg()
        probe = tool.probe(path, "mov")
        assert probe.seconds == pytest.approx(600, abs=0.2)
        client = Recorder()
        transcript, pieces = speech.transcribe(client, path, "mov", probe, work=work, tool=tool, stt=STT,
                                               language="en", deadline=None)
        assert pieces == 2 and sorted(transcript.split()) == ["piece0", "piece1"]
        lengths = []
        for n, audio in enumerate(client.pieces):
            piece = os.path.join(work, f"check-{n}.m4a")
            with open(piece, "wb") as f:
                f.write(audio)
            lengths.append(tool.probe(piece, "mov").seconds)
            os.remove(piece)
        assert sorted(lengths) == [pytest.approx(291, abs=0.5), pytest.approx(309, abs=0.5)]
        os.remove(path)
        assert os.listdir(work) == []   # every piece file was removed as it was read
    assert os.listdir(tmp_root) == []


def test_a_short_recording_in_the_apps_format_is_sent_as_it_is(tmp_root):
    with tempfile.TemporaryDirectory(dir=tmp_root) as work:
        path = _make(os.path.join(work, "input"), [("tone", 4)])
        probe = FFmpeg().probe(path, "mov")
        client = Recorder()
        assert speech.transcribe(client, path, "mov", probe, work=work, tool=FFmpeg(), stt=STT, language="en",
                                 deadline=None) == ("piece0", 1)
        assert client.pieces == [open(path, "rb").read()]


def test_quiet_pieces_are_skipped_and_all_quiet_is_no_speech(tmp_root):
    probe = Probe(900, [], False)

    class Tool:
        def encode(self, path, demuxer, out, start=None, end=None, deadline=None):
            return b"piece"

    joined, pieces = speech.transcribe(Recorder(silent={1}), "in", "mov", probe, work=tmp_root, tool=Tool(),
                                       stt=STT, language="en", deadline=None)
    assert pieces == 3 and "piece1" not in joined.split()
    with pytest.raises(TranscriptionFailed):
        speech.transcribe(Recorder(silent={0, 1, 2}), "in", "mov", probe, work=tmp_root, tool=Tool(), stt=STT,
                          language="en", deadline=None)


def test_at_most_parallel_pieces_are_transcribed_at_once():
    running, peak, lock = [0], [0], threading.Lock()
    gate = threading.Event()

    class Slow:
        def transcribe(self, audio, *, fmt, language):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            gate.wait(0.05)
            with lock:
                running[0] -= 1
            return "x"

    class Tool:
        def encode(self, path, demuxer, out, start=None, end=None, deadline=None):
            return b"piece"

    speech.transcribe(Slow(), "in", "mov", Probe(1800, [], False), work="unused", tool=Tool(), stt=STT,
                      language="en", deadline=None)
    assert peak[0] == 3
