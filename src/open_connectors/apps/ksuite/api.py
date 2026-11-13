"""The one road from the kSuite connector to Infomaniak.

Infomaniak serves kSuite on two hosts, both ``https`` on port 443, and the connector talks to these
two and to nothing else:

* ``api.infomaniak.com``  its public API: the person's profile (``/2/profile``) and Calendar
  (``/1/calendar/pim/...``);
* ``mail.infomaniak.com`` Mail (``/api/mailbox``, ``/api/mail/<mailbox>/...``): the API that
  Infomaniak's own Mail MCP server uses. The public API has no message, folder, draft or send
  endpoint, only mailbox administration.

Every address is built here or by the tools from these constants and checked ids; an address found
in an answer is never followed. The host is checked here before anything is sent (tests replace the
transport, so this check never relies on it), and the guarded transport of :mod:`open_connectors.net`
refuses any other host again, before any DNS lookup.

Each request carries exactly one token, the one the caller passes (the calling connection's own), as
``Authorization: Bearer``. Nothing here logs, and no error carries a token or an upstream body.

Infomaniak allows 60 requests a minute on its API. :class:`Throttle` keeps each connection under that,
counting every upstream request (one tool call may make several), before the request is sent.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from open_connectors import net

API_HOST = "api.infomaniak.com"
MAIL_HOST = "mail.infomaniak.com"
ALLOWED_HOSTS = frozenset({API_HOST, MAIL_HOST})
API = f"https://{API_HOST}"
MAIL_API = f"https://{MAIL_HOST}/api"
USER_AGENT = "Assistant connector"
JSON_MAX_BYTES = 4 * 1024 * 1024
RATE_LIMIT = 60
RATE_WINDOW = 60.0

TRANSPORT: httpx.AsyncBaseTransport | None = None   # tests only (an in-process fake Infomaniak)

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class HostRefused(ValueError):
    """An address outside Infomaniak's two kSuite hosts (or not https on 443): nothing was sent."""

    def __init__(self) -> None:
        super().__init__("only api.infomaniak.com and mail.infomaniak.com over https are reached; nothing was sent")


class NoToken(ValueError):
    """The caller holds no usable token: nothing was sent."""


class TooManyRequests(Exception):
    """This connection's share of Infomaniak's limit is used up for now: nothing was sent."""

    def __init__(self, wait: float):
        super().__init__("too many requests")
        self.wait = max(1, int(wait + 0.999))


class InfomaniakError(Exception):
    """Infomaniak refused or failed a request. ``status`` is the HTTP status; ``code`` its error code, if a plain one."""

    def __init__(self, status: int, code: str = ""):
        super().__init__(f"Infomaniak answered {status}")
        self.status = status
        self.code = code if _CODE.fullmatch(code or "") else ""


class Pending(Exception):
    """Infomaniak accepted a request but answered that it is still running: its outcome is not known."""


class Unreadable(Exception):
    """Infomaniak answered 2xx without its usual envelope (empty, HTML, or JSON without a known ``result``).

    The request reached Infomaniak and may have been done: for a write its outcome is not known.
    """

    def __init__(self, status: int):
        super().__init__(f"Infomaniak answered {status} without a readable envelope")
        self.status = status


class Throttle:
    """A sliding window of ``limit`` upstream requests per ``window`` seconds, per key (a connection)."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Count one request for ``key`` and return 0, or return the seconds to wait (nothing counted)."""
        now = self.clock()
        with self._lock:
            for other in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                del self._hits[other]                     # forget idle connections
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return self.window - (now - hits[0])
            hits.append(now)
            return 0.0


THROTTLE = Throttle()


@dataclass(frozen=True)
class Answer:
    status: int
    headers: httpx.Headers
    body: bytes


def check_url(url: str) -> None:
    """Refuse any address that is not https on 443 at one of the two kSuite hosts."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise HostRefused() from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or port not in (None, 443) or host not in ALLOWED_HOSTS or parts.username is not None:
        raise HostRefused()


def clean_token(value: object) -> str | None:
    """The pasted token as sent in the Authorization header, or None: printable ASCII, no spaces, 16-512 chars."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not 16 <= len(token) <= 512 or not token.isascii() or any(not ch.isprintable() or ch.isspace() for ch in token):
        return None
    return token


async def request(method: str, url: str, *, token: str, throttle_key: str | None = None,
                  params: dict[str, Any] | None = None, json_body: Any = None, content: bytes | None = None,
                  headers: dict[str, Any] | None = None, timeout: float = 30.0, max_bytes: int = JSON_MAX_BYTES,
                  denied_hosts: frozenset[str] = frozenset()) -> Answer:
    """One request to Infomaniak with ``token``. Raises HostRefused, NoToken or TooManyRequests before anything
    is sent, httpx errors when the request did not complete, net.AddressError when the answer is too large."""
    check_url(url)
    token = clean_token(token) or ""
    if not token:
        raise NoToken("this connection holds no Infomaniak token")
    if throttle_key is not None:
        wait = THROTTLE.take(throttle_key)
        if wait:
            raise TooManyRequests(wait)
    sent_headers: dict[str, Any] = {"Accept": "application/json", **(headers or {}), "Authorization": f"Bearer {token}"}
    if json_body is not None:
        content = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sent_headers.setdefault("Content-Type", "application/json")
    async with net.client_for(timeout=timeout, denied_hosts=denied_hosts, transport=TRANSPORT,
                              allowed_hosts=ALLOWED_HOSTS, headers={"User-Agent": USER_AGENT}) as client:
        async with client.stream(method, url, params=params, content=content, headers=sent_headers) as response:
            body = await net.read_capped(response, max_bytes)
            return Answer(response.status_code, response.headers, body)


def envelope(answer: Answer) -> dict[str, Any] | None:
    """Infomaniak's JSON envelope (``{"result": …, "data": …}``), or None when the body is not one."""
    try:
        payload = json.loads(answer.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("result"), str) else None


def data_of(answer: Answer) -> Any:
    """The ``data`` of a successful answer. A refusal can hide in a 2xx body: only ``result: success`` is one.

    A 2xx refusal counts only when it is explicit (``result: error`` with a plain error code); any other
    2xx body raises :class:`Unreadable`, because the request may have been done.
    """
    payload = envelope(answer)
    code = ""
    if payload is not None and isinstance(payload.get("error"), dict):
        code = str(payload["error"].get("code") or "")
    if not 200 <= answer.status < 300:
        raise InfomaniakError(answer.status, code)
    if payload is None:
        raise Unreadable(answer.status)
    if payload["result"] == "success":
        return payload.get("data")
    if payload["result"] == "asynchronous":
        raise Pending("Infomaniak accepted the request and has not finished it")
    if payload["result"] == "error" and _CODE.fullmatch(code):
        raise InfomaniakError(answer.status, code)
    raise Unreadable(answer.status)
