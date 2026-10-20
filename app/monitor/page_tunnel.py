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
over when it stops — or when Instagram keeps refusing its IP. Started at boot
and proved end to end — a request through it must come back — before anything
is routed through it. Boot waits for the first config only; if it gives no
answer the others are tried in the background at once, so silent configs do
not hold the app's port closed. A tunnel that does not come up leaves the
page door exactly as it was and says why — per config, including whether its
server answered the WireGuard handshake.

The watchdog keeps PROVING it, not just keeping the process alive. Measured
2026-10-20: wireproxy kept running while nothing came back through it, and
because the watchdog only re-checked a tunnel already marked down, it stayed
"up" for hours — every profile page through it a 10 s connect timeout, /status
saying "up", and the no-counts notice blaming Instagram. Now an up tunnel is
re-proven every PROVE_INTERVAL, at once when a page request through it gets no
answer at all (`report_failure`), and one whose path is dead is restarted —
a fresh WireGuard handshake from a fresh UDP port — then moved to the next
config.

A path that works is not enough: Instagram judges the IP. When the page door
keeps getting REFUSED through the tunnel — its breaker trips on pages that
came back (a 429, a login redirect, a page with no profile), not on timeouts
— it calls `report_refused`, and the watchdog moves to the next config the
same way, proven before anything is routed through it. At most one lap per
ROTATE_WINDOW: once every config has been refused (or gave no answer when
tried) within it, the tunnel stays put and the page door's cooldown holds.

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
# A config Instagram refused (or one that gave no answer when a refusal sent
# the tunnel to it) is not moved to again for this long — the page door's own
# cooldown (InstagramClient._DIRECT_PAGE_COOLDOWN), so a lap of refused IPs
# costs one lap per cooldown window, not a merry-go-round.
ROTATE_WINDOW = 1800.0


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
        # Set by `report_refused`: (the epoch the refusals were booked on,
        # why). When each config was last refused by Instagram, or gave no
        # answer when a refusal sent the tunnel to it — what keeps rotation
        # to one lap per ROTATE_WINDOW.
        self._refused: Optional[tuple[int, str]] = None
        self._left_at: dict[int, float] = {}
        # Set by start() when the first config gave no answer at boot: the
        # watchdog's first look runs the rest of the lap.
        self._boot_lap = False
        # Whether the last launch opened its port, whether that wireproxy's
        # server answered the WireGuard handshake, and, per config, whether
        # it had when it last gave no answer: "handshake OK, nothing came
        # back" on every config is the account (Proton Free's one
        # connection), not the servers.
        self._opened = False
        self._handshake = False
        self._shook: dict[int, bool] = {}
        self._account_hint_given = False
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
        # comes back on another IP — moving to another config included. The
        # page door books its refusal streak against this: a streak earned on
        # a path that is gone says nothing about the one that replaced it.
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

    def report_refused(self, reason: str, *, epoch: int) -> bool:
        """Instagram keeps refusing pages that came back THROUGH the tunnel —
        the page door's breaker tripped on its answers (a 429, a login
        redirect, a page with no profile). The path works; the IP is what
        Instagram will not serve, and restarting the same config would not
        change it.

        Wakes the watchdog to move to the next config (it alone touches the
        process). `epoch` is the path the refusals were booked on: a report
        about a path already replaced is dropped. True when a move was asked
        for — never with one config, where the page door's cooldown is all
        there is."""
        if self._wake is None or not self.up or len(self._configs) < 2:
            return False
        if epoch != self.epoch:
            return False
        self._refused = (epoch, reason)
        self._wake.set()
        return True

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
        self._wake = asyncio.Event()
        if len(usable) > 1:
            logger.info(
                "Page tunnel: {} configs ({}) — the first that answers is "
                "used, the next takes over when it stops or Instagram keeps "
                "refusing its IP",
                len(usable), ", ".join(_label(c) for c in usable),
            )
        # Boot waits for the FIRST config only. The app does not serve until
        # this returns, and a config that gives no answer costs ~20 s (on
        # Render, 2026-10-21: the DNS lookup inside the tunnel timing out,
        # config after config) — five silent ones held the port closed for
        # ~100 s. The rest of the lap runs in the watchdog at once.
        if await self._switch_to(0):
            pass
        elif len(usable) > 1:
            self._note_no_answer()
            self._boot_lap = True
            self._wake.set()
            others = len(usable) - 1
            logger.info(
                "Page tunnel: {} gave no answer at boot — trying {} in the "
                "background; profile pages go out from this host meanwhile",
                self.label,
                "the other config" if others == 1 else f"its other {others} configs",
            )
        else:
            self._down_reported = True
            self._down_attempts = 1
            logger.warning(
                "Page tunnel did not come up: {} — profile pages go out from "
                "this host as before. wireproxy said: {}",
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
        self._account_hint_given = False
        self._shook.pop(self._index, None)
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

    async def _bring_up(self, *, skip_current: bool = False) -> bool:
        """(Re)start wireproxy and prove it: on the current config first — a
        fresh process is a fresh WireGuard handshake from a fresh UDP port,
        which is all a stalled session needs — then on each other config in
        turn, for a server that has stopped answering altogether. True once
        one answers. `skip_current`: the current config was just tried (the
        boot lap)."""
        first = self._index
        for step in range(1 if skip_current else 0, len(self._configs)):
            if await self._switch_to(first + step):
                if step:
                    logger.warning(
                        "Page tunnel: {} gave no answer, so {} took over",
                        _label(self._configs[first]), self.label,
                    )
                return True
            if len(self._configs) > 1:
                self._note_no_answer()
        self._judge_lap()
        return False

    def _note_no_answer(self) -> None:
        """One line per config that gave no answer in a lap — when several
        fail, only the last one's wireproxy output reaches the WARNING, and
        "the server never answered" (dead, or UDP blocked) reads nothing like
        "the handshake worked and nothing passes" (measured on Render
        2026-10-21: RO-FREE#22 "Received handshake response" and keepalives,
        then the DNS lookup for the IP check timing out)."""
        self._shook[self._index] = self._opened and self._handshake
        verdict = ""
        if self._opened:  # wireproxy ran; it was the path that stayed silent
            verdict = (" — handshake OK, nothing came back" if self._handshake
                       else " — no handshake from the server")
        logger.info("Page tunnel: {} gave no answer ({}){}", self.label, self.error, verdict)

    def _judge_lap(self) -> None:
        """After a lap in which no config answered: if every one of them
        shook hands and then passed nothing, that is not several servers
        failing at once. Said once per outage."""
        count = len(self._configs)
        if count < 2 or self._account_hint_given:
            return
        if len(self._shook) == count and all(self._shook.values()):
            self._account_hint_given = True
            logger.warning(
                "Page tunnel: all {} configs answered the WireGuard handshake "
                "and then passed nothing — that looks like the Proton account, "
                "not the servers. Proton Free allows one connection at a time: "
                "check that no other device (the PC's or the phone's Proton "
                "app) is connected on this account", count,
            )

    async def _switch_to(self, index: int) -> bool:
        """Run config `index` in a fresh wireproxy and prove it."""
        self._use(index)
        await self._terminate()
        self._opened = await self._launch()
        return self._opened and await self.check()

    def _fresh_configs(self) -> list[int]:
        """The other configs a refused one may move to, in file order after
        it: each one not left within ROTATE_WINDOW."""
        now = asyncio.get_running_loop().time()
        count = len(self._configs)
        fresh: list[int] = []
        for step in range(1, count):
            index = (self._index + step) % count
            left = self._left_at.get(index)
            if left is None or now - left >= ROTATE_WINDOW:
                fresh.append(index)
        return fresh

    async def _rotate(self, reason: str) -> bool:
        """Move off a config whose IP Instagram keeps refusing (see
        `report_refused`): to the first fresh config that is proven up, else
        back to this one. False when there was nowhere to go and nothing was
        touched — every other config was left within ROTATE_WINDOW."""
        loop = asyncio.get_running_loop()
        start, old_label, old_ip, epoch = self._index, self.label, self.egress_ip, self.epoch
        self._left_at[start] = loop.time()
        fresh = self._fresh_configs()
        if not fresh:
            logger.info(
                "Page tunnel: Instagram is refusing pages through {} too ({}), "
                "and every other config was refused or gave no answer in the "
                "last {:.0f} min — staying on it; the page door's cooldown holds",
                old_label, reason, ROTATE_WINDOW / 60,
            )
            return False
        # Nothing is routed through it while it switches: the proof of the
        # next config is what lets pages through again (and bumps the epoch,
        # so the page door tries the new IP at once).
        self.up = False
        for index in fresh:
            if await self._switch_to(index):
                logger.warning(
                    "Page tunnel: Instagram kept refusing pages through {} ({}), "
                    "so {} took over — pages now leave from {}",
                    old_label, reason, self.label, self.egress_ip,
                )
                return True
            self._left_at[index] = loop.time()
            self._note_no_answer()
        silent = ", ".join(_label(self._configs[i]) for i in fresh)
        if await self._switch_to(start):
            same = self.egress_ip == old_ip
            if same:
                # Back on the very IP the page door's refusals are about:
                # not a new route, so its cooldown stands. (check() bumped
                # the epoch with no await since, so nothing has seen it.)
                self.epoch = epoch
            logger.warning(
                "Page tunnel: Instagram kept refusing pages through {} ({}), "
                "but {} gave no answer — staying on {}{}",
                old_label, reason, silent, old_label,
                "; the page door's cooldown holds" if same
                else f", now leaving from {self.egress_ip}",
            )
        else:
            logger.warning(
                "Page tunnel: Instagram kept refusing pages through {} ({}), but "
                "{} gave no answer and neither does {} now — profile pages go "
                "out from this host's own IP until one does",
                old_label, reason, silent, old_label,
            )
            self._down_reported = True
            self._still_down(self._recheck_interval())
        return True

    async def _launch(self) -> bool:
        assert self._conf is not None and self._port is not None
        self._output.clear()
        self._handshake = False
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
                if "Received handshake response" in line and proc is self._proc:
                    # Kept apart from _output: on a dead path the keepalives
                    # and DNS retries soon push it out of those 20 lines.
                    self._handshake = True
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
        crash loop). An up tunnel whose IP Instagram keeps refusing moves to
        the next config. An up tunnel is re-proven every PROVE_INTERVAL, and
        at once when a page request through it went unanswered; one that
        fails the proof is restarted, then moved to the next config. A down
        one is restarted on a back-off."""
        if self._boot_lap:
            # Before the crash-loop branch: a first config whose wireproxy
            # exited at boot must not hold the others back for its back-off.
            self._boot_lap = False
            if not await self._bring_up(skip_current=True):
                self._down_reported = True
                self._down_attempts = 1
                logger.warning(
                    "Page tunnel did not come up on any of its {} configs: {} "
                    "— profile pages go out from this host as before. "
                    "wireproxy said: {}", len(self._configs), self.error,
                    " | ".join(self._output) or "nothing",
                )
            return
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
        refused, self._refused = self._refused, None
        if refused is not None and self.up and refused[0] == self.epoch:
            # First: leaving this path also settles a timeout reported on it
            # (the next config is proven before use). Only when there is
            # nowhere to go is the suspect looked at below.
            if await self._rotate(refused[1]):
                return
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
        """Sleep, or wake early when `report_failure` or `report_refused`
        asks."""
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
