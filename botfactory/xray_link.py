"""Convert a proxy share link into an Xray client config exposing local SOCKS5/HTTP proxies.

Usage on the server (the link is read from stdin so it never lands in shell history or ps):

    read -rs LINK && printf '%s' "$LINK" | python -m botfactory.xray_link > /usr/local/etc/xray/config.json

Supported: vless://, vmess://, trojan://, ss://, socks5://, http://
The bot then uses ``PROXY_URL=socks5://127.0.0.1:1080`` (or ``http://127.0.0.1:8118``).
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

SOCKS_PORT = 1080
HTTP_PORT = 8118


class LinkError(ValueError):
    pass


def _b64decode(data: str) -> bytes:
    data = unquote(data).strip()
    data += "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data) if ("-" in data or "_" in data) else base64.b64decode(data)
    except (binascii.Error, ValueError) as exc:
        raise LinkError("بخش base64 لینک معتبر نیست.") from exc


def _q(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def _port(value: int | str | None) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LinkError("پورت در لینک نیست یا معتبر نیست.") from exc
    if not 1 <= port <= 65535:
        raise LinkError("پورت خارج از بازه است.")
    return port


def _stream(
    network: str,
    security: str,
    *,
    host: str = "",
    path: str = "",
    sni: str = "",
    fp: str = "",
    alpn: str = "",
    service_name: str = "",
    header_type: str = "",
    pbk: str = "",
    sid: str = "",
    spx: str = "",
    allow_insecure: bool = False,
    mode: str = "",
) -> dict[str, Any]:
    network = {"": "tcp", "h2": "http", "splithttp": "xhttp"}.get(network, network)
    if network not in {"tcp", "ws", "grpc", "http", "httpupgrade", "xhttp", "kcp", "quic"}:
        raise LinkError(f"نوع انتقال «{network}» پشتیبانی نمی‌شود.")
    stream: dict[str, Any] = {"network": network, "security": security if security in {"tls", "reality"} else "none"}

    if network == "ws":
        ws: dict[str, Any] = {"path": path or "/"}
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": service_name or path, "multiMode": mode == "multi"}
    elif network == "httpupgrade":
        hu: dict[str, Any] = {"path": path or "/"}
        if host:
            hu["host"] = host
        stream["httpupgradeSettings"] = hu
    elif network == "xhttp":
        xh: dict[str, Any] = {"path": path or "/"}
        if host:
            xh["host"] = host
        if mode:
            xh["mode"] = mode
        stream["xhttpSettings"] = xh
    elif network == "http":
        h2: dict[str, Any] = {"path": path or "/"}
        if host:
            h2["host"] = [h for h in host.split(",") if h]
        stream["httpSettings"] = h2
    elif network == "tcp" and header_type == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [p for p in (path or "/").split(",") if p], "headers": {"Host": [h for h in host.split(",") if h]}},
            }
        }

    if stream["security"] == "tls":
        tls: dict[str, Any] = {"allowInsecure": allow_insecure}
        if sni or host:
            tls["serverName"] = sni or host
        if fp:
            tls["fingerprint"] = fp
        if alpn:
            tls["alpn"] = [a for a in alpn.split(",") if a]
        stream["tlsSettings"] = tls
    elif stream["security"] == "reality":
        if not pbk:
            raise LinkError("لینک reality کلید عمومی (pbk) ندارد.")
        stream["realitySettings"] = {
            "serverName": sni,
            "fingerprint": fp or "chrome",
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": spx,
        }
    return stream


def _vless(link: str) -> dict[str, Any]:
    u = urlsplit(link)
    if not (u.username and u.hostname):
        raise LinkError("لینک vless ناقص است.")
    p = parse_qs(u.query)
    user: dict[str, Any] = {"id": unquote(u.username), "encryption": _q(p, "encryption", "none")}
    if _q(p, "flow"):
        user["flow"] = _q(p, "flow")
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": u.hostname, "port": _port(u.port), "users": [user]}]},
        "streamSettings": _stream(
            _q(p, "type", "tcp"),
            _q(p, "security", "none"),
            host=_q(p, "host"),
            path=unquote(_q(p, "path")),
            sni=_q(p, "sni"),
            fp=_q(p, "fp"),
            alpn=_q(p, "alpn"),
            service_name=_q(p, "serviceName"),
            header_type=_q(p, "headerType"),
            pbk=_q(p, "pbk"),
            sid=_q(p, "sid"),
            spx=unquote(_q(p, "spx")),
            allow_insecure=_q(p, "allowInsecure") in {"1", "true"},
            mode=_q(p, "mode"),
        ),
    }


def _vmess(link: str) -> dict[str, Any]:
    try:
        d = json.loads(_b64decode(link[len("vmess://"):]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LinkError("لینک vmess معتبر نیست.") from exc
    if not (d.get("add") and d.get("id")):
        raise LinkError("لینک vmess ناقص است.")
    return {
        "protocol": "vmess",
        "settings": {
            "vnext": [
                {
                    "address": d["add"],
                    "port": _port(d.get("port")),
                    "users": [{"id": d["id"], "alterId": int(d.get("aid") or 0), "security": d.get("scy") or "auto"}],
                }
            ]
        },
        "streamSettings": _stream(
            str(d.get("net") or "tcp"),
            "tls" if d.get("tls") == "tls" else "none",
            host=str(d.get("host") or ""),
            path=str(d.get("path") or ""),
            sni=str(d.get("sni") or ""),
            fp=str(d.get("fp") or ""),
            alpn=str(d.get("alpn") or ""),
            service_name=str(d.get("path") or "") if d.get("net") == "grpc" else "",
            header_type=str(d.get("type") or ""),
        ),
    }


def _trojan(link: str) -> dict[str, Any]:
    u = urlsplit(link)
    if not (u.username and u.hostname):
        raise LinkError("لینک trojan ناقص است.")
    p = parse_qs(u.query)
    return {
        "protocol": "trojan",
        "settings": {"servers": [{"address": u.hostname, "port": _port(u.port), "password": unquote(u.username)}]},
        "streamSettings": _stream(
            _q(p, "type", "tcp"),
            _q(p, "security", "tls"),
            host=_q(p, "host"),
            path=unquote(_q(p, "path")),
            sni=_q(p, "sni"),
            fp=_q(p, "fp"),
            alpn=_q(p, "alpn"),
            service_name=_q(p, "serviceName"),
            header_type=_q(p, "headerType"),
            allow_insecure=_q(p, "allowInsecure") in {"1", "true"},
            mode=_q(p, "mode"),
        ),
    }


def _shadowsocks(link: str) -> dict[str, Any]:
    body = link[len("ss://"):].split("#", 1)[0]
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    if "plugin=" in query:
        raise LinkError("لینک shadowsocks با plugin پشتیبانی نمی‌شود.")
    body = body.rstrip("/")
    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
        if ":" not in unquote(userinfo):
            userinfo = _b64decode(userinfo).decode("utf-8")
        userinfo = unquote(userinfo)
    else:  # whole thing is base64: method:password@host:port
        decoded = _b64decode(body).decode("utf-8")
        userinfo, _, hostport = decoded.rpartition("@")
    method, sep, password = userinfo.partition(":")
    host, _, port = hostport.rpartition(":")
    if not (sep and method and host and port):
        raise LinkError("لینک shadowsocks ناقص است.")
    return {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{"address": host.strip("[]"), "port": _port(port), "method": method, "password": password}]
        },
    }


def _plain(link: str) -> dict[str, Any]:
    u = urlsplit(link)
    if not (u.hostname and u.port):
        raise LinkError("آدرس یا پورت پروکسی در لینک نیست.")
    server: dict[str, Any] = {"address": u.hostname, "port": _port(u.port)}
    if u.username:
        server["users"] = [{"user": unquote(u.username), "pass": unquote(u.password or "")}]
    proto = "socks" if u.scheme.startswith("socks") else "http"
    return {"protocol": proto, "settings": {"servers": [server]}}


def parse_link(link: str) -> dict[str, Any]:
    link = link.strip()
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""
    parsers = {
        "vless": _vless,
        "vmess": _vmess,
        "trojan": _trojan,
        "ss": _shadowsocks,
        "socks": _plain,
        "socks5": _plain,
        "http": _plain,
    }
    if scheme not in parsers:
        raise LinkError("نوع لینک پشتیبانی نمی‌شود (vless / vmess / trojan / ss / socks5 / http).")
    try:
        return parsers[scheme](link)
    except LinkError:
        raise
    except (ValueError, KeyError, IndexError) as exc:  # malformed port/url pieces
        raise LinkError("لینک ناقص یا نامعتبر است (آدرس، پورت یا پارامترها را بررسی کنید).") from exc


def build_config(link: str) -> dict[str, Any]:
    outbound = parse_link(link)
    outbound["tag"] = "proxy"
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": SOCKS_PORT,
                "protocol": "socks",
                "settings": {"udp": False, "auth": "noauth"},
            },
            {"tag": "http-in", "listen": "127.0.0.1", "port": HTTP_PORT, "protocol": "http", "settings": {}},
        ],
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
        "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field", "inboundTag": ["socks-in", "http-in"], "outboundTag": "proxy"}]},
    }


def main() -> int:
    link = sys.stdin.read().strip()
    if not link:
        print("خطا: لینک از ورودی خوانده نشد.", file=sys.stderr)
        return 2
    try:
        config = build_config(link)
    except LinkError as exc:
        print(f"خطا: {exc}", file=sys.stderr)
        return 1
    json.dump(config, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
