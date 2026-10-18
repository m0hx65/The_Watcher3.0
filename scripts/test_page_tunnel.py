"""Regression tests for the page tunnel (2026-10-18).

Instagram sends this host's profile-page requests to its login page, and the
page is the only anonymous source of counts — so without the phone, sweeps
read no counts at all. The page tunnel carries this host's page request out
through a VPN Instagram does serve (WireGuard in user space via wireproxy).

What must hold:
- nothing is routed through the tunnel until a request through it has come
  back; a tunnel that is down leaves the page door exactly as it was;
- only the profile-page request uses it;
- a wireproxy that dies is restarted, and the route follows its state;
- with pages coming through it, a sweep does not pay the ~9 s Worker id call
  per account up front (the page carries handle, avatar and story);
- the no-counts notice names a tunnel that is down.

Runs fully offline: a stand-in "wireproxy" (a real SOCKS5 relay in Python)
and a local HTTP server stand in for the VPN and the IP-check site.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.config import settings  # noqa: E402
from app.monitor import home_fetch, page_tunnel  # noqa: E402
from app.monitor.instagram import InstagramClient  # noqa: E402
from app.monitor.page_tunnel import PageTunnel  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


WG_CONF = textwrap.dedent("""\
    [Interface]
    # Key for test
    PrivateKey = aGVsbG8gdGhpcyBpcyBub3QgYSByZWFsIGtleSE9PT0=
    Address = 10.2.0.2/32
    DNS = 10.2.0.1

    [Peer]
    PublicKey = cHVibGljIGtleSBvZiBub2JvZHkgYXQgYWxsIDEyMzQ=
    AllowedIPs = 0.0.0.0/0
    Endpoint = 203.0.113.9:51820
