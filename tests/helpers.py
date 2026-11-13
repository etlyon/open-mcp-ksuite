"""Shared test helpers (small utilities; no Hub package dependency)."""
from __future__ import annotations

import base64
import hashlib
import html as htmllib
import re
import secrets


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def csrf_of(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def back_link(page: str) -> str:
    return htmllib.unescape(re.search(r'<a class="primary" href="([^"]+)"', page).group(1))


def visible(page: str) -> str:
    page = re.sub(r'<p class="foot">.*?</p>', " ", page, flags=re.S)
    return " ".join(re.sub(r"<style>.*?</style>|<[^>]+>", " ", page, flags=re.S).split())
