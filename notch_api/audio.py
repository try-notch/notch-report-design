"""
audio.py — turn whatever the phone recorded into what speech-to-text wants.

iOS records mono AAC at 44.1 kHz in an .m4a (§5 "Request size"); Whisper works
at 16 kHz mono and downsamples anything else first. Transcoding here means the
upload to the model is a third the size and in a format every provider accepts.

Two details that are easy to get wrong:
  - Input goes through a temp FILE, not stdin. MP4 keeps its index (the moov
    atom) wherever the encoder put it, often at the end, and ffmpeg has to seek
    to find it; a pipe cannot seek.
  - Output also goes through a temp file. Written to a pipe, the WAV header's
    RIFF and data sizes are left as 0xFFFFFFFF because ffmpeg cannot go back and
    fill them in, and some decoders reject that.
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


def to_wav_16k(data):
    """Any audio ffmpeg can read -> 16 kHz mono 16-bit PCM WAV bytes."""
    if not data:
        raise AudioUnreadable("The recording is empty.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in.m4a"), os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", "16000", "-f", "wav", dst],
            capture_output=True, timeout=120,
        )
        wav = b""
        if proc.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                wav = f.read()
    if not wav:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        raise AudioUnreadable(f"ffmpeg could not decode the recording: {detail[-1] if detail else 'no output'}")
    return wav
