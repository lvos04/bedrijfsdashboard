"""FastAPI-applicatie: webinterface, beheer, datasets-proxy en eigen koppel-API."""

from __future__ import annotations

import logging
import re
import secrets
import ssl
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, koppeling, overzicht
from .beveiliging import (
    SESSIE_COOKIE,
    SESSIE_DUUR,
    AanmeldBeperking,
    controleer_wachtwoord,
    csrf_ok,
    haal_sessie,
    hash_wachtwoord,
    is_https,
    maak_sessie,
    oorsprong_ok,
    verwijder_sessie,
    wachtwoord_fout,
)
from .config import Config, laad_config
from .db import Database, migreer
from .ophalen import (
    DATASET_NAAM,
    OphaalFout,
    Planner,
    haal_alle_op,
    haal_bron_op,
    haal_dataset,
    historie_dagen,
    interval_minuten,
    test_verbinding,
)
from .tijd import iso, leesbaar
from .versleuteling import Kluis

log = logging.getLogger("bedrijfsdashboard")
HIER = Path(__file__).parent
PRE_CSRF_COOKIE = "bd_csrf"
SLUG_PATROON = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")
BEKENDE_APPS = {
    "milieu-registratie": "Teeltregistratie / milieu",
    "gewichtsverwachting": "Gewichtsverwachting",
    "scouten": "Scouten",
    "tuinwerk-os": "TuinWerk OS",
}
MELDINGEN = {
    "bron_opgeslagen": "Bron opgeslagen.",
    "bron_verwijderd": "Bron verwijderd.",
    "instellingen_opgeslagen": "Instellingen opgeslagen.",
    "gebruiker_toegevoegd": "Gebruiker toegevoegd.",
    "gebruiker_verwijderd": "Gebruiker verwijderd.",
    "sleutel_ingetrokken": "Koppelsleutel ingetrokken.",
    "opgehaald": "Alle bronnen zijn opnieuw opgehaald.",
}


class NietAangemeld(Exception):
    pass


class GeenRechten(Exception):
    pass


# ---------------------------------------------------------------- hulpfuncties


def _gebruiker(request: Request):
    if not hasattr(request.state, "sessie"):
        with request.app.state.db() as conn:
            request.state.sessie = haal_sessie(conn, request.cookies.get(SESSIE_COOKIE))
    return request.state.sessie


def _heeft_gebruikers(db: Database) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM gebruikers LIMIT 1").fetchone() is not None


def vereis_gebruiker(request: Request):
    sessie = _gebruiker(request)
    if sessie is None:
        raise NietAangemeld()
    return sessie


def vereis_beheerder(request: Request):
    sessie = vereis_gebruiker(request)
    if sessie["rol"] != "beheerder":
        raise GeenRechten()
    return sessie


async def controleer_post(request: Request) -> dict:
    """Oorsprong + CSRF-token (formulierveld csrf_token of header X-CSRF-Token)."""
    if not oorsprong_ok(request):
        raise HTTPException(403, "Verzoek geweigerd: onbekende oorsprong.")
    form = {}
    if request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = dict(await request.form())
    gegeven = form.get("csrf_token") or request.headers.get("x-csrf-token")
    sessie = _gebruiker(request)
    verwacht = sessie["csrf_token"] if sessie else request.cookies.get(PRE_CSRF_COOKIE)
    if not verwacht or not csrf_ok(verwacht, gegeven if isinstance(gegeven, str) else None):
        raise HTTPException(403, "Verzoek geweigerd: ongeldig of ontbrekend CSRF-token. Herlaad de pagina.")
    return form


def _veld(form: dict, naam: str, max_len: int = 500) -> str:
    waarde = form.get(naam)
    return waarde.strip()[:max_len] if isinstance(waarde, str) else ""


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "onbekend"


def _zet_sessiecookie(request: Request, response: Response, token: str) -> None:
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