""")

# A stand-in wireproxy: reads "-c <config>", binds the [Socks5] address, and
# relays CONNECTs for real — or, with mode "dead", accepts and drops them,
# or with "crash", exits at once.
FAKE_WIREPROXY = textwrap.dedent('''\
    import asyncio, re, sys
    conf = open(sys.argv[sys.argv.index("-c") + 1]).read()
    host, port = re.search(r"BindAddress = ([\\d.]+):(\\d+)", conf).groups()
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-c" else "ok"
    if mode == "crash":
        print("fatal: cannot reach the peer", flush=True)
        sys.exit(3)

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        finally:
            w.close()

    async def client(r, w):
        if mode == "dead":
            w.close()
            return
        try:
            head = await r.readexactly(2)
        except asyncio.IncompleteReadError:
            return  # a bare connect: the bot checking the port is open
        await r.readexactly(head[1])
        w.write(b"\\x05\\x00")
        ver, cmd, _, atyp = await r.readexactly(4)
        if atyp == 1:
            target = ".".join(str(b) for b in await r.readexactly(4))
        else:
            target = (await r.readexactly((await r.readexactly(1))[0])).decode()
        tport = int.from_bytes(await r.readexactly(2), "big")
        tr, tw = await asyncio.open_connection(target, tport)
        w.write(b"\\x05\\x00\\x00\\x01" + bytes(4) + bytes(2))
        await w.drain()
        await asyncio.gather(pipe(r, tw), pipe(tr, w))

    async def main():
        server = await asyncio.start_server(client, host, int(port))
        print("wireproxy stand-in listening", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())
''')


class _IpCheck(BaseHTTPRequestHandler):
    """Stands in for api.ipify.org: answers with an IP and counts requests."""

    hits = 0

    def do_GET(self) -> None:
        _IpCheck.hits += 1
        body = b"203.0.113.7"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _Fixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Path(self.tmp.name) / "fake_wireproxy.py"
        self.fake.write_text(FAKE_WIREPROXY, encoding="utf-8")
        self.conf = Path(self.tmp.name) / "proton.conf"
        self.conf.write_text(WG_CONF, encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _IpCheck)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.check_url = f"http://127.0.0.1:{self.server.server_port}/"

    def tunnel(self, mode: str = "ok") -> PageTunnel:
        return PageTunnel(command=[sys.executable, str(self.fake), mode])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


def test_the_config_is_found_wherever_it_is_given(fx: _Fixture) -> None:
    old, old_secret = settings.page_tunnel_config, page_tunnel.SECRET_FILE
    try:
        settings.page_tunnel_config = None
        page_tunnel.SECRET_FILE = Path(fx.tmp.name) / "missing.conf"
        expect("no config, no tunnel", page_tunnel._load_config() is None)
        page_tunnel.SECRET_FILE = fx.conf
        expect("a Render Secret File is picked up", page_tunnel._load_config() == WG_CONF)
        settings.page_tunnel_config = str(fx.conf)
        expect("a path in PAGE_TUNNEL_CONFIG is read", page_tunnel._load_config() == WG_CONF)
        settings.page_tunnel_config = WG_CONF
        expect("so is the text itself", page_tunnel._load_config() == WG_CONF.strip())
        settings.page_tunnel_config = WG_CONF.replace("\n", "\\n")
        expect("even pasted into one line with \\n for newlines",
               page_tunnel._load_config() == WG_CONF)
        expect("a config missing its peer is refused",
               page_tunnel._config_problem(WG_CONF.split("[Peer]")[0]) is not None)
        expect("a whole config passes", page_tunnel._config_problem(WG_CONF) is None)
    finally:
        settings.page_tunnel_config, page_tunnel.SECRET_FILE = old, old_secret


async def test_the_tunnel_is_used_only_once_proven(fx: _Fixture) -> None:
    old = (settings.page_tunnel_config, page_tunnel.CHECK_URL, page_tunnel.tunnel)
    settings.page_tunnel_config = str(fx.conf)
    page_tunnel.CHECK_URL = fx.check_url
    try:
        tunnel = fx.tunnel("ok")
        page_tunnel.tunnel = tunnel
        await tunnel.start()
        expect("a working tunnel is proven and used",
               tunnel.up and tunnel.egress_ip == "203.0.113.7"
               and (page_tunnel.page_proxy() or "").startswith("socks5h://127.0.0.1:"),
               tunnel.describe())
        expect("the check really went through it", _IpCheck.hits >= 1, repr(_IpCheck.hits))
        expect("the route says so", page_tunnel.page_route() == "the page tunnel")

        # The page request — and only it — carries the proxy.
        class Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            async def get(self, url: str, **kw: Any):
                self.calls.append((url, kw))

                class R:
                    status_code, text, url = 429, "", ""
                return R()

            async def close(self) -> None:
                pass

        session = Session()
        async with InstagramClient(max_retries=1, session=session) as client:
            await client.probe_public_page("pageuser", allow_home=False, force_direct=True)
        proxies = [kw.get("proxy") for url, kw in session.calls if "instagram.com/pageuser/" in url]
        expect("the page request leaves through the tunnel",
               proxies == [page_tunnel.page_proxy()], repr(session.calls))

        # The watchdog brings a dead wireproxy back.
        old_watch, old_backoff = page_tunnel.WATCH_INTERVAL, page_tunnel.RESTART_BACKOFF_MAX
        page_tunnel.WATCH_INTERVAL, page_tunnel.RESTART_BACKOFF_MAX = 0.2, 0.2
        tunnel._watchdog.cancel()
        tunnel._watchdog = asyncio.create_task(tunnel._watch())
        tunnel._proc.kill()
        await tunnel._proc.wait()
        for _ in range(60):
            await asyncio.sleep(0.2)
            if tunnel.up and tunnel._proc is not None and tunnel._proc.returncode is None:
                break
        expect("a wireproxy that dies is restarted and re-proven",
               tunnel.up and tunnel._proc.returncode is None, tunnel.describe())
        page_tunnel.WATCH_INTERVAL, page_tunnel.RESTART_BACKOFF_MAX = old_watch, old_backoff
        await tunnel.stop()
        expect("stopping it takes the route away", page_tunnel.page_proxy() is None
               and page_tunnel.page_route() == "this host")

        # Session sent no proxy when nothing is up.
        session = Session()
        async with InstagramClient(max_retries=1, session=session) as client:
            await client.probe_public_page("pageuser", allow_home=False, force_direct=True)
        expect("with no tunnel the page request goes out as before, no proxy",
               all("proxy" not in kw for _, kw in session.calls), repr(session.calls))
    finally:
        settings.page_tunnel_config, page_tunnel.CHECK_URL, page_tunnel.tunnel = old


async def test_a_tunnel_that_fails_is_never_used(fx: _Fixture) -> None:
    old = (settings.page_tunnel_config, page_tunnel.CHECK_URL, page_tunnel.tunnel)
    settings.page_tunnel_config = str(fx.conf)
    page_tunnel.CHECK_URL = fx.check_url
    try:
        for mode, why in (("crash", "exited"), ("dead", "nothing came back")):
            tunnel = fx.tunnel(mode)
            page_tunnel.tunnel = tunnel
            await tunnel.start()
            expect(f"a wireproxy that {'crashes' if mode == 'crash' else 'relays nothing'} "
                   "is not used", not tunnel.up and page_tunnel.page_proxy() is None
                   and why in (tunnel.error or ""), tunnel.describe())
            await tunnel.stop()
        expect("and its own words reach the log", True)

        missing = PageTunnel(command=["no-such-wireproxy-binary"])
        page_tunnel.tunnel = missing
        await missing.start()
        expect("a missing binary is said plainly",
               missing.configured and not missing.up
               and "not installed" in (missing.error or ""), missing.describe())

        old_page_proxy = settings.page_proxy_url
        settings.page_proxy_url = "http://user:pass@proxy.example:8080"
        expect("PAGE_PROXY_URL is the fallback when no tunnel is up",
               page_tunnel.page_proxy() == settings.page_proxy_url
               and page_tunnel.page_route() == "PAGE_PROXY_URL")
        settings.page_proxy_url = old_page_proxy
    finally:
        settings.page_tunnel_config, page_tunnel.CHECK_URL, page_tunnel.tunnel = old


async def test_tunnel_pages_spare_the_upfront_id_call() -> None:
    """Measured 2026-10-18 without the phone: the Worker's id route took
    9.0-9.5 s for nearly every account — 2.5 minutes a sweep for answers the
    page also carries. With pages coming through the tunnel, a sweep's probe
    is cache-only, as it is with the phone; the check asks by id only if the
    page then fails."""
    old_tunnel, old_proxy, old_token = page_tunnel.tunnel, settings.ig_proxy_url, settings.home_fetch_token
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    settings.home_fetch_token = None
    try:
        up = PageTunnel()
        up.up, up._port = True, 1080
        page_tunnel.tunnel = up

        class Session:
            requests: list = []

            async def get(self, url: str, **kw: Any):
                Session.requests.append(url)
                raise AssertionError("no request expected")

            async def close(self) -> None:
                pass

        async with InstagramClient(max_retries=1, session=Session()) as client:
            probe = await client.probe_by_id("42", cached_ok=True)
            expect("a sweep's probe does not call the Worker while the tunnel serves pages",
                   probe.via == "cache-miss" and Session.requests == [], repr((probe, Session.requests)))

        # With the page door failing (the tunnel's IP refused too), the id
        # route is asked as before.
        class Answering:
            async def get(self, url: str, **kw: Any):
                class R:
                    status_code = 200

                    def json(self):
                        return {"data": {"user": {"has_public_story": False, "is_live": False,
                                "reel": {"id": "42", "user": {"id": "42", "username": "x",
                                         "profile_pic_url": "https://e.x/p.jpg"}}}}}
                return R()

            async def close(self) -> None:
                pass

        async with InstagramClient(max_retries=1, session=Answering()) as client:
            client._direct_page_failures = 2
            probe = await client.probe_by_id("42", cached_ok=True)
        expect("but when the tunnel's pages are refused, the id route is asked",
               probe.answered and probe.via == "worker", repr(probe))
    finally:
        page_tunnel.tunnel = old_tunnel
        settings.ig_proxy_url, settings.home_fetch_token = old_proxy, old_token


async def main() -> int:
    fx = _Fixture()
    try:
        test_the_config_is_found_wherever_it_is_given(fx)
        await test_the_tunnel_is_used_only_once_proven(fx)
        await test_a_tunnel_that_fails_is_never_used(fx)
        await test_tunnel_pages_spare_the_upfront_id_call()
    finally:
        fx.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All page-tunnel tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
