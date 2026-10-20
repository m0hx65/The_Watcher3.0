"""The page tunnel — this host's profile-page requests, through a VPN.

Why it exists (measured 2026-10-18): Instagram sends every profile-page
request from Render's IP to its login page (the 416 KB "page" is the 417 KB
/accounts/login/?next= page), the Cloudflare Worker's egress is answered 429,
and the username API is login-walled everywhere. The page is the one
anonymous source of follower/following counts, the bio and the privacy flag,
and Instagram serves it by IP REPUTATION: the owner's PC got full pages
through Proton VPN (M247, Bucharest) — a datacenter network like Render's,
just not a burned one. So the fix is not "a home connection", it is "an IP
Instagram has nothing against", and that can be carried to the bot.

How: wireproxy (github.com/windtf/wireproxy, compiled into the image by the
Dockerfile) runs WireGuard entirely in user space — no root, no network
interface, nothing the host has to grant — and exposes a SOCKS5 proxy on
localhost. Only the profile-page request goes through it; the Worker, the
reel queries, saveinsta and Telegram leave exactly as before.

Configured with a standard WireGuard .conf (Proton VPN: account.protonvpn.com
→ Downloads → WireGuard configuration; the free plan has it), given as
PAGE_TUNNEL_CONFIG (the text, or a path) or as a Render Secret File named
page_tunnel.conf. Several configs may be given one after another (each from
its own [Interface] line): the first that answers is used, and the next takes
over when it stops. Started at boot and proved end to end — a request through
it must come back — before anything is routed through it. A tunnel that does
not come up leaves the page door exactly as it was and says why.

The watchdog keeps PROVING it, not just keeping the process alive. Measured
2026-10-20: wireproxy kept running while nothing came back through it, and
because the watchdog only re-checked a tunnel already marked down, it stayed
"up" for hours — every profile page through it a 10 s connect timeout, /status
saying "up", and the no-counts notice blaming Instagram. Now an up tunnel is
re-proven every PROVE_INTERVAL, at once when a page request through it gets no
answer at all (`report_failure`), and one whose path is dead is restarted —
a fresh WireGuard handshake from a fresh UDP port — then moved to the next
config.

This is live data over a different route, never a cache — it does not touch
the rule that a reading must be Instagram's state right now.
"""

from __future__ import annotations

import asyncio
import collections
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Optional

from app.config import settings
from app.utils.logger import logger

# Where Render mounts a Secret File of this name.
SECRET_FILE = Path("/etc/secrets/page_tunnel.conf")
BINARY = "wireproxy"
_HOST = "127.0.0.1"
# How long wireproxy gets to open its SOCKS port after launch.
START_TIMEOUT = 15.0
# The end-to-end check: a request through the tunnel that answers with the
# IP it arrived from — which is also the line the log needs ("pages now
# leave from X"). Nothing about any Instagram account is sent anywhere.
CHECK_URL = "https://api.ipify.org"
# A second witness, asked only when the first gives no answer: a dead path
# restarts wireproxy and can move to another config, so one IP-check site
# being down must not read as the tunnel being down.
CHECK_URL_SECOND = "https://icanhazip.com"
CHECK_TIMEOUT = 20.0
# The watchdog: how often it looks, how often an UP tunnel is re-proven, how
# long a down tunnel waits between re-checks, and the most a crash loop waits
# between restarts.
WATCH_INTERVAL = 60.0
PROVE_INTERVAL = 600.0
RECHECK_INTERVAL = 300.0
RECHECK_INTERVAL_MAX = 1800.0
RESTART_BACKOFF_MAX = 600.0
# A page timeout this soon after the tunnel was proven is not re-proven: the
# path answered moments ago, so the timeout was Instagram's side.
SUSPECT_MIN_GAP = 30.0


def _load_config() -> Optional[str]:
    """The WireGuard config text: PAGE_TUNNEL_CONFIG (the text itself, or a
    path to the file), else the Render Secret File. None when there is none."""
    raw = (settings.page_tunnel_config or "").strip()
    if raw:
        if "[Interface]" in raw:
            # Pasted into a one-line variable, the newlines may arrive as
            # the two characters backslash-n.
            return raw if "\n" in raw else raw.replace("\\n", "\n")
        path = Path(raw)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        logger.error("PAGE_TUNNEL_CONFIG is neither a WireGuard config nor a file that exists")
        return None
    if SECRET_FILE.is_file():
        return SECRET_FILE.read_text(encoding="utf-8")
    return None


