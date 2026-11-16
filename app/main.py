"""FastAPI-applicatie: webinterface, dashboard, datasets-proxy, tv-weergave en eigen koppel-API."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, instellingen, koppeling, onderhoud, overzicht, routes_auth, routes_beheer
from .beveiliging import AanmeldBeperking, is_https, sha256
from .config import Config, laad_config
from .db import Database, migreer
from .ophalen import (
    DATASET_NAAM,
    OphaalFout,
    Planner,
    haal_dataset,
    historie_dagen,
    interval_minuten,
)
from .tijd import iso, leesbaar
from .versleuteling import Kluis
from .web import (
    PRE_CSRF_COOKIE,
    GeenRechten,
    MfaVereist,
    NietAangemeld,
    gebruiker,
    heeft_gebruikers,
    mfa_ontbreekt,
    vereis_gebruiker,
)

log = logging.getLogger("bedrijfsdashboard")
HIER = Path(__file__).parent
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
    "wachtwoord_gewijzigd": "Uw wachtwoord is gewijzigd. Andere sessies zijn afgemeld.",
    "wachtwoord_gereset": "Wachtwoord gereset. De gebruiker is overal afgemeld.",
    "mfa_aan": "MFA is ingeschakeld. Bij de volgende aanmelding wordt om een code gevraagd.",
    "mfa_uit": "MFA is uitgeschakeld.",
    "mfa_vereist": "MFA is verplicht voor beheerders. Stel eerst MFA in om Beheer te gebruiken.",
    "testmail": "Testmail verstuurd.",
}


def _ruwe_bron_db(db: Database, bron_id: int):
    with db() as conn:
        rij = conn.execute("SELECT * FROM bronnen WHERE id = ?", (bron_id,)).fetchone()
    if rij is None:
        raise HTTPException(404, "Bron niet gevonden.")
    return rij


# ---------------------------------------------------------------- app-fabriek


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or laad_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.achtergrond:
            app.state.planner.start()
        yield
        await app.state.planner.stop()

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
    app.state.planner = Planner(
        app.state.db,
        app.state.kluis,
        onderhoud=lambda: onderhoud.misschien_opschonen(app.state.db, app.state.aanmeldbeperking),
    )

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
    app.include_router(routes_auth.router)
    app.include_router(routes_beheer.router)

    def render(request: Request, sjabloon: str, status_code: int = 200, **ctx) -> HTMLResponse:
        sessie = gebruiker(request)
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
                "mfa_ontbreekt": mfa_ontbreekt(request, sessie),
                **ctx,
            },
            status_code=status_code,
        )
        if pre_token and request.cookies.get(PRE_CSRF_COOKIE) != pre_token:
            resp.set_cookie(PRE_CSRF_COOKIE, pre_token, httponly=True, samesite="strict", secure=is_https(request))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.state.render = render

    # ------------------------------------------------------------ foutafhandeling

    @app.exception_handler(NietAangemeld)
    async def _niet_aangemeld(request: Request, exc: NietAangemeld):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"fout": "niet_aangemeld"}, status_code=401)
        doel = "/aanmelden" if heeft_gebruikers(app.state.db) else "/welkom"
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

    @app.exception_handler(MfaVereist)
    async def _mfa_vereist(request: Request, exc: MfaVereist):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"fout": "mfa_vereist"}, status_code=403)
        return RedirectResponse("/account?m=mfa_vereist", status_code=303)

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

    # ------------------------------------------------------------ tv-weergave (optioneel, standaard uit)

    @app.get("/tv/{token}", response_class=HTMLResponse)
    def tv(request: Request, token: str):
        db = app.state.db
        verwacht = instellingen.waarde(db, "tv_token_hash")
        if (
            not instellingen.aan(db, "tv_ingeschakeld")
            or not verwacht
            or not hmac.compare_digest(verwacht, sha256(token[:200]))
        ):
            raise HTTPException(404, "Deze pagina bestaat niet.")
        lijst = overzicht.bronnen(db)
        meldingen = overzicht.gebundelde_meldingen(lijst)
        resp = render(
            request,
            "tv.html",
            bronnen=lijst,
            kritiek=[m for m in meldingen if m["niveau"] == "kritiek"],
            waarschuwingen=[m for m in meldingen if m["niveau"] == "waarschuwing"],
            onbereikbaar=[b for b in lijst if b["fout"]],
            bedrijfsnaam=db.instelling("bedrijfsnaam"),
            nu=iso(),
        )
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    # ------------------------------------------------------------ dashboard

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not heeft_gebruikers(app.state.db):
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
        return _ruwe_bron_db(app.state.db, bron_id)

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
            body = {"fout": exc.tekst}
            if exc.code:
                body["code"] = exc.code
            return JSONResponse(body, status_code=code)

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

    return app


def _cel(waarde) -> str:
    if waarde is None:
        return "–"
    if isinstance(waarde, bool):
        return "ja" if waarde else "nee"
    if isinstance(waarde, (dict, list)):
        return json.dumps(waarde, ensure_ascii=False)[:300]
    return str(waarde)[:300]
