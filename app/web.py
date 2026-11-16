"""Gedeelde hulpfuncties voor de webroutes: aanmelding, rechten, CSRF/oorsprong, invoer en weergave."""

from __future__ import annotations

import re
import ssl
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from . import audit, instellingen
from .beveiliging import SESSIE_COOKIE, SESSIE_DUUR, client_ip, csrf_ok, haal_sessie, is_https, oorsprong_ok
from .config import Config
from .db import Database

PRE_CSRF_COOKIE = "bd_csrf"
MFA_COOKIE = "bd_mfa"
SLUG_PATROON = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")


class NietAangemeld(Exception):
    pass


class GeenRechten(Exception):
    pass


class MfaVereist(Exception):
    """Beheerder zonder MFA terwijl "MFA verplicht voor beheerders" aan staat."""


def gebruiker(request: Request):
    if not hasattr(request.state, "sessie"):
        with request.app.state.db() as conn:
            request.state.sessie = haal_sessie(conn, request.cookies.get(SESSIE_COOKIE))
    return request.state.sessie


def heeft_gebruikers(db: Database) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM gebruikers LIMIT 1").fetchone() is not None


def vereis_gebruiker(request: Request):
    sessie = gebruiker(request)
    if sessie is None:
        raise NietAangemeld()
    return sessie


def mfa_ontbreekt(request: Request, sessie) -> bool:
    return (
        sessie is not None
        and sessie["rol"] == "beheerder"
        and not sessie["mfa_actief"]
        and instellingen.aan(request.app.state.db, "mfa_verplicht_beheerders")
    )


def vereis_beheerder(request: Request):
    sessie = vereis_gebruiker(request)
    if sessie["rol"] != "beheerder":
        raise GeenRechten()
    if mfa_ontbreekt(request, sessie):
        raise MfaVereist()
    return sessie


def ip(request: Request) -> str:
    return client_ip(request, instellingen.vertrouwde_proxies(request.app.state.db))


def log(request: Request, actie: str, details: dict | None = None, gebruiker_naam: str | None = None) -> None:
    sessie = getattr(request.state, "sessie", None)
    naam = gebruiker_naam or (sessie["gebruikersnaam"] if sessie else None)
    audit.schrijf(request.app.state.db, actie, gebruiker=naam, ip=ip(request), details=details)


async def controleer_post(request: Request) -> dict:
    """Oorsprong + CSRF-token (formulierveld csrf_token of header X-CSRF-Token)."""
    strikt = instellingen.aan(request.app.state.db, "oorsprong_strikt")
    if not oorsprong_ok(request, strikt=strikt):
        raise HTTPException(403, "Verzoek geweigerd: onbekende of ontbrekende oorsprong (Origin/Referer).")
    form = {}
    if request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = dict(await request.form())
    gegeven = form.get("csrf_token") or request.headers.get("x-csrf-token")
    sessie = gebruiker(request)
    verwacht = sessie["csrf_token"] if sessie else request.cookies.get(PRE_CSRF_COOKIE)
    if not verwacht or not csrf_ok(verwacht, gegeven if isinstance(gegeven, str) else None):
        raise HTTPException(403, "Verzoek geweigerd: ongeldig of ontbrekend CSRF-token. Herlaad de pagina.")
    return form


def veld(form: dict, naam: str, max_len: int = 500) -> str:
    waarde = form.get(naam)
    return waarde.strip()[:max_len] if isinstance(waarde, str) else ""


def ruw(form: dict, naam: str, max_len: int = 1000) -> str:
    """Zoals `veld`, maar zonder spaties weg te halen (voor wachtwoorden)."""
    waarde = form.get(naam)
    return waarde[:max_len] if isinstance(waarde, str) else ""


def zet_sessiecookie(request: Request, response: Response, token: str) -> None:
    cfg: Config = request.app.state.config
    secure = is_https(request) if cfg.cookie_secure == "auto" else cfg.cookie_secure in ("1", "true", "ja")
    response.set_cookie(
        SESSIE_COOKIE,
        token,
        max_age=int(SESSIE_DUUR.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def render(request: Request, sjabloon: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return request.app.state.render(request, sjabloon, status_code=status_code, **ctx)


def valideer_url(url: str) -> str | None:
    delen = urlsplit(url)
    if delen.scheme not in ("http", "https") or not delen.netloc:
        return "De basis-URL moet beginnen met http:// of https:// en een host bevatten."
    if delen.query or delen.fragment or delen.username or delen.password:
        return "De basis-URL mag geen query, fragment of inloggegevens bevatten."
    return None


def valideer_ca(pem: str) -> str | None:
    if "BEGIN CERTIFICATE" not in pem:
        return "Het CA-bestand moet een PEM-certificaat zijn (-----BEGIN CERTIFICATE-----)."
    try:
        ssl.create_default_context().load_verify_locations(cadata=pem)
    except (ssl.SSLError, ValueError) as exc:
        return f"Het CA-certificaat is ongeldig: {exc}"
    return None


def valideer_bron(b: dict) -> str | None:
    """Gedeelde validatie voor formulier en terugzetten van een back-up."""
    if not isinstance(b.get("naam"), str) or not b["naam"].strip():
        return "Vul een naam in."
    if not isinstance(b.get("app_slug"), str) or not SLUG_PATROON.match(b["app_slug"]):
        return "De app-slug mag alleen kleine letters, cijfers en koppeltekens bevatten (bijv. scouten)."
    if not isinstance(b.get("basis_url"), str):
        return "De basis-URL ontbreekt."
    fout = valideer_url(b["basis_url"])
    if fout:
        return fout
    if b.get("ca_pem"):
        if not isinstance(b["ca_pem"], str):
            return "Ongeldig CA-certificaat."
        return valideer_ca(b["ca_pem"])
    return None


def optioneel_getal(tekst: str, minimum: int, maximum: int) -> int | None:
    """Leeg = None (globale instelling gebruiken). Gooit ValueError bij een ongeldig getal."""
    tekst = (tekst or "").strip()
    if not tekst:
        return None
    waarde = int(tekst)
    if not minimum <= waarde <= maximum:
        raise ValueError
    return waarde
