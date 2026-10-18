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
page_tunnel.conf. Started at boot and proved end to end — a request through
it must come back — before anything is routed through it. A tunnel that does
not come up leaves the page door exactly as it was and says why; a watchdog
restarts one that dies and re-proves one that went quiet.

This is live data over a different route, never a cache — it does not touch
the rule that a reading must be Instagram's state right now.
"""

from __future__ import annotations

import asyncio
import collections
import os
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
CHECK_TIMEOUT = 20.0
# The watchdog: how often it looks, how long a down tunnel waits between
# re-checks, and the most a crash loop waits between restarts.
WATCH_INTERVAL = 60.0
RECHECK_INTERVAL = 300.0
RESTART_BACKOFF_MAX = 600.0


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


def _config_problem(text: str) -> Optional[str]:
    for needed in ("[Interface]", "[Peer]", "PrivateKey", "Endpoint"):
        if needed not in text:
            return f"the WireGuard config has no {needed}"
    return None


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
        self._output: collections.deque[str] = collections.deque(maxlen=20)
        self._reader: Optional[asyncio.Task] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._restarts = 0
        self.configured = False
        self.up = False
        self.egress_ip: Optional[str] = None
        self.error: Optional[str] = None

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
            return f"up (pages leave from {self.egress_ip})"
        return f"down ({self.error or 'starting'})"

    # -------------------------------------------------------- lifecycle

    async def start(self) -> None:
        config = _load_config()
        if config is None:
            return
        self.configured = True
        problem = _config_problem(config)
        if problem:
            self.error = problem
            logger.error("Page tunnel not started: {}", problem)
            return
        if shutil.which(self._command[0]) is None:
            self.error = f"{self._command[0]} is not installed in this image"
            logger.error("Page tunnel not started: {}", self.error)
            return
        self._dir = Path(tempfile.mkdtemp(prefix="page-tunnel-"))
        os.chmod(self._dir, 0o700)
        wg = self._dir / "wg.conf"
        wg.write_text(config, encoding="utf-8")
        os.chmod(wg, 0o600)  # it holds the tunnel's private key
        self._port = _free_port()
        self._conf = self._dir / "wireproxy.conf"
        self._conf.write_text(
            f"WGConfig = {wg}\n\n[Socks5]\nBindAddress = {_HOST}:{self._port}\n",
            encoding="utf-8",
        )
        if await self._launch():
            await self.check()
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
        back. Only then is anything routed through it."""
        if self._port is None:
            return False
        from curl_cffi.requests import AsyncSession  # the client the page door uses

        url = f"socks5h://{_HOST}:{self._port}"
        try:
            async with AsyncSession(proxy=url, timeout=CHECK_TIMEOUT) as session:
                response = await session.get(CHECK_URL)
            ip = (response.text or "").strip()
            if response.status_code != 200 or not ip or len(ip) > 64:
                raise RuntimeError(f"the check answered HTTP {response.status_code}")
        except Exception as exc:
            was_up = self.up
            self.up = False
            self.error = f"nothing came back through it ({exc})"
            logger.warning(
                "Page tunnel {}: {} — profile pages go out from this host as "
                "before. wireproxy said: {}",
                "lost" if was_up else "is not working", self.error,
                " | ".join(self._output) or "nothing",
            )
            return False
        if not self.up or ip != self.egress_ip:
            logger.info(
                "Page tunnel up — profile-page requests now leave from {} "
                "instead of this host's own IP", ip,
            )
        self.up, self.egress_ip, self.error = True, ip, None
        return True

    # ---------------------------------------------------------- internal

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
        logger.warning(
            "Page tunnel did not start: {}. wireproxy said: {}",
            self.error, " | ".join(self._output) or "nothing",
        )
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

    async def _watch(self) -> None:
        """Restart wireproxy when it dies (backing off on a crash loop), and
        re-prove a tunnel that is down every RECHECK_INTERVAL."""
        since_check = 0.0
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            since_check += WATCH_INTERVAL
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
                if await self._launch() and await self.check():
                    self._restarts = 0
                since_check = 0.0
            elif not self.up and since_check >= RECHECK_INTERVAL:
                await self.check()
                since_check = 0.0


tunnel = PageTunnel()


def page_proxy() -> Optional[str]:
    """The proxy this host's profile-page requests should use, if any: the
    tunnel while it is proven up, else PAGE_PROXY_URL, else None (this
    host's own IP, as before)."""
    return tunnel.proxy_url or settings.page_proxy_url or None


def page_route() -> str:
    """Which way this host's page requests leave, for logs."""
    if tunnel.proxy_url:
        return "the page tunnel"
    if settings.page_proxy_url:
        return "PAGE_PROXY_URL"
    return "this host"
