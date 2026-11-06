"""Convert a proxy share link into an Xray client config exposing local SOCKS5/HTTP proxies.

Usage on the server (the link is read from stdin so it never lands in shell history or ps):

    read -rs LINK && printf '%s' "$LINK" | python -m botfactory.xray_link > /usr/local/etc/xray/config.json

Supported: vless://, vmess://, trojan://, ss://, socks5://, http://
and subscription URLs (https://host/sub/...): the list of nodes is fetched and one is chosen with --pick N.
The bot then uses ``PROXY_URL=socks5://127.0.0.1:1080`` (or ``http://127.0.0.1:8118``).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def build_config(link: str, socks_port: int = SOCKS_PORT, http_port: int | None = HTTP_PORT) -> dict[str, Any]:
    outbound = parse_link(link)
    outbound["tag"] = "proxy"
    inbounds: list[dict[str, Any]] = [
        {
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "port": socks_port,
            "protocol": "socks",
            "settings": {"udp": False, "auth": "noauth"},
        }
    ]
    tags = ["socks-in"]
    if http_port is not None:
        inbounds.append({"tag": "http-in", "listen": "127.0.0.1", "port": http_port, "protocol": "http", "settings": {}})
        tags.append("http-in")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
        "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field", "inboundTag": tags, "outboundTag": "proxy"}]},
    }


SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://")


def is_subscription_url(text: str) -> bool:
    """https://host/path…  → subscription.  A bare http://host:port stays a plain HTTP proxy."""
    u = urlsplit(text.strip())
    return u.scheme in {"http", "https"} and bool(u.hostname) and (u.path not in {"", "/"} or u.scheme == "https")


def fetch_subscription(url: str, timeout: int = 25, attempts: int = 4) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "v2rayN/6.45", "Accept": "*/*"})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - admin-supplied URL
                return resp.read(2_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code < 500 and exc.code != 429:
                break  # 4xx = wrong/expired link, retrying will not help
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        time.sleep(min(2 * (attempt + 1), 6))
    raise LinkError(f"دریافت لیست اشتراک ناموفق بود: {last}")


def parse_subscription(text: str) -> list[str]:
    """Subscription body = base64 of newline-separated links, or the links in plain text."""
    body = text.strip()
    if not any(scheme in body for scheme in SUPPORTED_SCHEMES):
        try:
            body = _b64decode(body.replace("\n", "").replace("\r", "")).decode("utf-8", "replace")
        except LinkError:
            body = ""
    links = [ln.strip() for ln in body.replace("\r", "").split("\n") if ln.strip().startswith(SUPPORTED_SCHEMES)]
    if not links:
        raise LinkError("در لیست اشتراک لینک قابل‌استفاده (vless/vmess/trojan/ss) پیدا نشد. فرمت Clash/sing-box پشتیبانی نمی‌شود.")
    return links


def describe(link: str) -> str:
    """Short human label without secrets: protocol, remark, host:port."""
    scheme = link.split("://", 1)[0]
    name = unquote(link.split("#", 1)[1]) if "#" in link else ""
    try:
        out = parse_link(link)
        if scheme == "ss":
            srv = out["settings"]["servers"][0]
        elif scheme in {"vless", "vmess"}:
            srv = out["settings"]["vnext"][0]
        else:
            srv = out["settings"]["servers"][0]
        where = f"{srv['address']}:{srv['port']}"
        net = out.get("streamSettings", {})
        extra = "/".join(x for x in (net.get("network"), net.get("security")) if x and x != "none")
    except LinkError:
        return f"{scheme}  {name}  (ناقص)"
    return f"{scheme}  {name}  {where}  {extra}".rstrip()


def resolve_links(text: str) -> list[str]:
    text = text.strip()
    if is_subscription_url(text):
        return parse_subscription(fetch_subscription(text))
    return [text]


def _wait_port(port: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def measure_link(
    link: str, xray_bin: str, port: int, url: str, timeout: int = 10, tries: int = 2
) -> float | None:
    """Start a throw-away Xray for ``link`` and return the best total request time in ms (None = unusable)."""
    try:
        config = build_config(link, socks_port=port, http_port=None)
    except LinkError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        proc = subprocess.Popen([xray_bin, "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        try:
            if not _wait_port(port, 6):
                return None
            best: float | None = None
            for _ in range(tries):
                r = subprocess.run(  # noqa: S603
                    ["curl", "-s", "-o", "/dev/null", "-m", str(timeout), "--noproxy", "", "--socks5-hostname", f"127.0.0.1:{port}", "-w", "%{http_code} %{time_total}", url],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=False,
                )
                code, _, secs = r.stdout.strip().partition(" ")
                if r.returncode == 0 and code and code != "000":
                    try:
                        ms = float(secs) * 1000
                    except ValueError:
                        continue
                    best = ms if best is None else min(best, ms)
            return best
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def benchmark(
    links: list[str], xray_bin: str, url: str, workers: int = 8, base_port: int = 21000, timeout: int = 10
) -> list[tuple[int, float]]:
    """Return [(1-based index, ms)] for the working links, fastest first."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(measure_link, link, xray_bin, base_port + i, url, timeout) for i, link in enumerate(links)]
        results = [(i + 1, f.result()) for i, f in enumerate(futures)]
    return sorted(((i, ms) for i, ms in results if ms is not None), key=lambda t: t[1])


def _load_links(args: argparse.Namespace) -> list[str]:
    """Links come from the cache file when it exists; otherwise from stdin (and are cached)."""
    cache = args.links_file
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            links = [ln.strip() for ln in fh if ln.strip()]
        if links:
            return links
    raw = sys.stdin.read().strip()
    if not raw:
        raise LinkError("لینک از ورودی خوانده نشد.")
    links = resolve_links(raw)
    if cache:
        fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(links) + "\n")
    return links


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="botfactory.xray_link", add_help=True)
    ap.add_argument("--list", action="store_true", help="فهرست سرورها (بدون اطلاعات محرمانه)")
    ap.add_argument("--pick", type=int, default=1, help="ساخت تنظیمات برای سرور شماره N")
    ap.add_argument("--links-file", help="فایل کش لینک‌ها (دسترسی ۶۰۰)؛ اگر نبود از stdin ساخته می‌شود")
    ap.add_argument("--bench", action="store_true", help="همه‌ی سرورها را امتحان و بر اساس تأخیر مرتب کن")
    ap.add_argument("--xray", default="/usr/local/bin/xray")
    ap.add_argument("--test-url", default="https://api.telegram.org/")
    ap.add_argument("--workers", type=int, default=8)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    try:
        links = _load_links(args)
        if args.list:
            for i, link in enumerate(links, 1):
                print(f"{i}\t{describe(link)}")
            return 0
        if args.bench:
            ranked = benchmark(links, args.xray, args.test_url, workers=args.workers)
            for i, ms in ranked:
                print(f"{i}\t{ms:.0f}\t{describe(links[i - 1])}")
            if not ranked:
                raise LinkError("هیچ‌کدام از سرورها از این شبکه به مقصد نرسید.")
            return 0
        if not 1 <= args.pick <= len(links):
            raise LinkError(f"شماره باید بین ۱ و {len(links)} باشد.")
        config = build_config(links[args.pick - 1])
    except LinkError as exc:
        print(f"خطا: {exc}", file=sys.stderr)
        return 1
    json.dump(config, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
