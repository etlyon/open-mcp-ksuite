"""kSuite's part of the sign-in: no address step, one pasted API token.

kSuite works by account on Infomaniak's own hosts, so there is no server to pick (``NO_ADDRESS``):
the sign-in opens at the token step. The pasted token is checked before anything is kept, with three
read-only requests, one per part the connector uses:

* ``GET https://api.infomaniak.com/2/profile``            who it belongs to (scope ``user_info``);
* ``GET https://mail.infomaniak.com/api/mailbox``          the mailboxes it opens (``workspace:mail``);
* ``GET https://api.infomaniak.com/1/calendar/pim/calendar`` the calendars (``workspace:calendar``).

Infomaniak answers a refused token and a missing scope alike (401 ``not_authorized``): all three
refused means the token is refused; some refused names exactly the missing parts.

The person's own mailbox is pinned by its id at this moment: the mailbox with the profile's address,
else the only one, else the one marked primary. Every mail request is built from that pinned id; no
tool takes a mailbox. An operator may limit the connector to listed mail domains
(``allowed_mail_domains``): checked here, before anything is kept, and again on every call
(``connection_allowed``), so a domain taken off the list stops at once.

No API call lets a token delete itself, so there is no ``on_disconnect``: the person deletes the
token in Infomaniak (the connected page and the README say so).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import idna

from open_connectors import net
from open_connectors.apps.ksuite import api

NAME = "ksuite"
NO_ADDRESS = True                     # the sign-in starts at the token step; nothing to change
SERVER_URL = api.API                  # the connection's server: bound into its sealed secret
TOKEN_PAGE = "https://manager.infomaniak.com/v3/ng/profile/user/token/list"   # Infomaniak's own link (its help pages)
ALLOWED_MAIL_DOMAINS: frozenset[str] = frozenset()   # set once at start from the settings; empty = any mailbox

PROFILE_URL = f"{api.API}/2/profile"
MAILBOXES_URL = f"{api.MAIL_API}/mailbox"
CALENDARS_URL = f"{api.API}/1/calendar/pim/calendar"
MAILBOXES_PARAMS = {"with": "aliases,permissions,accountId,count_users"}   # as Infomaniak's own Mail server asks
PARTS = (("profile", PROFILE_URL, None), ("mail", MAILBOXES_URL, MAILBOXES_PARAMS), ("calendar", CALENDARS_URL, None))

_MAILBOX_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class KsuiteCheckError(Exception):
    """A plain, person-facing refusal. ``code`` selects the words; ``detail`` fills them."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


CheckError = KsuiteCheckError


@dataclass(frozen=True)
class Mailbox:
    uuid: str
    email: str
    name: str = ""                 # the mailbox's local name, as Infomaniak names it
    hosting_id: int | None = None


@dataclass(frozen=True)
class KsuiteIdentity:
    user_id: str
    name: str
    email: str
    mailbox: Mailbox


def mail_domain(address: object) -> str | None:
    """The lower-case ASCII domain of a mail address, or None."""
    if not isinstance(address, str) or address.count("@") != 1:
        return None
    local, domain = address.strip().split("@")
    if not local or not domain or any(ch.isspace() or not ch.isprintable() for ch in address):
        return None
    try:
        ascii_domain = idna.encode(domain.rstrip("."), uts46=True).decode("ascii").lower()
    except idna.IDNAError:
        return None
    return ascii_domain if len(ascii_domain.split(".")) >= 2 else None


def domain_allowed(address: object) -> bool:
    if not ALLOWED_MAIL_DOMAINS:
        return True
    return mail_domain(address) in ALLOWED_MAIL_DOMAINS


