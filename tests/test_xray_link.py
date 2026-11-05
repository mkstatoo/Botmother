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
