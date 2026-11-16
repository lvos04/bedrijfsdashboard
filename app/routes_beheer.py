"""Beheer: bronnen, instellingen, beveiliging, gebruikers, koppelsleutels, e-mail, tv-weergave, back-up en audit."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import audit, back_up, instellingen, koppeling, mail, overzicht
from .beveiliging import controleer_wachtwoord, hash_wachtwoord, sha256, wachtwoord_fout
from .ophalen import OphaalFout, haal_alle_op, haal_bron_op, historie_dagen, interval_minuten, test_verbinding
from .tijd import iso
from .versleuteling import Kluis
from .web import (
    controleer_post,
    log,
    optioneel_getal,
    render,
    ruw,
    valideer_bron,
    veld,
    vereis_beheerder,
)

router = APIRouter()


# ---------------------------------------------------------------- hulpfuncties


def _bron_of_404(db, bron_id: int) -> dict:
    b = overzicht.bron(db, bron_id)
    if b is None:
        raise HTTPException(404, "Bron niet gevonden.")
    return b


def _ruwe_bron(db, bron_id: int):
    with db() as conn:
        rij = conn.execute("SELECT * FROM bronnen WHERE id = ?", (bron_id,)).fetchone()
    if rij is None:
        raise HTTPException(404, "Bron niet gevonden.")
    return rij


def beheer_ctx(request: Request) -> dict:
    db = request.app.state.db
    with db() as conn:
        gebruikers = [
            dict(r)
            for r in conn.execute(
                "SELECT id, gebruikersnaam, rol, aangemaakt_op, totp_geheim IS NOT NULL AS mfa_actief "
                "FROM gebruikers ORDER BY gebruikersnaam COLLATE NOCASE"
            )
        ]
        sleutels = koppeling.lijst_sleutels(conn)
    w = lambda k: instellingen.waarde(db, k)  # noqa: E731
    return {
        "bronnen": overzicht.bronnen(db, alleen_actief=False),
        "gebruikers": gebruikers,
        "koppelsleutels": sleutels,
        "interval": interval_minuten(db),
        "historie_dagen": historie_dagen(db),
        "verouderd_na": w("verouderd_na_minuten"),
        "bedrijfsnaam": db.instelling("bedrijfsnaam") or "",
        "koppeling_aan": instellingen.aan(db, "koppeling_ingeschakeld"),
        "beveiliging": {
            "mfa_verplicht": instellingen.aan(db, "mfa_verplicht_beheerders"),
            "oorsprong_strikt": instellingen.aan(db, "oorsprong_strikt"),
            "vertrouwde_proxies": w("vertrouwde_proxies"),
            "proxies_omgeving": instellingen.proxies_uit_omgeving(),
            "audit_bewaardagen": w("audit_bewaardagen"),
            "laatste_opschoning": db.instelling("laatste_opschoning"),
        },
        "email": {
            "aan": instellingen.aan(db, "email_ingeschakeld"),
            "smtp_host": w("smtp_host"),
            "smtp_poort": w("smtp_poort"),
            "smtp_beveiliging": w("smtp_beveiliging"),
            "smtp_gebruiker": w("smtp_gebruiker"),
            "heeft_wachtwoord": bool(w("smtp_wachtwoord_versleuteld")),
            "afzender": w("email_afzender"),
            "ontvangers": w("email_ontvangers"),
            "laatste_fout": db.instelling("laatste_mail_fout") or "",
            "laatste_op": db.instelling("laatste_mail_op"),
        },
        "tv": {"aan": instellingen.aan(db, "tv_ingeschakeld"), "heeft_link": bool(w("tv_token_hash"))},
    }


def _fout(request: Request, tekst: str, status_code: int = 422, **extra) -> HTMLResponse:
    return render(request, "beheer.html", status_code=status_code, fout=tekst, **beheer_ctx(request), **extra)


def _zet_instellingen(request: Request, nieuw: dict[str, str], vertrouwelijk: dict[str, str] | None = None) -> None:
    """Schrijft instellingen en logt welke er wijzigden (vertrouwelijke waarden zonder inhoud)."""
    db = request.app.state.db
    gewijzigd = {}
    for k, v in nieuw.items():
        oud = instellingen.waarde(db, k)
        if oud != v:
            gewijzigd[k] = {"oud": oud, "nieuw": v}
            db.zet_instelling(k, v)
    geheim_gewijzigd = []
    for label, (k, v) in (vertrouwelijk or {}).items():
        if instellingen.waarde(db, k) != v:
            db.zet_instelling(k, v)
            geheim_gewijzigd.append(label)
    if gewijzigd or geheim_gewijzigd:
        details = {"gewijzigd": gewijzigd}
        if geheim_gewijzigd:
            details["vertrouwelijk_gewijzigd"] = geheim_gewijzigd
        log(request, "instellingen.gewijzigd", details)


def _vinkje(form: dict, naam: str) -> str:
    return "1" if form.get(naam) == "1" else "0"


# ---------------------------------------------------------------- pagina en instellingen


@router.get("/beheer", response_class=HTMLResponse)
def beheer(request: Request):
    vereis_beheerder(request)
    return render(request, "beheer.html", **beheer_ctx(request))


@router.post("/beheer/ophalen")
async def beheer_ophalen(request: Request):
    vereis_beheerder(request)
    await controleer_post(request)
    await haal_alle_op(request.app.state.db, request.app.state.kluis, request.app.state.http_transport)
    return RedirectResponse("/beheer?m=opgehaald", status_code=303)


@router.post("/beheer/instellingen")
async def beheer_instellingen(request: Request):
    vereis_beheerder(request)
    form = await controleer_post(request)
    try:
        interval = int(veld(form, "interval_minuten", 5))
        dagen = int(veld(form, "historie_dagen", 5))
        verouderd = optioneel_getal(veld(form, "verouderd_na_minuten", 6), 1, 100000)
        if not (1 <= interval <= 1440 and 1 <= dagen <= 3650):
            raise ValueError
    except ValueError:
        return _fout(
            request,
            "Interval moet 1–1440 minuten zijn, historie 1–3650 dagen en 'verouderd na' leeg of 1–100000 minuten.",
        )
    nieuw = {
        "interval_minuten": str(interval),
        "historie_dagen": str(dagen),
        "verouderd_na_minuten": "" if verouderd is None else str(verouderd),
    }
    if "bedrijfsnaam" in form:
        nieuw["bedrijfsnaam"] = veld(form, "bedrijfsnaam", 200)
    _zet_instellingen(request, nieuw)
    request.app.state.planner.nu_ophalen.set()
    return RedirectResponse("/beheer?m=instellingen_opgeslagen", status_code=303)


@router.post("/beheer/beveiliging")
async def beheer_beveiliging(request: Request):
    sessie = vereis_beheerder(request)
    form = await controleer_post(request)
    proxies = veld(form, "vertrouwde_proxies", 1000) or "privé"
    try:
        instellingen.parse_proxies(proxies)
    except ValueError:
        return _fout(request, "Vertrouwde proxy: gebruik 'privé', 'geen' of een komma-lijst van IP-adressen/CIDR's.")
    try:
        bewaar = optioneel_getal(veld(form, "audit_bewaardagen", 6), 0, 36500) or 0
    except ValueError:
        return _fout(request, "Bewaartermijn auditlog: 0 (onbeperkt) tot 36500 dagen.")
    mfa_verplicht = _vinkje(form, "mfa_verplicht_beheerders")
    if mfa_verplicht == "1" and not sessie["mfa_actief"]:
        return _fout(
            request,
            "Stel eerst MFA in voor uw eigen account (Account → MFA) voordat u MFA verplicht maakt; "
            "anders sluit u uzelf buiten Beheer.",
        )
    _zet_instellingen(
        request,
        {
            "mfa_verplicht_beheerders": mfa_verplicht,
            "oorsprong_strikt": _vinkje(form, "oorsprong_strikt"),
            "vertrouwde_proxies": proxies,
            "audit_bewaardagen": str(bewaar),
        },
    )
    return RedirectResponse("/beheer?m=instellingen_opgeslagen#beveiliging", status_code=303)


@router.post("/beheer/koppeling")
async def beheer_koppeling(request: Request):
    vereis_beheerder(request)
    form = await controleer_post(request)
    _zet_instellingen(request, {"koppeling_ingeschakeld": _vinkje(form, "koppeling_ingeschakeld")})
    return RedirectResponse("/beheer?m=instellingen_opgeslagen#koppelsleutels", status_code=303)


# ---------------------------------------------------------------- e-mailmeldingen


@router.post("/beheer/email")
async def beheer_email(request: Request):
    vereis_beheerder(request)
    form = await controleer_post(request)
    kluis: Kluis = request.app.state.kluis
    aan = _vinkje(form, "email_ingeschakeld")
    host = veld(form, "smtp_host", 255)
    beveiliging = veld(form, "smtp_beveiliging", 10) or "starttls"
    afzender = veld(form, "email_afzender", 255)
    ontvangers = [o.strip() for o in veld(form, "email_ontvangers", 2000).split(",") if o.strip()]
    try:
        poort = int(veld(form, "smtp_poort", 5) or "587")
        if not 1 <= poort <= 65535:
            raise ValueError
    except ValueError:
        return _fout(request, "SMTP-poort moet 1–65535 zijn.")
    if beveiliging not in instellingen.SMTP_BEVEILIGING:
        return _fout(request, "Ongeldige SMTP-beveiliging.")
    if any("@" not in o or any(c in o for c in " <>\r\n") for o in [*ontvangers, *([afzender] if afzender else [])]):
        return _fout(request, "Vul geldige e-mailadressen in (ontvangers gescheiden door komma's).")
    if aan == "1" and not (host and afzender and ontvangers):
        return _fout(request, "Voor e-mailmeldingen zijn SMTP-server, afzender en minstens één ontvanger nodig.")
    vertrouwelijk = {}
    nieuw_ww = ruw(form, "smtp_wachtwoord", 500)
    if nieuw_ww:
        vertrouwelijk["SMTP-wachtwoord"] = ("smtp_wachtwoord_versleuteld", kluis.versleutel(nieuw_ww).decode("ascii"))
    elif form.get("smtp_wachtwoord_wissen") == "1":
        vertrouwelijk["SMTP-wachtwoord"] = ("smtp_wachtwoord_versleuteld", "")
    _zet_instellingen(
        request,
        {
            "email_ingeschakeld": aan,
            "smtp_host": host,
            "smtp_poort": str(poort),
            "smtp_beveiliging": beveiliging,
            "smtp_gebruiker": veld(form, "smtp_gebruiker", 255),
            "email_afzender": afzender,
            "email_ontvangers": ", ".join(ontvangers),
        },
        vertrouwelijk,
    )
    return RedirectResponse("/beheer?m=instellingen_opgeslagen#email", status_code=303)


@router.post("/beheer/email/test", response_class=HTMLResponse)
async def beheer_email_test(request: Request):
    vereis_beheerder(request)
    await controleer_post(request)
    db = request.app.state.db
    cfg = mail.smtp_config(db, request.app.state.kluis)
    try:
        await asyncio.to_thread(
            mail.verstuur,
            cfg,
            "[Bedrijfsdashboard] Testmail",
            "Dit is een testmail van het Bedrijfsdashboard. De e-mailinstellingen werken.",
        )
    except mail.MailFout as exc:
        return _fout(request, f"Testmail niet verstuurd: {exc}", status_code=200)
    log(request, "email.test", {"ontvangers": len(cfg["ontvangers"])})
    return RedirectResponse("/beheer?m=testmail#email", status_code=303)


# ---------------------------------------------------------------- tv-weergave


@router.post("/beheer/tv")
async def beheer_tv(request: Request):
    vereis_beheerder(request)
    form = await controleer_post(request)
    _zet_instellingen(request, {"tv_ingeschakeld": _vinkje(form, "tv_ingeschakeld")})
    return RedirectResponse("/beheer?m=instellingen_opgeslagen#tv", status_code=303)


@router.post("/beheer/tv/nieuwe-link", response_class=HTMLResponse)
async def beheer_tv_link(request: Request):
    vereis_beheerder(request)
    await controleer_post(request)
    token = secrets.token_urlsafe(24)
    request.app.state.db.zet_instelling("tv_token_hash", sha256(token))
    log(request, "tv.link_vernieuwd")
    url = str(request.base_url).rstrip("/") + f"/tv/{token}"
    return render(request, "beheer.html", nieuwe_tv_link=url, **beheer_ctx(request))


# ---------------------------------------------------------------- bronnen


@router.get("/beheer/bronnen/nieuw", response_class=HTMLResponse)
def bron_nieuw(request: Request):
    vereis_beheerder(request)
    return render(
        request, "bron_formulier.html", bron=None, waarden={}, interval=interval_minuten(request.app.state.db)
    )


@router.get("/beheer/bronnen/{bron_id}", response_class=HTMLResponse)
def bron_bewerken(request: Request, bron_id: int):
    vereis_beheerder(request)
    b = _bron_of_404(request.app.state.db, bron_id)
    return render(request, "bron_formulier.html", bron=b, waarden=b, interval=interval_minuten(request.app.state.db))


async def _bron_opslaan(request: Request, bron_id: int | None):
    vereis_beheerder(request)
    form = await controleer_post(request)
    db = request.app.state.db
    bestaand = _bron_of_404(db, bron_id) if bron_id else None
    waarden = {
        "naam": veld(form, "naam", 100),
        "app_slug": veld(form, "app_slug", 63).lower(),
        "basis_url": veld(form, "basis_url", 500).rstrip("/"),
        "actief": form.get("actief") == "1" if bron_id else True,
        "interval_minuten": veld(form, "interval_minuten", 5),
        "verouderd_na_minuten": veld(form, "verouderd_na_minuten", 6),
    }
    sleutel = veld(form, "koppelsleutel", 500)
    ca_pem = form.get("ca_pem") if isinstance(form.get("ca_pem"), str) else ""
    ca_bestand = form.get("ca_bestand")
    if ca_bestand is not None and hasattr(ca_bestand, "read"):
        inhoud = await ca_bestand.read(200_000)
        if inhoud:
            ca_pem = inhoud.decode("utf-8", errors="replace")
    ca_pem = (ca_pem or "").strip()
    ca_wissen = form.get("ca_wissen") == "1"

    fout = valideer_bron({**waarden, "ca_pem": ca_pem})
    if not fout and not bron_id and not sleutel:
        fout = "Vul de koppelsleutel in die u in de bron-app heeft aangemaakt."
    interval = verouderd = None
    if not fout:
        try:
            interval = optioneel_getal(waarden["interval_minuten"], 1, 1440)
            verouderd = optioneel_getal(waarden["verouderd_na_minuten"], 1, 100000)
        except ValueError:
            fout = "Ophaalinterval moet leeg of 1–1440 minuten zijn en 'verouderd na' leeg of 1–100000 minuten."
    if fout:
        return render(
            request,
            "bron_formulier.html",
            status_code=422,
            bron=bestaand,
            waarden=waarden,
            fout=fout,
            interval=interval_minuten(db),
        )

    kluis: Kluis = request.app.state.kluis
    with db.transactie() as conn:
        if bron_id is None:
            cur = conn.execute(
                "INSERT INTO bronnen (naam, app_slug, basis_url, sleutel_versleuteld, sleutel_voorvoegsel, ca_pem, "
                "actief, aangemaakt_op, interval_minuten, verouderd_na_minuten) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    waarden["naam"],
                    waarden["app_slug"],
                    waarden["basis_url"],
                    kluis.versleutel(sleutel),
                    sleutel[:8],
                    ca_pem or None,
                    iso(),
                    interval,
                    verouderd,
                ),
            )
            bron_id = cur.lastrowid
            actie = "bron.toegevoegd"
        else:
            conn.execute(
                "UPDATE bronnen SET naam = ?, app_slug = ?, basis_url = ?, actief = ?, interval_minuten = ?, "
                "verouderd_na_minuten = ? WHERE id = ?",
                (
                    waarden["naam"],
                    waarden["app_slug"],
                    waarden["basis_url"],
                    int(waarden["actief"]),
                    interval,
                    verouderd,
                    bron_id,
                ),
            )
            if sleutel:
                conn.execute(
                    "UPDATE bronnen SET sleutel_versleuteld = ?, sleutel_voorvoegsel = ? WHERE id = ?",
                    (kluis.versleutel(sleutel), sleutel[:8], bron_id),
                )
            if ca_pem:
                conn.execute("UPDATE bronnen SET ca_pem = ? WHERE id = ?", (ca_pem, bron_id))
            elif ca_wissen:
                conn.execute("UPDATE bronnen SET ca_pem = NULL WHERE id = ?", (bron_id,))
            actie = "bron.bijgewerkt"
    log(
        request,
        actie,
        {
            "bron_id": bron_id,
            "naam": waarden["naam"],
            "app": waarden["app_slug"],
            "basis_url": waarden["basis_url"],
            "actief": waarden["actief"],
            "interval_minuten": interval,
            "verouderd_na_minuten": verouderd,
            "nieuwe_koppelsleutel_voorvoegsel": sleutel[:8] if sleutel else None,
            "eigen_ca": "gewijzigd" if ca_pem else ("verwijderd" if ca_wissen else None),
        },
    )
    request.app.state.planner.nu_ophalen.set()
    return RedirectResponse("/beheer?m=bron_opgeslagen", status_code=303)


@router.post("/beheer/bronnen")
async def bron_toevoegen(request: Request):
    return await _bron_opslaan(request, None)


@router.post("/beheer/bronnen/{bron_id}")
async def bron_bijwerken(request: Request, bron_id: int):
    return await _bron_opslaan(request, bron_id)


@router.post("/beheer/bronnen/{bron_id}/verwijderen")
async def bron_verwijderen(request: Request, bron_id: int):
    vereis_beheerder(request)
    await controleer_post(request)
    with request.app.state.db() as conn:
        rij = conn.execute("SELECT naam, app_slug FROM bronnen WHERE id = ?", (bron_id,)).fetchone()
        conn.execute("DELETE FROM bronnen WHERE id = ?", (bron_id,))
    if rij:
        log(request, "bron.verwijderd", {"bron_id": bron_id, "naam": rij["naam"], "app": rij["app_slug"]})
    return RedirectResponse("/beheer?m=bron_verwijderd", status_code=303)


@router.post("/beheer/bronnen/{bron_id}/testen", response_class=HTMLResponse)
async def bron_testen(request: Request, bron_id: int):
    vereis_beheerder(request)
    await controleer_post(request)
    db, kluis, transport = request.app.state.db, request.app.state.kluis, request.app.state.http_transport
    rij = _ruwe_bron(db, bron_id)
    try:
        info = await test_verbinding(rij, kluis, transport)
        test = {"ok": True, "info": info, "bron_naam": rij["naam"]}
        if info.get("app") and info["app"] != rij["app_slug"]:
            test["waarschuwing"] = f"De bron meldt app-slug '{info['app']}', maar hier staat '{rij['app_slug']}'."
        await haal_bron_op(db, kluis, rij, transport)
    except OphaalFout as exc:
        test = {"ok": False, "fout": exc.tekst, "bron_naam": rij["naam"], "code": exc.code}
        await haal_bron_op(db, kluis, rij, transport)  # status bijwerken zodat het overzicht dezelfde uitleg toont
    except Exception as exc:  # bijv. ongeldige CA
        test = {"ok": False, "fout": f"Fout: {exc}", "bron_naam": rij["naam"]}
    return render(request, "beheer.html", test=test, **beheer_ctx(request))


# ---------------------------------------------------------------- gebruikers


@router.post("/beheer/gebruikers")
async def gebruiker_toevoegen(request: Request):
    vereis_beheerder(request)
    form = await controleer_post(request)
    naam = veld(form, "gebruikersnaam", 64)
    rol = veld(form, "rol", 20)
    ww = ruw(form, "wachtwoord")
    fout = None if naam else "Vul een gebruikersnaam in."
    fout = fout or (None if rol in ("beheerder", "lezer") else "Ongeldige rol.")
    fout = fout or wachtwoord_fout(ww)
    if not fout:
        with request.app.state.db() as conn:
            if conn.execute("SELECT 1 FROM gebruikers WHERE gebruikersnaam = ?", (naam,)).fetchone():
                fout = "Deze gebruikersnaam bestaat al."
            else:
                conn.execute(
                    "INSERT INTO gebruikers (gebruikersnaam, wachtwoord_hash, rol, aangemaakt_op) VALUES (?, ?, ?, ?)",
                    (naam, hash_wachtwoord(ww), rol, iso()),
                )
    if fout:
        return _fout(request, fout)
    log(request, "gebruiker.toegevoegd", {"gebruikersnaam": naam, "rol": rol})
    return RedirectResponse("/beheer?m=gebruiker_toegevoegd", status_code=303)


@router.post("/beheer/gebruikers/{gebruiker_id}/verwijderen")
async def gebruiker_verwijderen(request: Request, gebruiker_id: int):
    sessie = vereis_beheerder(request)
    await controleer_post(request)
    if gebruiker_id == sessie["id"]:
        return _fout(request, "U kunt uzelf niet verwijderen.")
    with request.app.state.db() as conn:
        rij = conn.execute("SELECT gebruikersnaam FROM gebruikers WHERE id = ?", (gebruiker_id,)).fetchone()
        conn.execute("DELETE FROM gebruikers WHERE id = ?", (gebruiker_id,))
    if rij:
        log(request, "gebruiker.verwijderd", {"gebruikersnaam": rij["gebruikersnaam"]})
    return RedirectResponse("/beheer?m=gebruiker_verwijderd", status_code=303)


@router.post("/beheer/gebruikers/{gebruiker_id}/wachtwoord")
async def gebruiker_wachtwoord_reset(request: Request, gebruiker_id: int):
    sessie = vereis_beheerder(request)
    form = await controleer_post(request)
    if gebruiker_id == sessie["id"]:
        return _fout(request, "Wijzig uw eigen wachtwoord via Account.")
    ww = ruw(form, "wachtwoord")
    fout = wachtwoord_fout(ww, ruw(form, "wachtwoord2") if "wachtwoord2" in form else None)
    if fout:
        return _fout(request, fout)
    mfa_reset = form.get("mfa_uitschakelen") == "1"
    with request.app.state.db() as conn:
        rij = conn.execute("SELECT gebruikersnaam FROM gebruikers WHERE id = ?", (gebruiker_id,)).fetchone()
        if rij is None:
            raise HTTPException(404, "Gebruiker niet gevonden.")
        conn.execute("UPDATE gebruikers SET wachtwoord_hash = ? WHERE id = ?", (hash_wachtwoord(ww), gebruiker_id))
        if mfa_reset:
            conn.execute(
                "UPDATE gebruikers SET totp_geheim = NULL, totp_nieuw = NULL, totp_laatste_stap = NULL WHERE id = ?",
                (gebruiker_id,),
            )
        # Alle sessies van die gebruiker beëindigen.
        conn.execute("DELETE FROM sessies WHERE gebruiker_id = ?", (gebruiker_id,))
    log(
        request,
        "gebruiker.wachtwoord_gereset",
        {"gebruikersnaam": rij["gebruikersnaam"], "mfa_uitgeschakeld": mfa_reset},
    )
    return RedirectResponse("/beheer?m=wachtwoord_gereset", status_code=303)


# ---------------------------------------------------------------- eigen koppelsleutels (UI)


@router.post("/beheer/koppelsleutels", response_class=HTMLResponse)
async def koppelsleutel_maken(request: Request):
    sessie = vereis_beheerder(request)
    form = await controleer_post(request)
    naam = veld(form, "naam", 100)
    if not naam:
        return _fout(request, "Geef de koppelsleutel een naam.")
    with request.app.state.db() as conn:
        sleutel_id, sleutel = koppeling.maak_sleutel(conn, naam, sessie["gebruikersnaam"])
    log(request, "koppelsleutel.aangemaakt", {"id": sleutel_id, "naam": naam, "voorvoegsel": sleutel[:8]})
    return render(request, "beheer.html", nieuwe_sleutel={"naam": naam, "sleutel": sleutel}, **beheer_ctx(request))


@router.post("/beheer/koppelsleutels/{sleutel_id}/intrekken")
async def koppelsleutel_intrekken(request: Request, sleutel_id: int):
    vereis_beheerder(request)
    await controleer_post(request)
    with request.app.state.db() as conn:
        if koppeling.trek_in(conn, sleutel_id):
            log(request, "koppelsleutel.ingetrokken", {"id": sleutel_id})
    return RedirectResponse("/beheer?m=sleutel_ingetrokken", status_code=303)


# ---------------------------------------------------------------- eigen koppelsleutels (JSON-beheer-API)


@router.get("/api/beheer/koppelsleutels")
def api_sleutels(request: Request):
    vereis_beheerder(request)
    with request.app.state.db() as conn:
        return koppeling.lijst_sleutels(conn)


@router.post("/api/beheer/koppelsleutels", status_code=201)
async def api_sleutel_maken(request: Request):
    sessie = vereis_beheerder(request)
    await controleer_post(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    naam = body.get("naam").strip()[:100] if isinstance(body, dict) and isinstance(body.get("naam"), str) else ""
    if not naam:
        return JSONResponse({"fout": "naam_verplicht"}, status_code=422)
    with request.app.state.db() as conn:
        sleutel_id, sleutel = koppeling.maak_sleutel(conn, naam, sessie["gebruikersnaam"])
    log(request, "koppelsleutel.aangemaakt", {"id": sleutel_id, "naam": naam, "voorvoegsel": sleutel[:8]})
    return {"id": sleutel_id, "naam": naam, "voorvoegsel": sleutel[:8], "sleutel": sleutel}


@router.delete("/api/beheer/koppelsleutels/{sleutel_id}")
async def api_sleutel_intrekken(request: Request, sleutel_id: int):
    vereis_beheerder(request)
    await controleer_post(request)
    with request.app.state.db() as conn:
        if not koppeling.trek_in(conn, sleutel_id):
            return JSONResponse({"fout": "niet_gevonden"}, status_code=404)
    log(request, "koppelsleutel.ingetrokken", {"id": sleutel_id})
    return Response(status_code=204)


# ---------------------------------------------------------------- back-up van de configuratie


def _herbevestig(request: Request, sessie, form: dict) -> str | None:
    with request.app.state.db() as conn:
        rij = conn.execute("SELECT wachtwoord_hash FROM gebruikers WHERE id = ?", (sessie["id"],)).fetchone()
    if not controleer_wachtwoord(rij["wachtwoord_hash"], ruw(form, "eigen_wachtwoord")):
        return "Uw eigen wachtwoord klopt niet (nodig ter bevestiging)."
    return None


@router.post("/beheer/back-up/exporteren")
async def back_up_exporteren(request: Request):
    sessie = vereis_beheerder(request)
    form = await controleer_post(request)
    fout = _herbevestig(request, sessie, form)
    herstel = ruw(form, "herstelwachtwoord")
    fout = fout or wachtwoord_fout(herstel, ruw(form, "herstelwachtwoord2"))
    if fout:
        return _fout(request, fout)
    data = await asyncio.to_thread(back_up.exporteer, request.app.state.db, request.app.state.kluis, herstel)
    log(request, "back_up.geexporteerd")
    naam = f"bedrijfsdashboard-configuratie-{iso()[:10]}.json"
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{naam}"', "Cache-Control": "no-store"},
    )


@router.post("/beheer/back-up/terugzetten", response_class=HTMLResponse)
async def back_up_terugzetten(request: Request):
    sessie = vereis_beheerder(request)
    form = await controleer_post(request)
    fout = _herbevestig(request, sessie, form)
    if fout:
        return _fout(request, fout)
    bestand = form.get("bestand")
    if bestand is None or not hasattr(bestand, "read"):
        return _fout(request, "Kies een exportbestand.")
    data = await bestand.read(back_up.MAX_GROOTTE + 1)
    db, kluis = request.app.state.db, request.app.state.kluis
    try:
        inhoud = await asyncio.to_thread(back_up.ontsleutel, data, ruw(form, "herstelwachtwoord"))
        teller = back_up.zet_terug(db, kluis, inhoud, valideer_bron)
    except back_up.BackUpFout as exc:
        return _fout(request, f"Terugzetten mislukt: {exc}")
    log(request, "back_up.teruggezet", teller)
    request.app.state.planner.nu_ophalen.set()
    return render(request, "beheer.html", terugzet_resultaat=teller, **beheer_ctx(request))


# ---------------------------------------------------------------- auditlog


def _pagina_nummer(request: Request) -> int:
    try:
        return max(1, min(1_000_000, int(request.query_params.get("pagina", "1"))))
    except ValueError:
        return 1


@router.get("/beheer/audit", response_class=HTMLResponse)
def audit_pagina(request: Request):
    vereis_beheerder(request)
    actie = request.query_params.get("actie", "")[:64] or None
    data = audit.pagina(request.app.state.db, _pagina_nummer(request), actie=actie)
    bewaar = instellingen.waarde(request.app.state.db, "audit_bewaardagen")
    return render(request, "audit.html", audit=data, actie=actie or "", acties=audit.ACTIES, bewaardagen=bewaar)


@router.get("/api/beheer/audit")
def audit_api(request: Request):
    vereis_beheerder(request)
    try:
        per_pagina = max(1, min(500, int(request.query_params.get("per_pagina", str(audit.PER_PAGINA)))))
    except ValueError:
        return JSONResponse({"fout": "ongeldige_parameter", "parameter": "per_pagina"}, status_code=422)
    actie = request.query_params.get("actie", "")[:64] or None
    return audit.pagina(request.app.state.db, _pagina_nummer(request), per_pagina, actie)
