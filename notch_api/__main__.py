"""
python -m notch_api — serve the API on 127.0.0.1:4131 (api.notch.localhost through Caddy).

Run it from the repo root: analysis.py and reports.py import the demo's top-level
modules (prompt_variants, seed_db, llm). The environment chooses everything else:

  NOTCH_HOST         interface, default 127.0.0.1; 0.0.0.0 lets a phone on the same
                     Wi-Fi reach it (and anyone else on that network)
  NOTCH_PORT         port, default 4131
  NOTCH_DB           SQLite file, default data/notch_api.db
  NOTCH_AUDIO_DIR    stored uploads, default data/audio
  NOTCH_FAKE_MODELS  1 = the offline doubles from fakes.py instead of OpenRouter and
                     ffmpeg, so the whole server runs with no key and no network
  OPENROUTER_API_KEY read on the first model call, not at boot
"""

import logging
import os

import uvicorn

from . import audio, config
from .app import create_app
from .fakes import FakeClient, fake_transcode


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fake = os.environ.get("NOTCH_FAKE_MODELS") == "1"
    if fake:
        logging.getLogger(__name__).info("NOTCH_FAKE_MODELS=1: offline fake models, no OpenRouter calls")
    app = create_app(db_path=config.DB_PATH, audio_dir=config.AUDIO_DIR,
                     client=FakeClient() if fake else None,
                     transcode=fake_transcode if fake else audio.to_m4a_16k)
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
