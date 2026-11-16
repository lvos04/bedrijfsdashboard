"""Wachtwoorden, sessies, CSRF/oorsprong en aanmeldbeperking."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
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
        "INSERT INTO sessies (token_hash, gebruiker_id, csrf_token, aangemaakt_op, verloopt_op) VALUES (?, ?, ?, ?, ?)",
        (sha256(token), gebruiker_id, secrets.token_urlsafe(32), iso(), iso(nu() + SESSIE_DUUR)),
    )
    conn.execute("DELETE FROM sessies WHERE verloopt_op < ?", (iso(),))
    return token


def haal_sessie(conn, token: str | None):
    if not token:
        return None
    rij = conn.execute(
        "SELECT s.token_hash, s.csrf_token, s.verloopt_op, g.id, g.gebruikersnaam, g.rol, "
        "g.totp_geheim IS NOT NULL AS mfa_actief "
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


def ruim_verlopen_op(conn) -> int:
    """Verlopen sessies en MFA-uitdagingen verwijderen (dagelijkse opschoontaak)."""
    n = conn.execute("DELETE FROM sessies WHERE verloopt_op < ?", (iso(),)).rowcount
    n += conn.execute("DELETE FROM mfa_uitdagingen WHERE verloopt_op < ?", (iso(),)).rowcount
    return n


def verwijder_sessie(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessies WHERE token_hash = ?", (sha256(token),))


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def oorsprong_ok(request: Request, strikt: bool = False) -> bool:
    """Muterende verzoeken moeten van dezelfde host komen (Origin, anders Referer).

    Standaard (niet strikt) wordt een verzoek zonder Origin én zonder Referer toegelaten en vertrouwen we dan alleen
    op het CSRF-token. Met ``strikt`` (instelling "Strikte oorsprongcontrole") wordt zo'n verzoek geweigerd.
    """
    bron = request.headers.get("origin") or request.headers.get("referer")
    if not bron:
        return not strikt
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
    """In-geheugen aanmeldbeperking per account + echt IP-adres (zie `client_ip`).

    * max ``per_account_ip`` mislukte pogingen per venster voor dezelfde gebruikersnaam vanaf hetzelfde IP;
    * daarnaast max ``per_account`` mislukte pogingen per venster voor een gebruikersnaam vanaf alle IP's samen
      (vangnet tegen het rouleren of vervalsen van IP-adressen).

    Er is bewust geen blokkade per IP alleen: achter een proxy of tunnel delen alle gebruikers vaak één IP, en
    dan zou één persoon met een fout wachtwoord iedereen buitensluiten. De tellers staan in het geheugen van het
    (enige) proces en beginnen na een herstart opnieuw.
    """

    def __init__(self, per_account_ip: int = 5, per_account: int = 20, venster_s: int = 900):
        self.per_account_ip = per_account_ip
        self.per_account = per_account
        self.venster = venster_s
        self._fouten: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _sleutels(naam: str, ip: str) -> tuple[str, str]:
        naam = (naam or "").strip().lower()
        return f"acc-ip:{naam}|{ip}", f"acc:{naam}"

    def _schoon(self, sleutel: str, t: float) -> list[float]:
        lijst = [x for x in self._fouten.get(sleutel, []) if t - x < self.venster]
        if lijst:
            self._fouten[sleutel] = lijst
        else:
            self._fouten.pop(sleutel, None)
        return lijst

    def geblokkeerd(self, naam: str, ip: str) -> bool:
        t = time.monotonic()
        combi, acc = self._sleutels(naam, ip)
        with self._lock:
            return len(self._schoon(combi, t)) >= self.per_account_ip or len(self._schoon(acc, t)) >= self.per_account

    def fout(self, naam: str, ip: str) -> None:
        t = time.monotonic()
        with self._lock:
            for s in self._sleutels(naam, ip):
                self._fouten.setdefault(s, []).append(t)

    def reset(self, naam: str, ip: str) -> None:
        with self._lock:
            for s in self._sleutels(naam, ip):
                self._fouten.pop(s, None)

    def opruimen(self) -> None:
        t = time.monotonic()
        with self._lock:
            for s in list(self._fouten):
                self._schoon(s, t)


def client_ip(request: Request, vertrouwd: list) -> str:
    """Het echte IP van de gebruiker.

    `X-Forwarded-For` wordt alleen gebruikt als het directe verbindings-IP een vertrouwde proxy is. Dan lopen we
    de lijst van rechts naar links af en nemen het eerste adres dat zelf geen vertrouwde proxy is.
    """
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "onbekend"

    def vertrouwd_ip(adres) -> bool:
        return any(adres.version == n.version and adres in n for n in vertrouwd)

    if not vertrouwd or not vertrouwd_ip(peer_ip):
        return str(peer_ip)
    kop = request.headers.get("x-forwarded-for", "")
    kandidaten = []
    for deel in kop.split(","):
        try:
            kandidaten.append(ipaddress.ip_address(deel.strip()))
        except ValueError:
            continue
    for adres in reversed(kandidaten):
        if not vertrouwd_ip(adres):
            return str(adres)
    return str(kandidaten[0]) if kandidaten else str(peer_ip)
