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
- the no-counts notice names a tunnel that is down;
- a path that dies while wireproxy keeps running is noticed — by the
  scheduled proof, and at once when a page request through it gets no answer
  — restarted, and handed to the next config (2026-10-20: it stayed "up" for
  hours while every page through it timed out);
- the page door's cooldown, earned on a path that is gone, is forgotten when
  the tunnel comes back.

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
import time
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

# Two configs in one file, as Proton writes them: the server's name is a
# comment under [Peer].
TWO_CONFS = (
    WG_CONF.replace("[Peer]\n", "[Peer]\n# TEST-FREE#1\n")
    + "\n"
    + WG_CONF.replace("[Peer]\n", "[Peer]\n# TEST-FREE#2\n").replace("203.0.113.9", "198.51.100.4")
)

# A stand-in wireproxy: reads "-c <config>", binds the [Socks5] address, and
# relays CONNECTs for real — or, with mode "dead", accepts and drops them,
# or with "crash", exits at once. An endpoint listed in $FAKE_WG_DEAD is a
# path that died under a running process: still accepting, nothing back.
FAKE_WIREPROXY = textwrap.dedent('''\
    import asyncio, os, re, sys
    conf = open(sys.argv[sys.argv.index("-c") + 1]).read()
    host, port = re.search(r"BindAddress = ([\\d.]+):(\\d+)", conf).groups()
    wg = open(re.search(r"WGConfig = (.+)", conf).group(1).strip()).read()
    endpoint = re.search(r"Endpoint = (\\S+)", wg).group(1)
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-c" else "ok"
    if mode == "crash":
        print("fatal: cannot reach the peer", flush=True)
        sys.exit(3)

    def path_dead():
        try:
            return endpoint in open(os.environ["FAKE_WG_DEAD"]).read().split()
        except (KeyError, OSError):
            return False

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        finally:
            w.close()

    async def client(r, w):
        if mode == "dead" or path_dead():
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
        self.two_confs = Path(self.tmp.name) / "two.conf"
        self.two_confs.write_text(TWO_CONFS, encoding="utf-8")
        self.dead = Path(self.tmp.name) / "dead.txt"
        self.dead.write_text("", encoding="utf-8")
        os.environ["FAKE_WG_DEAD"] = str(self.dead)
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


def test_several_configs_in_one_file() -> None:
    configs = page_tunnel._split_configs(TWO_CONFS)
    expect("two configs one after another are read as two", len(configs) == 2, repr(configs))
    expect("each is a whole config",
           all(page_tunnel._config_problem(c) is None for c in configs))
    expect("each is named by the server Proton wrote under [Peer]",
           [page_tunnel._label(c) for c in configs] == ["TEST-FREE#1", "TEST-FREE#2"],
           repr([page_tunnel._label(c) for c in configs]))
    expect("one config is still one config", page_tunnel._split_configs(WG_CONF) == [WG_CONF])
    expect("a config with no server comment is named by its Endpoint, never its key",
           page_tunnel._label(WG_CONF) == "203.0.113.9:51820", page_tunnel._label(WG_CONF))
    curl = ("Failed to perform, curl: (28) Connection timed out after 10000 milliseconds. "
            "See https://curl.se/libcurl/c/libcurl-errors.html first for more details.")
    expect("curl's errors read as one short line",
           page_tunnel.short_error(RuntimeError(curl))
           == "curl: (28) Connection timed out after 10000 milliseconds",
           page_tunnel.short_error(RuntimeError(curl)))


async def _until(condition, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


class _Refusing:
    """A page session that answers every request with a 429."""

    async def get(self, url: str, **kw: Any):
        class R:
            status_code, text, url = 429, "", ""
        return R()

    async def close(self) -> None:
        pass


async def test_a_dead_path_is_restarted_then_replaced(fx: _Fixture) -> None:
    """Measured 2026-10-20: wireproxy kept running while nothing came back
    through it. The watchdog only re-checked a tunnel already marked down, so
    this one stayed "up" for hours: 19 page timeouts in a row, a 30-minute
    cooldown on the page door, /status saying "up", and the no-counts notice
    blaming Instagram. A page request through the tunnel that gets no answer
    now makes the watchdog re-prove the path; a dead one is restarted, then
    moved to the next config in the file."""
    names = ("page_tunnel_config", "CHECK_URL", "CHECK_URL_SECOND", "tunnel",
             "WATCH_INTERVAL", "SUSPECT_MIN_GAP")
    old = {n: getattr(settings if n == "page_tunnel_config" else page_tunnel, n) for n in names}
    settings.page_tunnel_config = str(fx.two_confs)
    page_tunnel.CHECK_URL = page_tunnel.CHECK_URL_SECOND = fx.check_url
    page_tunnel.WATCH_INTERVAL, page_tunnel.SUSPECT_MIN_GAP = 0.05, 0.0
    fx.dead.write_text("", encoding="utf-8")
    try:
        tunnel = fx.tunnel("ok")
        page_tunnel.tunnel = tunnel
        await tunnel.start()
        expect("of two configs, the first that answers is used",
               tunnel.up and tunnel.label == "TEST-FREE#1", tunnel.describe())
        expect("/status names the server in use, and never the key",
               "TEST-FREE#1" in tunnel.describe() and "PrivateKey" not in tunnel.describe()
               and "aGVsbG8" not in tunnel.describe(), tunnel.describe())

        # A timeout on a path that still answers: re-checked, nothing restarted.
        pid, hits = tunnel._proc.pid, _IpCheck.hits
        tunnel.report_failure("timed out after 12s")
        await _until(lambda: _IpCheck.hits > hits, 5.0)
        await asyncio.sleep(0.3)
        expect("a page timeout makes the watchdog re-prove the path at once",
               _IpCheck.hits > hits, repr((hits, _IpCheck.hits)))
        expect("and a path that still answers is left alone",
               tunnel.up and tunnel._proc.pid == pid and tunnel.label == "TEST-FREE#1",
               tunnel.describe())

        async with InstagramClient(max_retries=1, session=_Refusing()) as client:
            # The page door tripped its breaker on the old path.
            client._direct_page_failures = 19
            client._direct_page_blocked_until = time.monotonic() + 1800
            epoch = tunnel.epoch

            # Path 1 dies; wireproxy keeps running and accepting.
            fx.dead.write_text("203.0.113.9:51820", encoding="utf-8")
            tunnel.report_failure("curl: (28) Connection timed out after 10000 milliseconds")
            moved = await _until(lambda: tunnel.up and tunnel.label == "TEST-FREE#2")
            expect("a dead path is restarted and then handed to the next config",
                   moved, tunnel.describe())
            expect("wireproxy itself was restarted",
                   tunnel._proc is not None and tunnel._proc.pid != pid)
            expect("the new path counts as a new route", tunnel.epoch > epoch,
                   repr((epoch, tunnel.epoch)))
            expect("and the cooldown the old path earned is forgotten",
                   not client.direct_page_door_failing and client.direct_page_failures == 0
                   and client._direct_page_blocked_until == 0.0,
                   repr((client._direct_page_failures, client._direct_page_blocked_until)))
        await tunnel.stop()
    finally:
        fx.dead.write_text("", encoding="utf-8")
        for n, value in old.items():
            setattr(settings if n == "page_tunnel_config" else page_tunnel, n, value)


async def test_an_up_tunnel_is_proven_on_a_schedule(fx: _Fixture) -> None:
    """Nothing has to report a dead path for it to be noticed: an up tunnel
    is re-proven every PROVE_INTERVAL. A down one comes back by itself once
    its path answers again."""
    names = ("page_tunnel_config", "CHECK_URL", "CHECK_URL_SECOND", "tunnel",
             "WATCH_INTERVAL", "PROVE_INTERVAL", "RECHECK_INTERVAL")
    old = {n: getattr(settings if n == "page_tunnel_config" else page_tunnel, n) for n in names}
    settings.page_tunnel_config = str(fx.conf)
    page_tunnel.CHECK_URL = page_tunnel.CHECK_URL_SECOND = fx.check_url
    page_tunnel.WATCH_INTERVAL = 0.05
    page_tunnel.PROVE_INTERVAL = page_tunnel.RECHECK_INTERVAL = 0.2
    try:
        tunnel = fx.tunnel("ok")
        page_tunnel.tunnel = tunnel
        await tunnel.start()
        epoch = tunnel.epoch
        fx.dead.write_text("203.0.113.9:51820", encoding="utf-8")
        lost = await _until(lambda: not tunnel.up)
        expect("a path that dies under a running wireproxy is marked down by the "
               "scheduled proof alone",
               lost and page_tunnel.page_proxy() is None
               and "nothing came back" in (tunnel.error or ""), tunnel.describe())
        fx.dead.write_text("", encoding="utf-8")
        back = await _until(lambda: tunnel.up)
        expect("and it comes back by itself once the path answers",
               back and tunnel.epoch > epoch and page_tunnel.page_proxy() is not None,
               tunnel.describe())
        await tunnel.stop()
    finally:
        fx.dead.write_text("", encoding="utf-8")
        for n, value in old.items():
            setattr(settings if n == "page_tunnel_config" else page_tunnel, n, value)


async def test_only_an_unanswered_page_request_suspects_the_tunnel() -> None:
    """A 429 or a login redirect came back THROUGH the tunnel, which proves
    the path. Only a request that got no answer at all says it may be dead."""
    old_tunnel = page_tunnel.tunnel
    try:
        up = PageTunnel()
        up.up, up._port = True, 1080
        reports: list[str] = []
        up.report_failure = reports.append  # type: ignore[method-assign]
        page_tunnel.tunnel = up

        class Timeout(Exception):
            pass

        class Unanswered:
            async def get(self, url: str, **kw: Any):
                raise Timeout(
                    "Failed to perform, curl: (28) Connection timed out after 10000 "
                    "milliseconds. See https://curl.se/libcurl/c/libcurl-errors.html "
                    "first for more details."
                )

            async def close(self) -> None:
                pass

        async with InstagramClient(max_retries=1, session=Unanswered()) as client:
            out = await client.probe_public_page("pageuser", allow_home=False, force_direct=True)
        expect("a page request through the tunnel that got no answer is reported to it",
               reports == ["curl: (28) Connection timed out after 10000 milliseconds"],
               repr(reports))
        expect("and its error reads as one line, not curl's paragraph",
               out["error"] == "Timeout: curl: (28) Connection timed out after 10000 milliseconds",
               repr(out["error"]))

        reports.clear()
        async with InstagramClient(max_retries=1, session=_Refusing()) as client:
            await client.probe_public_page("pageuser", allow_home=False, force_direct=True)
        expect("a 429 through the tunnel does not suspect it", reports == [], repr(reports))

        up.up = False
        async with InstagramClient(max_retries=1, session=Unanswered()) as client:
            await client.probe_public_page("pageuser", allow_home=False, force_direct=True)
        expect("nor does a timeout from this host's own IP", reports == [], repr(reports))
    finally:
        page_tunnel.tunnel = old_tunnel


async def main() -> int:
    fx = _Fixture()
    try:
        test_the_config_is_found_wherever_it_is_given(fx)
        test_several_configs_in_one_file()
        await test_the_tunnel_is_used_only_once_proven(fx)
        await test_a_tunnel_that_fails_is_never_used(fx)
        await test_tunnel_pages_spare_the_upfront_id_call()
        await test_a_dead_path_is_restarted_then_replaced(fx)
        await test_an_up_tunnel_is_proven_on_a_schedule(fx)
        await test_only_an_unanswered_page_request_suspects_the_tunnel()
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
