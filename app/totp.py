"""TOTP (RFC 6238, SHA-1, 6 cijfers, 30 s) zonder extra afhankelijkheid."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STAP = 30
CIJFERS = 6


def nieuw_geheim() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _sleutel(geheim: str) -> bytes:
    schoon = geheim.replace(" ", "").upper()
    return base64.b32decode(schoon + "=" * (-len(schoon) % 8))


def code(geheim: str, stap: int) -> str:
    digest = hmac.new(_sleutel(geheim), struct.pack(">Q", stap), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    getal = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(getal % 10**CIJFERS).zfill(CIJFERS)


def huidige_stap(t: float | None = None) -> int:
    return int((time.time() if t is None else t) // STAP)


def controleer(geheim: str, invoer: str, laatste_stap: int | None = None, t: float | None = None) -> int | None:
    """Geeft de gebruikte tijdstap terug bij een geldige code (±1 stap speling), anders None.

    Een code van een tijdstap die al gebruikt is (``laatste_stap``) wordt geweigerd (geen hergebruik).
    """
    invoer = (invoer or "").replace(" ", "").strip()
    if len(invoer) != CIJFERS or not invoer.isdigit():
        return None
    nu = huidige_stap(t)
    for stap in (nu - 1, nu, nu + 1):
        if laatste_stap is not None and stap <= laatste_stap:
            continue
        if hmac.compare_digest(code(geheim, stap), invoer):
            return stap
    return None


def otpauth_uri(geheim: str, account: str, uitgever: str = "Bedrijfsdashboard") -> str:
    label = quote(f"{uitgever}:{account}")
    return f"otpauth://totp/{label}?secret={geheim}&issuer={quote(uitgever)}&algorithm=SHA1&digits=6&period=30"


def opgemaakt(geheim: str) -> str:
    return " ".join(geheim[i : i + 4] for i in range(0, len(geheim), 4))
