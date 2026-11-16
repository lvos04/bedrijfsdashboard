"""Wachtwoorden, sessies, CSRF/oorsprong en aanmeldbeperking."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request

from .tijd import iso, nu, parse_iso

SESSIE_COOKIE = "bd_sessie"
SESSIE_DUUR = timedelta(days=14)
MIN_WACHTWOORD = 12

_hasher = PasswordHasher()
# Hash om tijd-verschil tussen bestaande/niet-bestaande gebruikers te maskeren.
_DUMMY_HASH = _hasher.hash("dummy-wachtwoord-voor-timing")


def hash_wachtwoord(wachtwoord: str) -> str:
    return _hasher.hash(wachtwoord)


def controleer_wachtwoord(hash_: str | None, wachtwoord: str) -> bool:
    try:
        return _hasher.verify(hash_ or _DUMMY_HASH, wachtwoord) and hash_ is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def wachtwoord_fout(wachtwoord: str, herhaling: str | None = None) -> str | None:
    if len(wachtwoord) < MIN_WACHTWOORD:
        return f"Het wachtwoord moet minimaal {MIN_WACHTWOORD} tekens lang zijn."
    if herhaling is not None and wachtwoord != herhaling:
        return "De wachtwoorden komen niet overeen."
    return None


def sha256(tekst: str) -> str:
    return hashlib.sha256(tekst.encode("utf-8")).hexdigest()


def maak_sessie(conn, gebruiker_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessies (token_hash, gebruiker_id, csrf_token, aangemaakt_op, verloopt_op) "
        "VALUES (?, ?, ?, ?, ?)",
        (sha256(token), gebruiker_id, secrets.token_urlsafe(32), iso(), iso(nu() + SESSIE_DUUR)),
    )
    conn.execute("DELETE FROM sessies WHERE verloopt_op < ?", (iso(),))
    return token


def haal_sessie(conn, token: str | None):
    if not token:
        return None
    rij = conn.execute(
        "SELECT s.token_hash, s.csrf_token, s.verloopt_op, g.id, g.gebruikersnaam, g.rol "
        "FROM sessies s JOIN gebruikers g ON g.id = s.gebruiker_id WHERE s.token_hash = ?",
        (sha256(token),),
    ).fetchone()
    if rij is None:
        return None
    verloopt = parse_iso(rij["verloopt_op"])
    if verloopt is None or verloopt < nu():
        conn.execute("DELETE FROM sessies WHERE token_hash = ?", (rij["token_hash"],))
        return None
    return rij


def verwijder_sessie(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessies WHERE token_hash = ?", (sha256(token),))


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def oorsprong_ok(request: Request) -> bool:
    """Muterende verzoeken moeten van dezelfde host komen (Origin, anders Referer)."""
    bron = request.headers.get("origin") or request.headers.get("referer")
    if not bron:
        # Browsers sturen bij POST altijd Origin; ontbreekt die, dan vertrouwen we alleen het CSRF-token.
        return True
    if bron == "null":
        return False
    host_bron = urlsplit(bron).netloc.lower()
    toegestaan = {request.headers.get("host", "").lower()}
    fwd = request.headers.get("x-forwarded-host")
    if fwd:
        toegestaan.add(fwd.split(",")[0].strip().lower())
    return host_bron in toegestaan


def csrf_ok(verwacht: str, gegeven: str | None) -> bool:
    return bool(gegeven) and hmac.compare_digest(verwacht, gegeven or "")


class AanmeldBeperking:
    """Eenvoudige in-geheugen beperking: max N mislukte pogingen per venster per IP en per gebruikersnaam."""

    def __init__(self, max_pogingen: int = 5, venster_s: int = 900):
        self.max = max_pogingen
        self.venster = venster_s
        self._fouten: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _schoon(self, sleutel: str, t: float) -> list[float]:
        lijst = [x for x in self._fouten.get(sleutel, []) if t - x < self.venster]
        self._fouten[sleutel] = lijst
        return lijst

    def geblokkeerd(self, *sleutels: str) -> bool:
        t = time.monotonic()
        with self._lock:
            return any(len(self._schoon(s, t)) >= self.max for s in sleutels)

    def fout(self, *sleutels: str) -> None:
        t = time.monotonic()
        with self._lock:
            for s in sleutels:
                self._schoon(s, t).append(t)

    def reset(self, *sleutels: str) -> None:
        with self._lock:
            for s in sleutels:
                self._fouten.pop(s, None)
