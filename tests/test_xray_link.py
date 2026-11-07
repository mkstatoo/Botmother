"""Share-link → Xray config conversion (all data below is synthetic)."""

from __future__ import annotations

import base64
import json

import pytest

from botfactory.xray_link import LinkError, build_config

UUID = "11111111-2222-3333-4444-555555555555"


def out(link: str) -> dict:
    return build_config(link)["outbounds"][0]


def test_vless_reality():
    o = out(f"vless://{UUID}@example.org:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.example.com&fp=chrome&pbk=PUBKEY&sid=ab12&type=tcp#name")
    assert o["protocol"] == "vless"
    user = o["settings"]["vnext"][0]["users"][0]
    assert user["id"] == UUID and user["flow"] == "xtls-rprx-vision"
    assert o["settings"]["vnext"][0]["port"] == 443
    rs = o["streamSettings"]["realitySettings"]
    assert rs["publicKey"] == "PUBKEY" and rs["shortId"] == "ab12" and rs["serverName"] == "www.example.com"
    assert o["streamSettings"]["security"] == "reality"


def test_vless_ws_tls():
    o = out(f"vless://{UUID}@example.org:443?security=tls&type=ws&host=cdn.example.org&path=%2Fws%3Fed%3D2048&sni=cdn.example.org&alpn=h2,http/1.1&fp=firefox")
    s = o["streamSettings"]
    assert s["network"] == "ws" and s["wsSettings"] == {"path": "/ws?ed=2048", "headers": {"Host": "cdn.example.org"}}
    assert s["tlsSettings"]["serverName"] == "cdn.example.org" and s["tlsSettings"]["alpn"] == ["h2", "http/1.1"]


def test_vless_grpc():
    o = out(f"vless://{UUID}@example.org:8443?security=tls&type=grpc&serviceName=svc&mode=multi&sni=example.org")
    assert o["streamSettings"]["grpcSettings"] == {"serviceName": "svc", "multiMode": True}


def test_vmess():
    payload = {"v": "2", "ps": "n", "add": "example.org", "port": "443", "id": UUID, "aid": "0", "scy": "auto", "net": "ws", "type": "none", "host": "h.example.org", "path": "/p", "tls": "tls", "sni": "h.example.org"}
    link = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
    o = out(link)
    assert o["protocol"] == "vmess"
    assert o["settings"]["vnext"][0]["users"][0]["id"] == UUID
    assert o["streamSettings"]["network"] == "ws" and o["streamSettings"]["security"] == "tls"


def test_trojan():
    o = out("trojan://s3cret%40x@example.org:443?security=tls&sni=example.org&type=tcp")
    assert o["protocol"] == "trojan"
    assert o["settings"]["servers"][0]["password"] == "s3cret@x"
    assert o["streamSettings"]["tlsSettings"]["serverName"] == "example.org"


def test_shadowsocks_both_forms():
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pw123").decode().rstrip("=")
    a = out(f"ss://{userinfo}@example.org:8388#n")
    whole = base64.urlsafe_b64encode(b"aes-256-gcm:pw123@example.org:8388").decode().rstrip("=")
    b = out(f"ss://{whole}#n")
    for o in (a, b):
        srv = o["settings"]["servers"][0]
        assert (srv["method"], srv["password"], srv["address"], srv["port"]) == ("aes-256-gcm", "pw123", "example.org", 8388)


def test_plain_socks_and_http():
    o = out("socks5://user:pa%20ss@example.org:1080")
    assert o["protocol"] == "socks" and o["settings"]["servers"][0]["users"] == [{"user": "user", "pass": "pa ss"}]
    assert out("http://example.org:3128")["protocol"] == "http"


def test_config_shape_listens_only_locally():
    cfg = build_config(f"vless://{UUID}@example.org:443?security=tls&sni=example.org")
    assert {i["listen"] for i in cfg["inbounds"]} == {"127.0.0.1"}
    assert {i["port"] for i in cfg["inbounds"]} == {1080, 8118}
    assert cfg["outbounds"][0]["tag"] == "proxy"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "hello",
        "ftp://example.org",
        "vless://example.org:443",  # no user
        f"vless://{UUID}@example.org",  # no port
        f"vless://{UUID}@example.org:99999",
        f"vless://{UUID}@example.org:443?security=reality&sni=x",  # no pbk
        "vmess://!!!notbase64!!!",
        "ss://abc@example.org:1?plugin=obfs-local",
    ],
)
def test_bad_links_are_rejected(bad):
    with pytest.raises(LinkError):
        build_config(bad)


# ----------------------------------------------------------------------------- subscriptions
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from botfactory import xray_link as xl

L1 = f"vless://{UUID}@a.example.org:443?security=tls&type=ws&path=%2Fw&sni=a.example.org#%F0%9F%87%A9%F0%9F%87%AA%20DE"
L2 = "trojan://pw@b.example.org:443?sni=b.example.org#NL"
L3 = "hysteria2://x@c.example.org:443#unsupported-skipped"


