"""
The deploy kit (deploy/, DEPLOY.md): what deploy.sh ships runs on its own, the example
settings hold placeholders only, and the files agree with each other and with the server.
"""

import os
import re
import shutil
import subprocess
import sys

from notch_api import config

ROOT = config.REPO_ROOT
DEPLOY = os.path.join(ROOT, "deploy")


def _read(name):
    with open(os.path.join(DEPLOY, name), encoding="utf-8") as f:
        return f.read()


def test_what_deploy_sh_ships_runs_without_the_rest_of_the_repo(tmp_path):
    for name in ("notch_api", "notch_dash"):
        shutil.copytree(os.path.join(ROOT, name), tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(os.path.join(ROOT, "requirements-server.txt"), tmp_path)
    code = ("import sys; import notch_api.__main__, notch_api.v2, notch_api.cloud, notch_api.admin, notch_dash.app; "
            "demo = {'prompt_variants', 'seed_db', 'llm', 'anthropic', 'matplotlib', 'reportlab'} & set(sys.modules); "
            "sys.exit(f'imported {sorted(demo)}' if demo else 0)")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    shipped = re.findall(r"^shipped=\(([^)]*)\)", _read("deploy.sh"), re.M)
    assert shipped == ["notch_api notch_dash requirements-server.txt"]


def test_the_example_settings_are_placeholders_only():
    for name in ("notch.env.example", "dash.env.example"):
        for line in _read(name).splitlines():
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if re.search(r"KEY|SECRET|TOKEN|PASSWORD", key) and key != "APPLE_KEY_ID" and not key.endswith("_PATH"):
                assert value == "" or re.fullmatch(r"<[^<>]+>", value), f"{name}: {key} must be a <placeholder>"
    settings = dict(line.split("=", 1) for line in _read("notch.env.example").splitlines()
                    if line and not line.startswith("#"))
    assert settings["NOTCH_ENV"] == "prod" and settings["NOTCH_PORT"] == str(config.PORT)
    assert settings["NOTCH_HOST"] == "127.0.0.1"


def test_the_files_agree_on_paths_and_ports():
    caddy, api, dash, setup = _read("Caddyfile"), _read("notch-api.service"), _read("notch-dash.service"), _read(
        "setup.sh")
    assert f"reverse_proxy 127.0.0.1:{config.PORT}" in caddy and "max_size 30MB" in caddy
    assert not re.search(r"^\s*log\b", caddy, re.M), "no access log"
    assert "api.trynotch.xyz {" in caddy
    for unit in (api, dash):
        assert "User=notch" in unit and "ExecStart=/opt/notch/venv/bin/python -m notch_" in unit
        assert "EnvironmentFile=/etc/notch/notch.env" in unit and "NoNewPrivileges=yes" in unit
    assert "ReadWritePaths=/var/lib/notch" in api and "RequiresMountsFor=/var/lib/notch/tmp" in api
    settings = _read("notch.env.example")
    assert "NOTCH_TMP=/var/lib/notch/tmp" in settings and "NOTCH_METER_DB=/var/lib/notch/meter.db" in settings
    assert "tmpfs=/var/lib/notch/tmp" in setup and "venv=/opt/notch/venv" in setup
    assert re.findall(r"ufw allow (\S+)", setup) == ["22/tcp", "80/tcp", "443/tcp", "443/udp"]


def test_deploy_sh_has_no_default_host():
    script = _read("deploy.sh")
    assert '"${NOTCH_VPS:?' in script
    assert "mac-mini" not in script.lower() and "localhost" not in script.split("REMOTE", 1)[0]
