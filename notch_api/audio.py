"""
audio.py — turn whatever the phone recorded into what speech-to-text is sent.

iOS records mono AAC at 44.1 kHz and ~64 kbps in an .m4a (§5 "Request size"); Whisper
works at 16 kHz mono and downsamples anything else first. Re-encoding as 16 kHz mono
AAC at 32 kbps (§5's own recommended recorder settings) halves what the model is sent,
to about 4 KB a second, so §5's 18-minute catch-up is ~4.3 MB, ~5.8 MB once base64'd
into the request: well inside OpenRouter's 25 MB cap. (16-bit PCM WAV would be 32 KB a
second and pass that cap at about ten minutes.) The re-encode also proves the upload
decodes, so a file that is not audio fails `audio_unreadable`, not as a model refusal.

Both ends go through temp FILES, not pipes: MP4 keeps its index (the moov atom)
wherever the encoder put it, often at the end, so ffmpeg has to seek to read one and
to write one, and a pipe cannot seek.
"""

import os
import subprocess
import tempfile


class AudioUnreadable(Exception):
    """The upload is not audio ffmpeg can decode. Same shape as openrouter.ModelError."""

    code = "audio_unreadable"
    retryable = False

    def __init__(self, message="The recording could not be read as audio."):
        super().__init__(message)
        self.message = message


def to_m4a_16k(data):
    """Any audio ffmpeg can read -> 16 kHz mono AAC at 32 kbps, as .m4a bytes."""
    if not data:
        raise AudioUnreadable("The recording is empty.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.m4a"), os.path.join(tmp, "out.m4a")
        with open(src, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", src,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", dst],
            capture_output=True, timeout=120,
        )
        out = b""
        if proc.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                out = f.read()
    if not out:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        raise AudioUnreadable(f"ffmpeg could not decode the recording: {detail[-1] if detail else 'no output'}")
    return out
