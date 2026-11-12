"""Where downloads are written, and what may be downloaded at all.

Guards for the two places the bot saves what it fetched
(stories.StoriesClient.download and media_hasher.MediaHasher):

- the folder is named after the account, and usernames reach it from more
  than validated commands: a rename read off Instagram's own payloads, a
  handle saveinsta echoes back. "..", which the command pattern accepts, named
  the folder ABOVE the media root (measured 2026-11-05). A name that is not
  plainly one path component is replaced by a stable hash of it;
- the URL comes from a third party's HTML (saveinsta's download links) or
  Instagram's JSON, and the client underneath is libcurl, which also speaks
  file:// and others and connects to 127.0.0.1 for "127.1", "2130706433" or
  "%31%32%37.0.0.1" (measured 2026-11-05). Only http(s) from a public address
  is fetched: checked on every redirect hop, and against the address libcurl
  actually connected to, so a hostile link can neither read a local file nor
  bring back anything from this host's own network.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from app.config import settings

# What a path component built from a username may look like: Instagram's own
# alphabet, and at least one character that is not a dot.
_PLAIN_NAME = re.compile(r"[a-z0-9._]{1,30}")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
# A host name libcurl is given: ASCII labels and nothing else. libcurl decodes
# percent-escapes in a host, and an IDN-aware resolver folds Unicode to ASCII.
_DNS_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
# A label inet_aton reads as a number (decimal, octal or hex), which is how
# libcurl takes "127.1" or "0x7f.0.0.1" for an address.
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+")
# More hops than any CDN link takes; a redirect loop ends here.
_MAX_REDIRECTS = 10


class RefusedURL(Exception):
    """A download the guards stopped; the message says why."""


def account_media_dir(username: str) -> Path:
    """The account's media folder — always directly inside MEDIA_DIR."""
    name = (username or "").strip().lower()
    if not _PLAIN_NAME.fullmatch(name) or not name.strip("."):
        # A stable folder name for input that is no plain handle ("..", a
        # path), not a security use. SHA-256 rather than SHA-1 so CodeQL's
        # weak-hash rule (py/weak-sensitive-data-hashing) has nothing to flag.
        name = "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return settings.media_path / name


def unsafe_media_url(url: Optional[str]) -> Optional[str]:
    """Why `url` must not be downloaded, or None when it may be.

    Judged on what the URL itself says, with no DNS lookup: another scheme, a
    name for this machine, or an address that is not public, written in any
    form libcurl reads as one. A public name that resolves inward is caught by
    fetch_public, on the address the connection actually reached."""
    if not url:
        return "no URL"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "not a URL"
    if parts.scheme.lower() not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'!r} is not http(s)"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "no host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None  # a name, or an address in a form this module can't read
    if address is not None:
        return None if address.is_global else f"address {host} is not a public one"
    if not _DNS_NAME.fullmatch(host):
        return f"host {host!r} is not a plain DNS name"
    if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
        return f"host {host!r} is an address in shorthand"
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return f"host {host!r} is this machine"
    return None


async def fetch_public(session: Any, url: str, **kwargs: Any) -> Any:
    """GET `url` with a curl_cffi session, from public hosts only; raises
    RefusedURL instead.

    Redirects are followed here rather than by libcurl, so every hop passes
    unsafe_media_url, and every answer is checked against the address libcurl
    actually connected to. That last check catches a public name that
    resolves inward, which takes no control of DNS (wildcard services answer
    127.0.0.1 for "127.0.0.1.nip.io"): the request still goes out, but
    nothing it brings back is kept, forwarded or followed."""
    for _ in range(_MAX_REDIRECTS + 1):
        refused = unsafe_media_url(url)
        if refused:
            raise RefusedURL(refused)
        response = await session.get(url, allow_redirects=False, **kwargs)
        refused = _unsafe_peer(response)
        if refused:
            raise RefusedURL(refused)
        location = response.redirect_url if 300 <= response.status_code < 400 else None
        if not location:
            return response
        url = location  # libcurl's own reading of Location: absolute, resolved
    raise RefusedURL(f"more than {_MAX_REDIRECTS} redirects")


def _unsafe_peer(response: Any) -> Optional[str]:
    """Why an answer must be dropped for where it came from, or None."""
    if settings.proxy:
        return None  # the proxy made the connection; the address is the proxy's
    try:
        address = ipaddress.ip_address(response.primary_ip)
    except (TypeError, ValueError):
        return "libcurl reported no address for the connection"
    if not address.is_global:
        return f"the answer came from {address}, which is not a public address"
    return None
