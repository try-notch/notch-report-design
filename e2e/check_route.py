"""
check_route.py — the writing check end to end through /v2 on a local server, on real models.

    .venv/bin/python e2e/check_route.py        # ~30 s, ~$0.01; needs OPENROUTER_API_KEY (.env)

Starts the server on 127.0.0.1 with dev auth and throwaway databases, pushes remote config that
turns the check on (analyze and takeaways on v5, check c1), posts one notch from
evals/writing_set.json to /v2/analyze and /v2/takeaways, then turns the check off and posts to
/v2/analyze again. It passes when every call answers 200 and prompt_version names the check exactly
when it is on. The takeaways are printed both ways: on this notch v5 alone tends to write the
return to 50% as a pause, which is the kind of slip the check exists for.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLUG = "sep22-fix-and-meeting"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    data = json.load(open(os.path.join(HERE, "evals", "writing_set.json")))
    notch = next(n for n in data["notches"] if n["slug"] == SLUG)
    body = {"transcript": notch["transcript"], "project_names": data["project_names"],
            "vocabulary": data["vocabulary"]}
    work = tempfile.mkdtemp(prefix="notch-check-e2e-")
    port = free_port()
    env = dict(os.environ, NOTCH_DEV_AUTH="1", NOTCH_ENV="dev", NOTCH_HOST="127.0.0.1", NOTCH_PORT=str(port),
               NOTCH_DB=os.path.join(work, "notch.db"), NOTCH_METER_DB=os.path.join(work, "meter.db"),
               NOTCH_AUDIO_DIR=os.path.join(work, "audio"))
    os.makedirs(env["NOTCH_AUDIO_DIR"])
    base = f"http://127.0.0.1:{port}"
    with open(os.path.join(work, "server.log"), "w") as log:
        server = subprocess.Popen([sys.executable, "-m", "notch_api"], cwd=HERE, env=env, stdout=log, stderr=log)
    try:
        for _ in range(60):
            try:
                httpx.get(f"{base}/v2/config", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.5)
        else:
            raise SystemExit(f"the server did not start; see {work}/server.log")

        def push(check):
            path = os.path.join(work, "config.json")
            with open(path, "w") as f:
                json.dump({"prompts": {"analyze": "v5", "takeaways": "v5", "check": check}}, f)
            subprocess.run([sys.executable, "-m", "notch_api.admin", "config", "push", path, "--note",
                            f"check_route e2e: check {check}"], cwd=HERE, env=env, check=True, capture_output=True)

        def post(route):
            response = httpx.post(f"{base}{route}", json=body, timeout=180, headers={
                "authorization": "Bearer dev", "x-client": "ios/1.0.0+1",
                "idempotency-key": str(uuid.uuid4()).upper()})
            answer = response.json()
            print(f"{route}: {response.status_code}, prompt_version {answer.get('prompt_version')!r}")
            for takeaway in answer.get("takeaways", []):
                print(f"    - {takeaway}")
            return response.status_code, answer.get("prompt_version")

        push("c1")
        results = [post("/v2/analyze") == (200, "v5+c1"), post("/v2/takeaways") == (200, "v5+c1")]
        push("off")
        results.append(post("/v2/analyze") == (200, "v5"))
    finally:
        server.terminate()
        server.wait(timeout=10)
    if not all(results):
        print(f"FAIL: {results}; server log in {work}/server.log")
        return 1
    print("PASS: config switches the check, both routes run it, and prompt_version names it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
