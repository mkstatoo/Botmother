"""Stand-in for the ``xray`` binary used by the benchmark test.

``fake_xray.py run -c config.json`` starts a SOCKS5 proxy on the first inbound's port.
The outbound server *address* decides behaviour: ``dead.*`` refuses every connection,
``ok-<ms>.*`` forwards after sleeping <ms> milliseconds.
"""

from __future__ import annotations

import json
import re
import select
import socket
import socketserver
import sys
import time


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


def make_handler(address: str):
    dead = address.startswith("dead")
    m = re.match(r"ok-(\d+)", address)
    delay = int(m.group(1)) / 1000 if m else 0.0

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            c = self.request
            try:
                _, n = _recv_exact(c, 2)
                _recv_exact(c, n)
                c.sendall(b"\x05\x00")
                _, cmd, _, atyp = _recv_exact(c, 4)
                if atyp == 1:
                    host = socket.inet_ntoa(_recv_exact(c, 4))
                elif atyp == 3:
                    host = _recv_exact(c, _recv_exact(c, 1)[0]).decode()
                else:
                    return
                port = int.from_bytes(_recv_exact(c, 2), "big")
                if dead:
                    return
                time.sleep(delay)
                upstream = socket.create_connection((host, port), timeout=5)
                c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                socks = [c, upstream]
                while True:
                    ready, _, _ = select.select(socks, [], [], 10)
                    if not ready:
                        return
                    for s in ready:
                        data = s.recv(65536)
                        if not data:
                            return
                        (upstream if s is c else c).sendall(data)
            except (ConnectionError, OSError):
                return

    return Handler


def main() -> None:
    if "-test" in sys.argv:
        return
    cfg = json.load(open(sys.argv[sys.argv.index("-c") + 1], encoding="utf-8"))
    port = cfg["inbounds"][0]["port"]
    address = cfg["outbounds"][0]["settings"]["servers"][0]["address"]

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", port), make_handler(address)) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
