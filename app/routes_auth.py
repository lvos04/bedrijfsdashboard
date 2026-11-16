"""Welkomstwizard, aanmelden (met optionele TOTP-MFA), afmelden en het eigen account."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import instellingen, totp
from .beveiliging import (
    SESSIE_COOKIE,
    AanmeldBeperking,
    controleer_wachtwoord,
    hash_wachtwoord,
    is_https,
    maak_sessie,
    sha256,
    verwijder_sessie,
    wachtwoord_fout,
)
from .tijd import iso, nu, parse_iso
from .web import (
    MFA_COOKIE,
    PRE_CSRF_COOKIE,
    controleer_post,
    heeft_gebruikers,
    ip,
    log,
    render,
    ruw,
    veld,
    vereis_gebruiker,
    zet_sessiecookie,
)
from .web import gebruiker as huidige_gebruiker

router = APIRouter()
MFA_GELDIG = timedelta(minutes=5)
MFA_MAX_POGINGEN = 5


def _na_aanmelden(request: Request, gebruiker_id: int, doel: str = "/") -> RedirectResponse:
    with request.app.state.db() as conn:
        token = maak_sessie(conn, gebruiker_id)
    resp = RedirectResponse(doel, status_code=303)
    zet_sessiecookie(request, resp, token)
    resp.delete_cookie(PRE_CSRF_COOKIE)
    resp.delete_cookie(MFA_COOKIE, path="/aanmelden")
    return resp


# ---------------------------------------------------------------- welkom


@router.get("/welkom", response_class=HTMLResponse)
def welkom(request: Request):
    if heeft_gebruikers(request.app.state.db):
        return RedirectResponse("/aanmelden", status_code=303)
    return render(request, "welkom.html")


@router.post("/welkom")
async def welkom_post(request: Request):
    form = await controleer_post(request)
    naam = veld(form, "gebruikersnaam", 64)
    ww, ww2 = ruw(form, "wachtwoord"), ruw(form, "wachtwoord2")
    bedrijf = veld(form, "bedrijfsnaam", 200)
    fout = None if naam else "Vul een gebruikersnaam in."
    fout = fout or wachtwoord_fout(ww, ww2)
    if fout:
        return render(request, "welkom.html", status_code=422, fout=fout, gebruikersnaam=naam, bedrijfsnaam=bedrijf)
    with request.app.state.db.transactie() as conn:
        # Dubbele controle binnen de transactie: de wizard kan maar één keer worden doorlopen.
        if conn.execute("SELECT 1 FROM gebruikers LIMIT 1").fetchone():
            return RedirectResponse("/aanmelden", status_code=303)
        cur = conn.execute(
            "INSERT INTO gebruikers (gebruikersnaam, wachtwoord_hash, rol, aangemaakt_op) "
            "VALUES (?, ?, 'beheerder', ?)",
            (naam, hash_wachtwoord(ww), iso()),
        )
        if bedrijf:
            conn.execute("INSERT OR REPLACE INTO instellingen (sleutel, waarde) VALUES ('bedrijfsnaam', ?)", (bedrijf,))
        gebruiker_id = cur.lastrowid
    log(request, "welkom.beheerder_aangemaakt", gebruiker_naam=naam)
    return _na_aanmelden(request, gebruiker_id, "/beheer")


# ---------------------------------------------------------------- aanmelden


@router.get("/aanmelden", response_class=HTMLResponse)
def aanmelden(request: Request):
    if not heeft_gebruikers(request.app.state.db):
        return RedirectResponse("/welkom", status_code=303)
    if huidige_gebruiker(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "aanmelden.html")


@router.post("/aanmelden")
async def aanmelden_post(request: Request):
    form = await controleer_post(request)
    naam = veld(form, "gebruikersnaam", 64)
    ww = ruw(form, "wachtwoord")
    beperking: AanmeldBeperking = request.app.state.aanmeldbeperking
    adres = ip(request)
    if beperking.geblokkeerd(naam, adres):
        log(request, "aanmelden.geblokkeerd", gebruiker_naam=naam)
        return render(
            request,
            "aanmelden.html",
            status_code=429,
            gebruikersnaam=naam,
            fout="Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw.",
        )
    db = request.app.state.db
    with db() as conn:
        rij = conn.execute(
            "SELECT id, gebruikersnaam, wachtwoord_hash, totp_geheim FROM gebruikers WHERE gebruikersnaam = ?", (naam,)
        ).fetchone()
    if not controleer_wachtwoord(rij["wachtwoord_hash"] if rij else None, ww):
        beperking.fout(naam, adres)
        log(request, "aanmelden.mislukt", gebruiker_naam=naam)
        return render(
            request,
            "aanmelden.html",
            status_code=401,
            gebruikersnaam=naam,
            fout="Onjuiste gebruikersnaam of wachtwoord.",
        )
    if rij["totp_geheim"] is not None:
        token = secrets.token_urlsafe(32)
        with db() as conn:
            conn.execute(
                "INSERT INTO mfa_uitdagingen (token_hash, gebruiker_id, verloopt_op) VALUES (?, ?, ?)",
                (sha256(token), rij["id"], iso(nu() + MFA_GELDIG)),
            )
        log(request, "aanmelden.mfa_gevraagd", gebruiker_naam=rij["gebruikersnaam"])
        resp = render(request, "aanmelden_mfa.html")
        resp.set_cookie(
            MFA_COOKIE,
            token,
            max_age=int(MFA_GELDIG.total_seconds()),
            httponly=True,
            samesite="strict",
            secure=is_https(request),
            path="/aanmelden",
        )
        return resp
    beperking.reset(naam, adres)
    log(request, "aanmelden.gelukt", gebruiker_naam=rij["gebruikersnaam"])
    return _na_aanmelden(request, rij["id"])


@router.post("/aanmelden/mfa")
async def aanmelden_mfa(request: Request):
    form = await controleer_post(request)
    db = request.app.state.db
    token = request.cookies.get(MFA_COOKIE)
    with db() as conn:
        rij = (
            conn.execute(
                "SELECT u.token_hash, u.verloopt_op, u.pogingen, g.id, g.gebruikersnaam, g.totp_geheim, "
                "g.totp_laatste_stap FROM mfa_uitdagingen u JOIN gebruikers g ON g.id = u.gebruiker_id "
                "WHERE u.token_hash = ?",
                (sha256(token),),
            ).fetchone()
            if token
            else None
        )
    verlopen = rij is None or (parse_iso(rij["verloopt_op"]) or nu()) < nu() or rij["totp_geheim"] is None
    if verlopen:
        return render(
            request,
            "aanmelden.html",
            status_code=401,
            fout="De aanmelding is verlopen. Meld u opnieuw aan met uw wachtwoord.",
        )
    beperking: AanmeldBeperking = request.app.state.aanmeldbeperking
    adres = ip(request)
    if beperking.geblokkeerd(rij["gebruikersnaam"], adres):
        log(request, "aanmelden.geblokkeerd", gebruiker_naam=rij["gebruikersnaam"])
        return render(
            request,
            "aanmelden.html",
            status_code=429,
            fout="Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw.",
        )
    geheim = request.app.state.kluis.ontsleutel(rij["totp_geheim"])
    stap = totp.controleer(geheim, veld(form, "code", 20), rij["totp_laatste_stap"])
    if stap is None:
        beperking.fout(rij["gebruikersnaam"], adres)
        log(request, "aanmelden.mfa_mislukt", gebruiker_naam=rij["gebruikersnaam"])
        with db() as conn:
            conn.execute(
                "UPDATE mfa_uitdagingen SET pogingen = pogingen + 1 WHERE token_hash = ?", (rij["token_hash"],)
            )
            if rij["pogingen"] + 1 >= MFA_MAX_POGINGEN:
                conn.execute("DELETE FROM mfa_uitdagingen WHERE token_hash = ?", (rij["token_hash"],))
                return render(
                    request,
                    "aanmelden.html",
                    status_code=401,
                    fout="Te vaak een onjuiste code. Meld u opnieuw aan met uw wachtwoord.",
                )
        return render(request, "aanmelden_mfa.html", status_code=401, fout="Onjuiste of verlopen code.")
    with db() as conn:
        conn.execute("UPDATE gebruikers SET totp_laatste_stap = ? WHERE id = ?", (stap, rij["id"]))
        conn.execute("DELETE FROM mfa_uitdagingen WHERE token_hash = ?", (rij["token_hash"],))
    beperking.reset(rij["gebruikersnaam"], adres)
    log(request, "aanmelden.gelukt", {"mfa": True}, gebruiker_naam=rij["gebruikersnaam"])
    return _na_aanmelden(request, rij["id"])


@router.post("/afmelden")
async def afmelden(request: Request):
    await controleer_post(request)
    sessie = huidige_gebruiker(request)
    with request.app.state.db() as conn:
        verwijder_sessie(conn, request.cookies.get(SESSIE_COOKIE))
    if sessie:
        log(request, "afmelden")
    resp = RedirectResponse("/aanmelden", status_code=303)
    resp.delete_cookie(SESSIE_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- eigen account


def _account_ctx(request: Request, sessie) -> dict:
    return {
        "mfa_actief": bool(sessie["mfa_actief"]),
        "mfa_verplicht": sessie["rol"] == "beheerder"
        and instellingen.aan(request.app.state.db, "mfa_verplicht_beheerders"),
    }


@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    sessie = vereis_gebruiker(request)
    return render(request, "account.html", **_account_ctx(request, sessie))


@router.post("/account/wachtwoord")
async def account_wachtwoord(request: Request):
    sessie = vereis_gebruiker(request)
    form = await controleer_post(request)
    huidig, nieuw, nieuw2 = ruw(form, "huidig"), ruw(form, "nieuw"), ruw(form, "nieuw2")
    db = request.app.state.db
    with db() as conn:
        rij = conn.execute("SELECT wachtwoord_hash FROM gebruikers WHERE id = ?", (sessie["id"],)).fetchone()
    fout = None if controleer_wachtwoord(rij["wachtwoord_hash"], huidig) else "Het huidige wachtwoord klopt niet."
    fout = fout or wachtwoord_fout(nieuw, nieuw2)
    if fout:
        return render(request, "account.html", status_code=422, fout=fout, **_account_ctx(request, sessie))
    with db() as conn:
        conn.execute("UPDATE gebruikers SET wachtwoord_hash = ? WHERE id = ?", (hash_wachtwoord(nieuw), sessie["id"]))
        # Andere sessies van deze gebruiker ongeldig maken; de huidige blijft geldig.
        conn.execute(
            "DELETE FROM sessies WHERE gebruiker_id = ? AND token_hash != ?", (sessie["id"], sessie["token_hash"])
        )
    log(request, "account.wachtwoord_gewijzigd")
    return RedirectResponse("/account?m=wachtwoord_gewijzigd", status_code=303)


@router.post("/account/mfa/start", response_class=HTMLResponse)
async def mfa_start(request: Request):
    sessie = vereis_gebruiker(request)
    await controleer_post(request)
    geheim = totp.nieuw_geheim()
    with request.app.state.db() as conn:
        conn.execute(
            "UPDATE gebruikers SET totp_nieuw = ? WHERE id = ?",
            (request.app.state.kluis.versleutel(geheim), sessie["id"]),
        )
    return render(request, "account.html", **_account_ctx(request, sessie), mfa_instellen=_instel_ctx(sessie, geheim))


def _instel_ctx(sessie, geheim: str) -> dict:
    return {"geheim": totp.opgemaakt(geheim), "uri": totp.otpauth_uri(geheim, sessie["gebruikersnaam"])}


@router.post("/account/mfa/bevestigen")
async def mfa_bevestigen(request: Request):
    sessie = vereis_gebruiker(request)
    form = await controleer_post(request)
    db = request.app.state.db
    with db() as conn:
        rij = conn.execute("SELECT totp_nieuw FROM gebruikers WHERE id = ?", (sessie["id"],)).fetchone()
    if rij["totp_nieuw"] is None:
        return RedirectResponse("/account", status_code=303)
    geheim = request.app.state.kluis.ontsleutel(rij["totp_nieuw"])
    stap = totp.controleer(geheim, veld(form, "code", 20))
    if stap is None:
        return render(
            request,
            "account.html",
            status_code=422,
            fout="Onjuiste code. Controleer de tijd op uw telefoon.",
            **_account_ctx(request, sessie),
            mfa_instellen=_instel_ctx(sessie, geheim),
        )
    with db() as conn:
        conn.execute(
            "UPDATE gebruikers SET totp_geheim = totp_nieuw, totp_nieuw = NULL, totp_laatste_stap = ? WHERE id = ?",
            (stap, sessie["id"]),
        )
    log(request, "account.mfa_ingeschakeld")
    return RedirectResponse("/account?m=mfa_aan", status_code=303)


@router.post("/account/mfa/uitschakelen")
async def mfa_uitschakelen(request: Request):
    sessie = vereis_gebruiker(request)
    form = await controleer_post(request)
    ctx = _account_ctx(request, sessie)
    if ctx["mfa_verplicht"]:
        return render(
            request,
            "account.html",
            status_code=422,
            **ctx,
            fout="MFA is verplicht voor beheerders en kan niet worden uitgeschakeld.",
        )
    db = request.app.state.db
    with db() as conn:
        rij = conn.execute("SELECT wachtwoord_hash FROM gebruikers WHERE id = ?", (sessie["id"],)).fetchone()
    if not controleer_wachtwoord(rij["wachtwoord_hash"], ruw(form, "wachtwoord")):
        return render(request, "account.html", status_code=422, **ctx, fout="Het wachtwoord klopt niet.")
    with db() as conn:
        conn.execute(
            "UPDATE gebruikers SET totp_geheim = NULL, totp_nieuw = NULL, totp_laatste_stap = NULL WHERE id = ?",
            (sessie["id"],),
        )
    log(request, "account.mfa_uitgeschakeld")
    return RedirectResponse("/account?m=mfa_uit", status_code=303)
