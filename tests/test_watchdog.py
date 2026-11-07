"""Proxy watchdog (deploy/debian/xray-watchdog.sh) driven with stub curl/systemctl and a fake xray."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "debian" / "xray-watchdog.sh"
FAKE_XRAY = Path(__file__).with_name("fake_xray.py")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("curl") is None or shutil.which("flock") is None, reason="needs bash, curl, flock")


def _ss(host: str) -> str:
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=")
    return f"ss://{userinfo}@{host}:8388#{host}"


class Rig:
    def __init__(self, tmp: Path, web_port: int, links: list[str]) -> None:
        self.tmp = tmp
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.state = tmp / "state"
        self.cfg = tmp / "xray"
        self.cfg.mkdir()
        (self.cfg / "config.json").write_text('{"outbounds":[{"settings":{"servers":[{"address": "dead.example"}]}}]}')
        self.link_file = tmp / "proxy-link"
        self.set_links(links)
        self.env_file = tmp / ".env"
        self.env_file.write_text("BOT_TOKEN=123456:SECRET-TOKEN\nADMIN_IDS=11,22\n")
        self.notify_log = tmp / "notify.log"
        self.systemctl_log = tmp / "systemctl.log"
        self.proxy_ok = tmp / "proxy_ok"
        real_curl = shutil.which("curl")
        (self.stubs / "curl").write_text(
            f"""#!/bin/bash
args="$*"
if [[ "$args" == *"127.0.0.1:1080"* ]]; then
  if [[ "$args" == *"-K -"* ]]; then
    {{ cat; echo "$args"; }} >> "{self.notify_log}"
    printf '{{"ok":true}}'; exit 0
  fi
  if [ -f "{self.proxy_ok}" ]; then printf 200; else printf 000; fi
  exit 0
fi
exec "{real_curl}" "$@"
"""
        )
        (self.stubs / "systemctl").write_text(
            f"""#!/bin/bash
echo "$*" >> "{self.systemctl_log}"
if [ "$1 $2" = "restart xray" ]; then
  if grep -q '"address": "ok-' "{self.cfg}/config.json"; then touch "{self.proxy_ok}"; else rm -f "{self.proxy_ok}"; fi
fi
exit 0
"""
        )
        (self.stubs / "xray").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_XRAY}" "$@"\n')
        for f in self.stubs.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.web_port = web_port

    def set_links(self, links: list[str]) -> None:
        self.link_file.write_text("\n".join(links) + "\n")

    def run(self, **extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "APP_DIR": str(ROOT),
            "CFG_DIR": str(self.cfg),
            "BIN": str(self.stubs / "xray"),
            "STATE": str(self.state),
            "LINK_FILE": str(self.link_file),
            "ENV_FILE": str(self.env_file),
            "PROXY_ADDR": "127.0.0.1:1080",
            "CHECK_URL": f"http://127.0.0.1:{self.web_port}/",
            "API_BASE": "http://notify.invalid",
            "RECHECK_DELAY": "0",
            "PORT_WAIT": "0",
            **extra,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)  # noqa: S603, S607

    def server_in_config(self) -> str:
        return (self.cfg / "config.json").read_text()


@pytest.fixture
def web():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hi")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def make_rig(tmp_path, web, links=None) -> Rig:
    return Rig(tmp_path, web, links or [_ss("dead.example"), _ss("ok-200.example"), _ss("ok-60.example")])


def test_healthy_proxy_does_nothing(tmp_path, web):
    rig = make_rig(tmp_path, web)
    rig.proxy_ok.touch()
    r = rig.run()
    assert r.returncode == 0
    assert not rig.notify_log.exists() and not rig.systemctl_log.exists()
    assert not (rig.state / "fails").exists()


def test_outage_switches_to_fastest_working_server_and_notifies_admins(tmp_path, web):
    rig = make_rig(tmp_path, web)
    r1 = rig.run()  # first failure: only counted
    assert r1.returncode == 0 and (rig.state / "fails").read_text().strip() == "1"
    assert "dead.example" in rig.server_in_config()
    assert not rig.systemctl_log.exists()

    r2 = rig.run()  # second failure: re-pick
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "ok-60.example" in rig.server_in_config()  # fastest working server
    assert "restart xray" in rig.systemctl_log.read_text()
    log = rig.notify_log.read_text()
    assert log.count("chat_id=11") == 1 and log.count("chat_id=22") == 1
    assert "bot123456:SECRET-TOKEN/sendMessage" in log  # token only travels via stdin config, not argv
    assert not (rig.state / "outage_since").exists() and not (rig.state / "fails").exists()

    n = rig.notify_log.read_text()
    r3 = rig.run()  # healthy again → quiet
    assert r3.returncode == 0 and rig.notify_log.read_text() == n


def test_token_never_appears_in_process_arguments(tmp_path, web):
    rig = make_rig(tmp_path, web)
    rig.run()
    rig.run()
    argv_lines = [ln for ln in rig.notify_log.read_text().splitlines() if ln.startswith("-s ")]
    assert argv_lines and all("SECRET-TOKEN" not in ln for ln in argv_lines)


def test_no_working_server_keeps_trying_but_respects_cooldown(tmp_path, web):
    rig = make_rig(tmp_path, web, [_ss("dead.example")])
    rig.run()
    r = rig.run()
    assert r.returncode == 1 and (rig.state / "outage_since").exists()
    stamp = (rig.state / "last_repick").read_text()
    r = rig.run()  # inside the cooldown: no new benchmark
    assert r.returncode == 0 and (rig.state / "last_repick").read_text() == stamp
    assert not rig.notify_log.exists()

    # the provider fixes things later: a plain recovery is reported and the outage record is cleared
    rig.proxy_ok.touch()
    r = rig.run()
    assert r.returncode == 0
    assert "دوباره برقرار شد" in rig.notify_log.read_text()
    assert not (rig.state / "outage_since").exists()


def test_without_saved_link_it_only_restarts_xray(tmp_path, web):
    rig = make_rig(tmp_path, web)
    rig.link_file.unlink()
    rig.run()
    r = rig.run()
    assert r.returncode == 1
    assert "restart xray" in rig.systemctl_log.read_text()
    assert "dead.example" in rig.server_in_config()


def test_installer_scripts_are_valid_bash():
    for name in ("xray-watchdog.sh", "05-watchdog.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "debian" / name)], check=False).returncode == 0  # noqa: S603, S607