def _split_configs(text: str) -> list[str]:
    """One config, or several one after another — each starts at its
    [Interface] line. Text with no [Interface] at all comes back whole, so
    `_config_problem` can say what is missing."""
    parts = [p.strip() for p in re.split(r"(?m)^(?=[ \t]*\[Interface\])", text)]
    configs = [p + "\n" for p in parts if "[Interface]" in p]
    return configs or [text]


def _config_problem(text: str) -> Optional[str]:
    for needed in ("[Interface]", "[Peer]", "PrivateKey", "Endpoint"):
        if needed not in text:
            return f"the WireGuard config has no {needed}"
    return None


def _label(config: str) -> str:
    """A name for one config, for logs: the server Proton writes as a comment
    under [Peer] ("# RO-FREE#22"), else its Endpoint. Never the key."""
    peer = config.split("[Peer]", 1)[-1]
    for line in peer.splitlines():
        line = line.strip()
        if line.startswith("#") and "=" not in line:
            name = line.lstrip("#").strip()
            if name:
                return name[:40]
    match = re.search(r"(?m)^\s*Endpoint\s*=\s*(\S+)", peer)
    return match.group(1) if match else "the config"


def short_error(exc: BaseException) -> str:
    """An exception as one short line. curl_cffi's carry a "Failed to
    perform," prefix and a link to curl's error table that say nothing."""
    text = str(exc) or type(exc).__name__
    text = text.split(" See https://curl.se/", 1)[0]
    text = text.removeprefix("Failed to perform, ").strip().rstrip(".")
    return text[:200]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_HOST, 0))
        return probe.getsockname()[1]