@pytest.fixture
def sub_server():
    body_b64 = base64.b64encode("\n".join([L1, L2, L3]).encode())
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["ua"] = self.headers.get("User-Agent")
            data = body_b64 if self.path.startswith("/sub/") else b"<html>not found</html>"
            self.send_response(200 if self.path.startswith("/sub/") else 404)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_subscription_detection():
    assert xl.is_subscription_url("https://host.example/sub/abcd")
    assert xl.is_subscription_url("http://host.example/sub/abcd")
    assert not xl.is_subscription_url("http://host.example:3128")
    assert not xl.is_subscription_url("vless://x@h:1")


def test_parse_subscription_base64_and_plain():
    assert xl.parse_subscription(base64.b64encode("\n".join([L1, L2, L3]).encode()).decode()) == [L1, L2]
    assert xl.parse_subscription("\n".join([L1, L2])) == [L1, L2]
    with pytest.raises(LinkError):
        xl.parse_subscription("proxies:\n  - name: clash")


def test_subscription_cli_list_and_pick(sub_server, monkeypatch, capsys):
    base, seen = sub_server
    url = f"{base}/sub/TOKEN123"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(url))
    assert xl.main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "a.example.org:443" in listing and "b.example.org:443" in listing and "c.example.org" not in listing
    assert "TOKEN123" not in listing and UUID not in listing and "pw" not in listing.replace("example", "")
    assert seen["ua"].startswith("v2rayN")

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(url))
    assert xl.main(["--pick", "2"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["outbounds"][0]["protocol"] == "trojan"

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(url))
    assert xl.main(["--pick", "9"]) == 1
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(f"{base}/nope/x"))
    assert xl.main(["--list"]) == 1


def test_unreachable_subscription_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setattr(xl.time, "sleep", lambda *_: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("https://127.0.0.1:9/sub/x"))
    assert xl.main(["--list"]) == 1
    assert "خطا" in capsys.readouterr().err


def test_server_errors_are_retried_then_succeed(monkeypatch):
    monkeypatch.setattr(xl.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"ok"

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise xl.urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)
        return Resp()

    monkeypatch.setattr(xl.urllib.request, "urlopen", fake_urlopen)
    assert xl.fetch_subscription("https://h.example/sub/x") == "ok"
    assert calls["n"] == 3

    # 4xx is not retried
    calls["n"] = 0

    def gone(req, timeout):
        calls["n"] += 1
        raise xl.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(xl.urllib.request, "urlopen", gone)
    with pytest.raises(LinkError):
        xl.fetch_subscription("https://h.example/sub/x")
    assert calls["n"] == 1


def test_links_file_caches_and_is_private(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "links.txt"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("\n".join([L1, L2])))
    # a plain list of links on stdin is not a URL → treated as ONE raw link, so feed a single link
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(L2))
    assert xl.main(["--links-file", str(cache), "--list"]) == 0
    assert oct(cache.stat().st_mode & 0o777) == "0o600"
    # second call must not need stdin at all (the subscription may rate-limit repeated fetches)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    assert xl.main(["--links-file", str(cache), "--pick", "1"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["outbounds"][0]["protocol"] == "trojan"


# ----------------------------------------------------------------------------- benchmark
import os
import socket
import stat
import sys
from pathlib import Path

FAKE_XRAY = Path(__file__).with_name("fake_xray.py")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ss(host: str) -> str:
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=")
    return f"ss://{userinfo}@{host}:8388#{host}"


def test_benchmark_ranks_by_delay_and_drops_dead_servers(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hi")

        def log_message(self, *a):
            pass

    web = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=web.serve_forever, daemon=True).start()

    wrapper = tmp_path / "xray"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_XRAY}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    links = [_ss("ok-400.example"), _ss("dead.example"), _ss("ok-60.example"), _ss("ok-200.example")]
    try:
        ranked = xl.benchmark(links, str(wrapper), f"http://127.0.0.1:{web.server_port}/", workers=4, base_port=_free_port() + 50, timeout=5)
    finally:
        web.shutdown()

    assert [i for i, _ in ranked] == [3, 4, 1]  # 60ms, 200ms, 400ms; index 2 (dead) is dropped
    assert all(ms >= 50 for _, ms in ranked)


def test_bench_cli_prints_ranking_and_fails_when_nothing_works(tmp_path, monkeypatch, capsys):
    wrapper = tmp_path / "xray"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_XRAY}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    cache = tmp_path / "links.txt"
    cache.write_text(_ss("dead.example") + "\n")
    monkeypatch.setattr(xl.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 7, "stdout": ""})())
    assert xl.main(["--links-file", str(cache), "--bench", "--xray", str(wrapper)]) == 1
    assert "خطا" in capsys.readouterr().err


def test_resolve_links_splits_several_pasted_links():
    a, b = "ss://YWVzLTI1Ni1nY206cHc@a.example:8388#a", "trojan://pw@b.example:443#b"
    assert xl.resolve_links(f"{a}\n{b}\n") == [a, b]
    assert xl.resolve_links(f"{a} {b}") == [a, b]
    assert xl.resolve_links(a) == [a]