# ---------------------------------------------------------------- app-fabriek


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or laad_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        planner = Planner(app.state.db, app.state.kluis)
        app.state.planner = planner
        if cfg.achtergrond:
            planner.start()
        yield
        await planner.stop()

    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        proef = cfg.data_dir / ".schrijftest"
        proef.write_text("ok")
        proef.unlink()
    except OSError as exc:
        raise SystemExit(
            f"DATA_DIR '{cfg.data_dir}' is niet schrijfbaar voor deze gebruiker ({exc}). "
            "Geef de containergebruiker (standaard UID/GID 568 'apps') schrijfrechten op de dataset."
        ) from exc
    migreer(cfg.db_pad)

    app = FastAPI(
        title="Bedrijfsdashboard",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.db = Database(cfg.db_pad)
    app.state.kluis = Kluis(cfg.sleutel_pad)
    app.state.aanmeldbeperking = AanmeldBeperking()
    app.state.koppel_limiter = koppeling._Limiter(koppeling.LIMIET_PER_MINUUT)
    app.state.http_transport = None  # tests kunnen hier een nep-transport zetten
    app.state.planner = Planner(app.state.db, app.state.kluis)

    templates = Jinja2Templates(directory=str(HIER / "templates"))
    templates.env.globals.update(
        leesbaar=leesbaar,
        getal=overzicht.formatteer_getal,
        status_label=overzicht.STATUS_LABEL,
        versie=__version__,
        bekende_apps=BEKENDE_APPS,
    )
    app.mount("/static", StaticFiles(directory=str(HIER / "static")), name="static")
    app.include_router(koppeling.router)

    def render(request: Request, sjabloon: str, status_code: int = 200, **ctx) -> HTMLResponse:
        sessie = _gebruiker(request)
        pre_token = None
        if sessie is None:
            pre_token = request.cookies.get(PRE_CSRF_COOKIE) or secrets.token_urlsafe(32)
        melding = MELDINGEN.get(request.query_params.get("m", ""))
        resp = templates.TemplateResponse(
            request,
            sjabloon,
            {
                "gebruiker": sessie,
                "csrf_token": sessie["csrf_token"] if sessie else pre_token,
                "melding": melding,
                **ctx,
            },
            status_code=status_code,
        )
        if pre_token and request.cookies.get(PRE_CSRF_COOKIE) != pre_token:
            resp.set_cookie(PRE_CSRF_COOKIE, pre_token, httponly=True, samesite="strict", secure=is_https(request))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ foutafhandeling

    @app.exception_handler(NietAangemeld)
    async def _niet_aangemeld(request: Request, exc: NietAangemeld):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"fout": "niet_aangemeld"}, status_code=401)
        doel = "/aanmelden" if _heeft_gebruikers(app.state.db) else "/welkom"
        return RedirectResponse(doel, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_fout(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or exc.status_code == 405:
            return JSONResponse({"fout": exc.detail}, status_code=exc.status_code)
        titel = {403: "Geweigerd", 404: "Niet gevonden"}.get(exc.status_code, "Fout")
        tekst = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "Deze pagina bestaat niet."
        return render(request, "fout.html", status_code=exc.status_code, titel=titel, tekst=tekst)

    @app.exception_handler(GeenRechten)
    async def _geen_rechten(request: Request, exc: GeenRechten):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"fout": "geen_rechten"}, status_code=403)
        return render(
            request,
            "fout.html",
            status_code=403,
            titel="Geen toegang",
            tekst="Alleen beheerders hebben toegang tot deze pagina.",
        )

    @app.middleware("http")
    async def _beveiligingskoppen(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        return resp

    # ------------------------------------------------------------ algemeen

    @app.get("/gezondheid", include_in_schema=False)
    def gezondheid():
        with app.state.db() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "versie": __version__}

    @app.get("/welkom", response_class=HTMLResponse)
    def welkom(request: Request):
        if _heeft_gebruikers(app.state.db):
            return RedirectResponse("/aanmelden", status_code=303)
        return render(request, "welkom.html")

    @app.post("/welkom")
    async def welkom_post(request: Request):
        form = await controleer_post(request)
        naam = _veld(form, "gebruikersnaam", 64)
        ww, ww2 = form.get("wachtwoord") or "", form.get("wachtwoord2") or ""
        bedrijf = _veld(form, "bedrijfsnaam", 200)
        fout = None if naam else "Vul een gebruikersnaam in."
        fout = fout or wachtwoord_fout(ww, ww2)
        if fout:
            return render(request, "welkom.html", status_code=422, fout=fout, gebruikersnaam=naam, bedrijfsnaam=bedrijf)
        with app.state.db.transactie() as conn:
            # Dubbele controle binnen de transactie: de wizard kan maar één keer worden doorlopen.
            if conn.execute("SELECT 1 FROM gebruikers LIMIT 1").fetchone():
                return RedirectResponse("/aanmelden", status_code=303)
            cur = conn.execute(
                "INSERT INTO gebruikers (gebruikersnaam, wachtwoord_hash, rol, aangemaakt_op) VALUES (?, ?, 'beheerder', ?)",
                (naam, hash_wachtwoord(ww), iso()),
            )
            if bedrijf:
                conn.execute(
                    "INSERT OR REPLACE INTO instellingen (sleutel, waarde) VALUES ('bedrijfsnaam', ?)", (bedrijf,)
                )
            token = maak_sessie(conn, cur.lastrowid)
        resp = RedirectResponse("/beheer", status_code=303)
        _zet_sessiecookie(request, resp, token)
        resp.delete_cookie(PRE_CSRF_COOKIE)
        return resp

    @app.get("/aanmelden", response_class=HTMLResponse)
    def aanmelden(request: Request):
        if not _heeft_gebruikers(app.state.db):
            return RedirectResponse("/welkom", status_code=303)
        if _gebruiker(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "aanmelden.html")

    @app.post("/aanmelden")
    async def aanmelden_post(request: Request):
        form = await controleer_post(request)
        naam = _veld(form, "gebruikersnaam", 64)
        ww = form.get("wachtwoord") or ""
        beperking: AanmeldBeperking = app.state.aanmeldbeperking
        sleutels = (f"ip:{_client_ip(request)}", f"naam:{naam.lower()}")
        if beperking.geblokkeerd(*sleutels):
            return render(
                request,
                "aanmelden.html",
                status_code=429,
                gebruikersnaam=naam,
                fout="Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw.",
            )
        with app.state.db() as conn:
            rij = conn.execute(
                "SELECT id, wachtwoord_hash FROM gebruikers WHERE gebruikersnaam = ?", (naam,)
            ).fetchone()
        if not controleer_wachtwoord(rij["wachtwoord_hash"] if rij else None, ww if isinstance(ww, str) else ""):
            beperking.fout(*sleutels)
            return render(
                request,
                "aanmelden.html",
                status_code=401,
                gebruikersnaam=naam,
                fout="Onjuiste gebruikersnaam of wachtwoord.",
            )
        beperking.reset(*sleutels)
        with app.state.db() as conn:
            token = maak_sessie(conn, rij["id"])
        resp = RedirectResponse("/", status_code=303)
        _zet_sessiecookie(request, resp, token)
        resp.delete_cookie(PRE_CSRF_COOKIE)
        return resp

    @app.post("/afmelden")
    async def afmelden(request: Request):
        await controleer_post(request)
        with app.state.db() as conn:
            verwijder_sessie(conn, request.cookies.get(SESSIE_COOKIE))
        resp = RedirectResponse("/aanmelden", status_code=303)
        resp.delete_cookie(SESSIE_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------ dashboard

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not _heeft_gebruikers(app.state.db):
            return RedirectResponse("/welkom", status_code=303)
        vereis_gebruiker(request)
        lijst = overzicht.bronnen(app.state.db)
        meldingen = overzicht.gebundelde_meldingen(lijst)
        return render(
            request,
            "overzicht.html",
            bronnen=lijst,
            kritiek=[m for m in meldingen if m["niveau"] == "kritiek"],
            waarschuwingen=[m for m in meldingen if m["niveau"] == "waarschuwing"],
            info_meldingen=[m for m in meldingen if m["niveau"] == "info"],
            onbereikbaar=[b for b in lijst if b["fout"]],
            bedrijfsnaam=app.state.db.instelling("bedrijfsnaam"),
            interval=interval_minuten(app.state.db),
        )

    def _bron_of_404(bron_id: int) -> dict:
        b = overzicht.bron(app.state.db, bron_id)
        if b is None:
            raise HTTPException(404, "Bron niet gevonden.")
        return b

    @app.get("/bronnen/{bron_id}", response_class=HTMLResponse)
    def bron_detail(request: Request, bron_id: int):
        vereis_gebruiker(request)
        b = _bron_of_404(bron_id)
        historie = overzicht.kpi_historie(app.state.db, bron_id, historie_dagen(app.state.db))
        sparklines = {s: overzicht.sparkline(p, label=s) for s, p in historie.items()}
        return render(
            request,
            "bron.html",
            bron=b,
            sparklines=sparklines,
            meldingen=overzicht.gebundelde_meldingen([b]),
            dagen=historie_dagen(app.state.db),
        )

    def _ruwe_bron(bron_id: int):
        with app.state.db() as conn:
            rij = conn.execute("SELECT * FROM bronnen WHERE id = ?", (bron_id,)).fetchone()
        if rij is None:
            raise HTTPException(404, "Bron niet gevonden.")
        return rij

    def _dataset_params(request: Request) -> dict:
        params = {}
        for k in ("sinds", "limiet", "cursor"):
            v = request.query_params.get(k)
            if v:
                params[k] = v[:500]
        if "sinds" in params:
            # Invoer zonder tijdzone (datetime-local) interpreteren als lokale tijd en als UTC doorgeven.
            try:
                dt = datetime.fromisoformat(params["sinds"].replace("Z", "+00:00"))
            except ValueError:
                raise OphaalFout("Ongeldige waarde voor 'sinds'", 422) from None
            params["sinds"] = iso(dt if dt.tzinfo else dt.astimezone())
        return params

    async def _proxy_dataset(bron_id: int, naam: str, params: dict) -> dict:
        b = overzicht.bron(app.state.db, bron_id)
        if b is None:
            raise HTTPException(404, "Bron niet gevonden.")
        if not DATASET_NAAM.match(naam):
            raise OphaalFout("Ongeldige datasetnaam", 422)
        bekend = (b["info"] or {}).get("datasets")
        if bekend is not None and naam not in bekend:
            raise OphaalFout("Deze dataset wordt niet aangeboden door de bron", 404)
        return await haal_dataset(_ruwe_bron(bron_id), app.state.kluis, naam, params, app.state.http_transport)

    @app.get("/api/bronnen/{bron_id}/datasets/{naam}")
    async def api_dataset(request: Request, bron_id: int, naam: str):
        vereis_gebruiker(request)
        try:
            return await _proxy_dataset(bron_id, naam, _dataset_params(request))
        except OphaalFout as exc:
            code = exc.status_code if exc.status_code in (404, 422) else 502
            return JSONResponse({"fout": exc.tekst}, status_code=code)

    @app.get("/bronnen/{bron_id}/datasets/{naam}", response_class=HTMLResponse)
    async def dataset_pagina(request: Request, bron_id: int, naam: str):
        vereis_gebruiker(request)
        b = _bron_of_404(bron_id)
        params: dict = {}
        try:
            params = _dataset_params(request)
            params.setdefault("limiet", "50")
            data = await _proxy_dataset(bron_id, naam, params)
            fout = None
        except OphaalFout as exc:
            data, fout = None, exc.tekst
        kolommen: list[str] = []
        rijen: list[list[str]] = []
        if data:
            for item in data["items"]:
                if isinstance(item, dict):
                    for k in item:
                        if k not in kolommen:
                            kolommen.append(k)
            for item in data["items"]:
                if isinstance(item, dict):
                    rijen.append([_cel(item.get(k)) for k in kolommen])
        return render(
            request,
            "dataset.html",
            bron=b,
            naam=naam,
            fout=fout,
            kolommen=kolommen,
            rijen=rijen,
            volgende_cursor=(data or {}).get("volgende_cursor"),
            params=params,
            sinds_invoer=request.query_params.get("sinds", "")[:16],
        )

    # ------------------------------------------------------------ beheer

    def _beheer_ctx() -> dict:
        db = app.state.db
        with db() as conn:
            gebruikers = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, gebruikersnaam, rol, aangemaakt_op FROM gebruikers ORDER BY gebruikersnaam COLLATE NOCASE"
                )
            ]
            sleutels = koppeling.lijst_sleutels(conn)
        return {
            "bronnen": overzicht.bronnen(db, alleen_actief=False),
            "gebruikers": gebruikers,
            "koppelsleutels": sleutels,
            "interval": interval_minuten(db),
            "historie_dagen": historie_dagen(db),
            "bedrijfsnaam": db.instelling("bedrijfsnaam") or "",
        }

    @app.get("/beheer", response_class=HTMLResponse)
    def beheer(request: Request):
        vereis_beheerder(request)
        return render(request, "beheer.html", **_beheer_ctx())

    @app.post("/beheer/ophalen")
    async def beheer_ophalen(request: Request):
        vereis_beheerder(request)
        await controleer_post(request)
        await haal_alle_op(app.state.db, app.state.kluis, app.state.http_transport)
        return RedirectResponse("/beheer?m=opgehaald", status_code=303)

    @app.post("/beheer/instellingen")
    async def beheer_instellingen(request: Request):
        vereis_beheerder(request)
        form = await controleer_post(request)
        try:
            interval = int(_veld(form, "interval_minuten", 5))
            dagen = int(_veld(form, "historie_dagen", 5))
            if not (1 <= interval <= 1440 and 1 <= dagen <= 3650):
                raise ValueError
        except ValueError:
            return render(
                request,
                "beheer.html",
                status_code=422,
                **_beheer_ctx(),
                fout="Interval moet 1–1440 minuten zijn en historie 1–3650 dagen.",
            )
        db = app.state.db
        db.zet_instelling("interval_minuten", str(interval))
        db.zet_instelling("historie_dagen", str(dagen))
        db.zet_instelling("bedrijfsnaam", _veld(form, "bedrijfsnaam", 200))
        app.state.planner.nu_ophalen.set()
        return RedirectResponse("/beheer?m=instellingen_opgeslagen", status_code=303)

    # --- bronnen

    @app.get("/beheer/bronnen/nieuw", response_class=HTMLResponse)
    def bron_nieuw(request: Request):
        vereis_beheerder(request)
        return render(request, "bron_formulier.html", bron=None, waarden={})

    @app.get("/beheer/bronnen/{bron_id}", response_class=HTMLResponse)
    def bron_bewerken(request: Request, bron_id: int):
        vereis_beheerder(request)
        b = _bron_of_404(bron_id)
        return render(request, "bron_formulier.html", bron=b, waarden=b)

    async def _bron_opslaan(request: Request, bron_id: int | None):
        vereis_beheerder(request)
        form = await controleer_post(request)
        bestaand = _bron_of_404(bron_id) if bron_id else None
        waarden = {
            "naam": _veld(form, "naam", 100),
            "app_slug": _veld(form, "app_slug", 63).lower(),
            "basis_url": _veld(form, "basis_url", 500).rstrip("/"),
            "actief": form.get("actief") == "1" if bron_id else True,
        }
        sleutel = _veld(form, "koppelsleutel", 500)
        ca_pem = form.get("ca_pem") if isinstance(form.get("ca_pem"), str) else ""
        ca_bestand = form.get("ca_bestand")
        if ca_bestand is not None and hasattr(ca_bestand, "read"):
            inhoud = await ca_bestand.read(200_000)
            if inhoud:
                ca_pem = inhoud.decode("utf-8", errors="replace")
        ca_pem = (ca_pem or "").strip()
        ca_wissen = form.get("ca_wissen") == "1"

        fout = None
        if not waarden["naam"]:
            fout = "Vul een naam in."
        elif not SLUG_PATROON.match(waarden["app_slug"]):
            fout = "De app-slug mag alleen kleine letters, cijfers en koppeltekens bevatten (bijv. scouten)."
        else:
            fout = valideer_url(waarden["basis_url"])
        if not fout and not bron_id and not sleutel:
            fout = "Vul de koppelsleutel in die u in de bron-app heeft aangemaakt."
        if not fout and ca_pem:
            fout = valideer_ca(ca_pem)
        if fout:
            return render(request, "bron_formulier.html", status_code=422, bron=bestaand, waarden=waarden, fout=fout)

        kluis: Kluis = app.state.kluis
        with app.state.db.transactie() as conn:
            if bron_id is None:
                cur = conn.execute(
                    "INSERT INTO bronnen (naam, app_slug, basis_url, sleutel_versleuteld, sleutel_voorvoegsel, ca_pem, "
                    "actief, aangemaakt_op) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        waarden["naam"],
                        waarden["app_slug"],
                        waarden["basis_url"],
                        kluis.versleutel(sleutel),
                        sleutel[:8],
                        ca_pem or None,
                        iso(),
                    ),
                )
                bron_id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE bronnen SET naam = ?, app_slug = ?, basis_url = ?, actief = ? WHERE id = ?",
                    (waarden["naam"], waarden["app_slug"], waarden["basis_url"], int(waarden["actief"]), bron_id),
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
        app.state.planner.nu_ophalen.set()
        return RedirectResponse("/beheer?m=bron_opgeslagen", status_code=303)

    @app.post("/beheer/bronnen")
    async def bron_toevoegen(request: Request):
        return await _bron_opslaan(request, None)

    @app.post("/beheer/bronnen/{bron_id}")
    async def bron_bijwerken(request: Request, bron_id: int):
        return await _bron_opslaan(request, bron_id)

    @app.post("/beheer/bronnen/{bron_id}/verwijderen")
    async def bron_verwijderen(request: Request, bron_id: int):
        vereis_beheerder(request)
        await controleer_post(request)
        with app.state.db() as conn:
            conn.execute("DELETE FROM bronnen WHERE id = ?", (bron_id,))
        return RedirectResponse("/beheer?m=bron_verwijderd", status_code=303)

    @app.post("/beheer/bronnen/{bron_id}/testen", response_class=HTMLResponse)
    async def bron_testen(request: Request, bron_id: int):
        vereis_beheerder(request)
        await controleer_post(request)
        rij = _ruwe_bron(bron_id)
        try:
            info = await test_verbinding(rij, app.state.kluis, app.state.http_transport)
            test = {"ok": True, "info": info, "bron_naam": rij["naam"]}
            if info.get("app") and info["app"] != rij["app_slug"]:
                test["waarschuwing"] = f"De bron meldt app-slug '{info['app']}', maar hier staat '{rij['app_slug']}'."
            await haal_bron_op(app.state.db, app.state.kluis, rij, app.state.http_transport)
        except OphaalFout as exc:
            test = {"ok": False, "fout": exc.tekst, "bron_naam": rij["naam"]}
        except Exception as exc:  # bijv. ongeldige CA
            test = {"ok": False, "fout": f"Fout: {exc}", "bron_naam": rij["naam"]}
        return render(request, "beheer.html", test=test, **_beheer_ctx())

    # --- gebruikers

    @app.post("/beheer/gebruikers")
    async def gebruiker_toevoegen(request: Request):
        vereis_beheerder(request)
        form = await controleer_post(request)
        naam = _veld(form, "gebruikersnaam", 64)
        rol = _veld(form, "rol", 20)
        ww = form.get("wachtwoord") or ""
        fout = None if naam else "Vul een gebruikersnaam in."
        fout = fout or (None if rol in ("beheerder", "lezer") else "Ongeldige rol.")
        fout = fout or wachtwoord_fout(ww if isinstance(ww, str) else "")
        if not fout:
            with app.state.db() as conn:
                if conn.execute("SELECT 1 FROM gebruikers WHERE gebruikersnaam = ?", (naam,)).fetchone():
                    fout = "Deze gebruikersnaam bestaat al."
                else:
                    conn.execute(
                        "INSERT INTO gebruikers (gebruikersnaam, wachtwoord_hash, rol, aangemaakt_op) VALUES (?, ?, ?, ?)",
                        (naam, hash_wachtwoord(ww), rol, iso()),
                    )
        if fout:
            return render(request, "beheer.html", status_code=422, fout=fout, **_beheer_ctx())
        return RedirectResponse("/beheer?m=gebruiker_toegevoegd", status_code=303)

    @app.post("/beheer/gebruikers/{gebruiker_id}/verwijderen")
    async def gebruiker_verwijderen(request: Request, gebruiker_id: int):
        sessie = vereis_beheerder(request)
        await controleer_post(request)
        if gebruiker_id == sessie["id"]:
            return render(
                request, "beheer.html", status_code=422, fout="U kunt uzelf niet verwijderen.", **_beheer_ctx()
            )
        with app.state.db() as conn:
            conn.execute("DELETE FROM gebruikers WHERE id = ?", (gebruiker_id,))
        return RedirectResponse("/beheer?m=gebruiker_verwijderd", status_code=303)

    # --- eigen koppelsleutels (UI)

    @app.post("/beheer/koppelsleutels", response_class=HTMLResponse)
    async def koppelsleutel_maken(request: Request):
        sessie = vereis_beheerder(request)
        form = await controleer_post(request)
        naam = _veld(form, "naam", 100)
        if not naam:
            return render(
                request, "beheer.html", status_code=422, fout="Geef de koppelsleutel een naam.", **_beheer_ctx()
            )
        with app.state.db() as conn:
            _, sleutel = koppeling.maak_sleutel(conn, naam, sessie["gebruikersnaam"])
        return render(request, "beheer.html", nieuwe_sleutel={"naam": naam, "sleutel": sleutel}, **_beheer_ctx())

    @app.post("/beheer/koppelsleutels/{sleutel_id}/intrekken")
    async def koppelsleutel_intrekken(request: Request, sleutel_id: int):
        vereis_beheerder(request)
        await controleer_post(request)
        with app.state.db() as conn:
            koppeling.trek_in(conn, sleutel_id)
        return RedirectResponse("/beheer?m=sleutel_ingetrokken", status_code=303)

    # --- eigen koppelsleutels (JSON-beheer-API volgens contract)

    @app.get("/api/beheer/koppelsleutels")
    def api_sleutels(request: Request):
        vereis_beheerder(request)
        with app.state.db() as conn:
            return koppeling.lijst_sleutels(conn)

    @app.post("/api/beheer/koppelsleutels", status_code=201)
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
        with app.state.db() as conn:
            sleutel_id, sleutel = koppeling.maak_sleutel(conn, naam, sessie["gebruikersnaam"])
        return {"id": sleutel_id, "naam": naam, "voorvoegsel": sleutel[:8], "sleutel": sleutel}

    @app.delete("/api/beheer/koppelsleutels/{sleutel_id}")
    async def api_sleutel_intrekken(request: Request, sleutel_id: int):
        vereis_beheerder(request)
        await controleer_post(request)
        with app.state.db() as conn:
            if not koppeling.trek_in(conn, sleutel_id):
                return JSONResponse({"fout": "niet_gevonden"}, status_code=404)
        return Response(status_code=204)

    return app


def _cel(waarde) -> str:
    if waarde is None:
        return "–"
    if isinstance(waarde, bool):
        return "ja" if waarde else "nee"
    if isinstance(waarde, (dict, list)):
        import json

        return json.dumps(waarde, ensure_ascii=False)[:300]
    return str(waarde)[:300]
