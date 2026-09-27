# notch_api /v2 as one container: the stateless processor, its meter database and the
# Notch Cloud ciphertext store. Every setting comes from the environment (.env through
# compose.yaml); the image never contains a key. DEPLOY.md › "Run it as a container".
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin notch \
 && install -d -o notch -g notch -m 0700 /data /tmp/notch

WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY notch_api/ notch_api/
COPY notch_dash/ notch_dash/

USER notch
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NOTCH_HOST=0.0.0.0 NOTCH_PORT=4131 \
    NOTCH_METER_DB=/data/meter.db NOTCH_TMP=/tmp/notch
EXPOSE 4131
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4131/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "notch_api"]