def _text(value: object, limit: int = 200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _mailboxes(data: object) -> list[Mailbox]:
    found: list[Mailbox] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        uuid, email = item.get("uuid"), item.get("email")
        if not isinstance(uuid, str) or not _MAILBOX_ID.fullmatch(uuid) or mail_domain(email) is None:
            continue
        hosting = item.get("hosting_id")
        found.append(Mailbox(uuid=uuid, email=email.strip(), name=_text(item.get("mailbox"), 255),
                             hosting_id=hosting if isinstance(hosting, int) and not isinstance(hosting, bool) else None))
    return found


def pick_mailbox(data: object, profile_email: str) -> Mailbox:
    """The person's own mailbox: the one with the profile's address, else the only one, else the primary one."""
    listed = _mailboxes(data)
    if not listed:
        raise KsuiteCheckError("no_mailbox")
    own = [m for m in listed if profile_email and m.email.lower() == profile_email.lower()]
    if len(own) == 1:
        return own[0]
    if len(listed) == 1:
        return listed[0]
    primary = [m for m in listed if any(isinstance(item, dict) and item.get("uuid") == m.uuid and item.get("is_primary") is True
                                        for item in data)]
    if len(primary) == 1:
        return primary[0]
    raise KsuiteCheckError("which_mailbox")


async def _probe(url: str, params: dict[str, str] | None, token: str, denied_hosts: frozenset[str]) -> tuple[str, Any]:
    """("ok", data) or ("refused", None); anything else raises the person-facing refusal at once."""
    try:
        answer = await api.request("GET", url, token=token, params=params, denied_hosts=denied_hosts,
                                   timeout=net.DEFAULT_TIMEOUT, max_bytes=1024 * 1024)
    except (httpx.HTTPError, net.AddressError, api.HostRefused):
        raise KsuiteCheckError("ksuite_unreachable") from None
    if answer.status in (401, 403):
        return "refused", None
    if answer.status == 429:
        raise KsuiteCheckError("ksuite_busy")
    if not 200 <= answer.status < 300:
        raise KsuiteCheckError("ksuite_error", str(answer.status))
    payload = api.envelope(answer)
    if payload is None:
        raise KsuiteCheckError("ksuite_error")
    if payload["result"] != "success":
        return "refused", None                     # a refusal hidden in a 2xx body
    return "ok", payload.get("data")


async def identify(server_url: str, key: str, _extra: str = "", *,
                   denied_hosts: frozenset[str] = frozenset()) -> KsuiteIdentity:
    """Whose token is this, and does it open the profile, Mail and Calendar? Raises :class:`KsuiteCheckError`."""
    if server_url != SERVER_URL:
        raise KsuiteCheckError("bad_request")
    token = api.clean_token(key)
    if token is None:
        raise KsuiteCheckError("bad_token")
    found: dict[str, Any] = {}
    refused: list[str] = []
    for part, url, params in PARTS:
        status, data = await _probe(url, params, token, denied_hosts)
        if status == "refused":
            refused.append(part)
        else:
            found[part] = data
    if len(refused) == len(PARTS):
        raise KsuiteCheckError("token_refused")
    if refused:
        raise KsuiteCheckError("missing_scope", ",".join(refused))
    profile = found["profile"]
    user_id = profile.get("id") if isinstance(profile, dict) else None
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)) or not str(user_id).isdigit():
        raise KsuiteCheckError("ksuite_error")
    email = _text(profile.get("email"), 254)
    name = _text(profile.get("display_name")) or " ".join(
        x for x in (_text(profile.get("first_name")), _text(profile.get("last_name"))) if x)
    mailbox = pick_mailbox(found["mail"], email)
    if not domain_allowed(mailbox.email):
        raise KsuiteCheckError("account_not_listed")
    return KsuiteIdentity(user_id=str(user_id), name=name, email=email if mail_domain(email) else "", mailbox=mailbox)


def connection_allowed(connection: Any) -> bool:
    """False when this connection's mailbox domain is no longer on the operator's list (checked on every call)."""
    return domain_allowed((connection.secret or {}).get("mailbox_email"))


def open_url(_server_url: str) -> str:
    """Where "Open Infomaniak" goes: Infomaniak's own API token page (the person signs in there)."""
    return TOKEN_PAGE


def secret(key: str, identity: KsuiteIdentity) -> dict[str, Any]:
    box = identity.mailbox
    return {"api_token": api.clean_token(key) or "", "mailbox_uuid": box.uuid, "mailbox_email": box.email,
            "mailbox_name": box.name, "hosting_id": box.hosting_id}


def display(identity: KsuiteIdentity) -> dict[str, Any]:
    label = identity.name or identity.email
    if identity.name and identity.email:
        label = f"{identity.name} ({identity.email})"
    return {"name": identity.name, "email": identity.email, "label": label}


def remote_user(identity: KsuiteIdentity) -> str:
    return identity.user_id