class PageTunnel:
    def __init__(self, command: Optional[list[str]] = None) -> None:
        # What to run; "-c <config>" is appended. Tests stand a fake in.
        self._command = list(command or [BINARY])
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._dir: Optional[Path] = None
        self._conf: Optional[Path] = None
        self._port: Optional[int] = None
        self._configs: list[str] = []
        self._index = 0
        self._output: collections.deque[str] = collections.deque(maxlen=20)
        self._reader: Optional[asyncio.Task] = None
        self._watchdog: Optional[asyncio.Task] = None
        # Set by `report_failure` to wake the watchdog before its next look.
        self._wake: Optional[asyncio.Event] = None
        self._suspect: Optional[str] = None
        self._checked_at = 0.0
        self._restarts = 0
        # One outage is one WARNING: the re-tries after it log at INFO, on a
        # back-off, so an evening-long outage does not bury the sweeps' log.
        self._down_reported = False
        self._down_attempts = 0
        self.configured = False
        self.up = False
        self.egress_ip: Optional[str] = None
        self.error: Optional[str] = None
        self.label: Optional[str] = None
        # Bumped every time the tunnel is proven up after not being up, or
        # comes back on another IP. The page door books its refusal streak
        # against this: a streak earned on a path that is gone says nothing
        # about the one that replaced it.
        self.epoch = 0

    # ----------------------------------------------------------- state

    @property
    def proxy_url(self) -> Optional[str]:
        """socks5h:// (names resolve inside the tunnel too), or None while
        the tunnel is not proven up."""
        if not self.up or self._port is None:
            return None
        return f"socks5h://{_HOST}:{self._port}"

    def describe(self) -> str:
        if not self.configured:
            return "not configured"
        if self.up:
            via = f" via {self.label}" if self.label else ""
            return f"up (pages leave from {self.egress_ip}{via})"
        return f"down ({self.error or 'starting'})"

    def report_failure(self, reason: str) -> None:
        """A page request through the tunnel got no answer at all — a
        timeout, a refused or reset connection. Not an HTTP refusal: a 429 or
        a login redirect came back through the path, which proves it works.

        Wakes the watchdog to re-prove the path now instead of at its next
        scheduled look, so a dead path costs the sweep one account's timeout
        rather than every account's."""
        if self._wake is None or not self.up:
            return
        self._suspect = reason
        self._wake.set()

    # -------------------------------------------------------- lifecycle

    async def start(self) -> None:
        text = _load_config()
        if text is None:
            return
        self.configured = True
        configs = _split_configs(text)
        usable: list[str] = []
        for number, config in enumerate(configs, 1):
            problem = _config_problem(config)
            if problem is None:
                usable.append(config)
            elif len(configs) > 1:
                logger.error(
                    "Page tunnel: config {} of {} skipped — {}",
                    number, len(configs), problem,
                )
            else:
                self.error = problem
        if not usable:
            self.error = self.error or "none of its WireGuard configs is complete"
            logger.error("Page tunnel not started: {}", self.error)
            return
        if shutil.which(self._command[0]) is None:
            self.error = f"{self._command[0]} is not installed in this image"
            logger.error("Page tunnel not started: {}", self.error)
            return
        self._configs = usable
        self._dir = Path(tempfile.mkdtemp(prefix="page-tunnel-"))
        os.chmod(self._dir, 0o700)
        self._port = _free_port()
        self._conf = self._dir / "wireproxy.conf"
        self._use(0)
        self._wake = asyncio.Event()
        if len(usable) > 1:
            logger.info(
                "Page tunnel: {} configs ({}) — the first that answers is "
                "used, the next takes over when it stops",
                len(usable), ", ".join(_label(c) for c in usable),
            )
        if not await self._bring_up():
            self._down_reported = True
            self._down_attempts = 1
            logger.warning(
                "Page tunnel did not come up{}: {} — profile pages go out from "
                "this host as before. wireproxy said: {}",
                f" on any of its {len(usable)} configs" if len(usable) > 1 else "",
                self.error, " | ".join(self._output) or "nothing",
            )
        self._watchdog = asyncio.create_task(self._watch(), name="page-tunnel:watchdog")

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        await self._terminate()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
        self.up = False

    async def check(self) -> bool:
        """Prove the path end to end: a request through the tunnel must come
        back. Only then is anything routed through it.

        Logs the moment the tunnel comes up and the moment it is lost; a
        failure while it is already down is the caller's to report, once."""
        if self._port is None:
            return False
        ip: Optional[str] = None
        failure: Optional[BaseException] = None
        for url, timeout in ((CHECK_URL, CHECK_TIMEOUT), (CHECK_URL_SECOND, CHECK_TIMEOUT / 2)):
            try:
                ip = await self._ask_ip(url, timeout)
                break
            except Exception as exc:
                failure = failure or exc
        self._checked_at = asyncio.get_running_loop().time()
        if ip is None:
            was_up = self.up
            self.up = False
            self.error = f"nothing came back through it ({short_error(failure)})"
            if was_up:
                self._down_reported = True
                logger.warning(
                    "Page tunnel lost ({}): {} — restarting it; profile pages go "
                    "out from this host's own IP meanwhile. wireproxy said: {}",
                    self.label, self.error, " | ".join(self._output) or "nothing",
                )
            return False
        if not self.up or ip != self.egress_ip:
            self.epoch += 1
            logger.info(
                "Page tunnel up — profile-page requests now leave from {} ({}) "
                "instead of this host's own IP", ip, self.label,
            )
        self.up, self.egress_ip, self.error = True, ip, None
        self._down_reported, self._down_attempts = False, 0
        return True

    # ---------------------------------------------------------- internal

    async def _ask_ip(self, url: str, timeout: float) -> str:
        from curl_cffi.requests import AsyncSession  # the client the page door uses

        proxy = f"socks5h://{_HOST}:{self._port}"
        async with AsyncSession(proxy=proxy, timeout=timeout) as session:
            response = await session.get(url)
        ip = (response.text or "").strip()
        if response.status_code != 200 or not ip or len(ip) > 64:
            raise RuntimeError(f"the check answered HTTP {response.status_code}")
        return ip

    def _use(self, index: int) -> None:
        """Write config `index` where wireproxy reads it."""
        assert self._dir is not None and self._conf is not None and self._port is not None
        self._index = index % len(self._configs)
        config = self._configs[self._index]
        self.label = _label(config)
        wg = self._dir / "wg.conf"
        wg.write_text(config, encoding="utf-8")
        os.chmod(wg, 0o600)  # it holds the tunnel's private key
        self._conf.write_text(
            f"WGConfig = {wg}\n\n[Socks5]\nBindAddress = {_HOST}:{self._port}\n",
            encoding="utf-8",
        )

    async def _bring_up(self) -> bool:
        """(Re)start wireproxy and prove it: on the current config first — a
        fresh process is a fresh WireGuard handshake from a fresh UDP port,
        which is all a stalled session needs — then on each other config in
        turn, for a server that has stopped answering altogether. True once
        one answers."""
        first = self._index
        for step in range(len(self._configs)):
            if step:
                self._use(first + step)
            await self._terminate()
            if await self._launch() and await self.check():
                if step:
                    logger.warning(
                        "Page tunnel: {} gave no answer, so {} took over",
                        _label(self._configs[first]), self.label,
                    )
                return True
        return False

    async def _launch(self) -> bool:
        assert self._conf is not None and self._port is not None
        self._output.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *self._command, "-c", str(self._conf),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self._reader = asyncio.create_task(self._read_output(self._proc), name="page-tunnel:output")
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if self._proc.returncode is not None:
                break
            try:
                _reader, writer = await asyncio.open_connection(_HOST, self._port)
            except OSError:
                await asyncio.sleep(0.2)
                continue
            writer.close()
            return True
        self.up = False
        self.error = (
            f"wireproxy exited (code {self._proc.returncode})"
            if self._proc.returncode is not None
            else f"wireproxy did not open its port within {START_TIMEOUT:.0f}s"
        )
        logger.debug("Page tunnel ({}) did not start: {}", self.label, self.error)
        return False

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._output.append(line[:300])
                logger.debug("wireproxy: {}", line[:300])

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
        self._proc = None

    def _recheck_interval(self) -> float:
        """How long a down tunnel waits before the next restart: doubling
        per failed one, from RECHECK_INTERVAL up to RECHECK_INTERVAL_MAX."""
        return min(
            RECHECK_INTERVAL_MAX,
            RECHECK_INTERVAL * 2 ** max(0, self._down_attempts - 1),
        )

    def _still_down(self, next_in: float) -> None:
        self._down_attempts += 1
        configs = len(self._configs)
        logger.log(
            "INFO" if self._down_reported else "WARNING",
            "Page tunnel still down after restarting wireproxy{}: {} — profile "
            "pages go out from this host's own IP until it answers; next try "
            "in {:.0f} min. wireproxy said: {}",
            f" on each of its {configs} configs" if configs > 1 else "",
            self.error, next_in / 60, " | ".join(self._output) or "nothing",
        )
        self._down_reported = True

    async def _tend(self) -> None:
        """One look. A wireproxy that exited is restarted (backing off on a
        crash loop). An up tunnel is re-proven every PROVE_INTERVAL, and at
        once when a page request through it went unanswered; one that fails
        the proof is restarted, then moved to the next config. A down one is
        restarted on a back-off."""
        if self._proc is None or self._proc.returncode is not None:
            code = self._proc.returncode if self._proc is not None else None
            self.up = False
            self.error = f"wireproxy exited (code {code})"
            self._restarts += 1
            wait = min(RESTART_BACKOFF_MAX, WATCH_INTERVAL * (2 ** min(self._restarts, 4)))
            logger.warning(
                "Page tunnel: {} — restarting it in {:.0f}s. wireproxy said: {}",
                self.error, wait, " | ".join(self._output) or "nothing",
            )
            await asyncio.sleep(wait)
            if await self._bring_up():
                self._restarts = 0
            else:
                self._still_down(
                    WATCH_INTERVAL
                    + min(RESTART_BACKOFF_MAX, WATCH_INTERVAL * (2 ** min(self._restarts + 1, 4)))
                )
            return
        suspect, self._suspect = self._suspect, None
        since = asyncio.get_running_loop().time() - self._checked_at
        if not self.up:
            if since < self._recheck_interval():
                return
        elif suspect is not None:
            if since < SUSPECT_MIN_GAP:
                return  # proven moments ago: that timeout was Instagram's
        elif since < PROVE_INTERVAL:
            return
        if self.up:
            if await self.check():
                if suspect is not None:
                    logger.info(
                        "Page tunnel answers (pages leave from {}) — the page "
                        "request that got no answer ({}) failed on Instagram's "
                        "side, not the tunnel's", self.egress_ip, suspect,
                    )
                return
        if not await self._bring_up():
            self._still_down(self._recheck_interval())

    async def _watch(self) -> None:
        """Keep the tunnel honest (see _tend). An error never ends it: a
        watchdog that has stopped is how a dead tunnel stays "up"."""
        while True:
            await self._nap(WATCH_INTERVAL)
            try:
                await self._tend()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Page tunnel watchdog: {}", exc)

    async def _nap(self, seconds: float) -> None:
        """Sleep, or wake early when `report_failure` asks."""
        if self._wake is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()


tunnel = PageTunnel()


def page_proxy() -> Optional[str]:
    """The proxy this host's profile-page requests should use, if any: the
    tunnel while it is proven up, else PAGE_PROXY_URL, else None (this
    host's own IP, as before)."""
    return tunnel.proxy_url or settings.page_proxy_url or None


# The route name page requests through the tunnel are logged under.
TUNNEL_ROUTE = "the page tunnel"


def page_route() -> str:
    """Which way this host's page requests leave, for logs."""
    if tunnel.proxy_url:
        return TUNNEL_ROUTE
    if settings.page_proxy_url:
        return "PAGE_PROXY_URL"
    return "this host"
