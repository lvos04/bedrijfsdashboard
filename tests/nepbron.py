"""Een nep-bron die de Bedrijfskoppeling API v1 implementeert, voor tests."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

GELDIGE_SLEUTEL = "bk_" + "a" * 43


def maak_nepbron(slug: str = "scouten", sleutel: str = GELDIGE_SLEUTEL) -> FastAPI:
    app = FastAPI()
    app.state.status = 200
    app.state.kpis = [
        {
            "sleutel": "open_waarnemingen",
            "label": "Open waarnemingen",
            "waarde": 7,
            "eenheid": None,
            "status": "let_op",
            "toelichting": "2 met hoge druk",
        },
        {
            "sleutel": "gemiddeld_gewicht",
            "label": "Gemiddeld gewicht",
            "waarde": "12.50",
            "eenheid": "g",
            "status": "ok",
            "toelichting": None,
        },
        {
            "sleutel": "onbekend",
            "label": "Nog niet berekend",
            "waarde": None,
            "eenheid": None,
            "status": "raar",
            "toelichting": None,
        },
    ]
    app.state.meldingen = [
        {"niveau": "kritiek", "tekst": "Trips boven drempel in afdeling 3", "sinds": "2026-11-15T08:00:00Z"},
        {"niveau": "waarschuwing", "tekst": "Val 12 niet geleegd", "sinds": "2026-11-14T08:00:00Z"},
    ]
    app.state.aanroepen = []
    app.state.fout_body = {"fout": "kapot"}

    def _auth(request: Request):
        app.state.aanroepen.append((request.url.path, request.headers.get("authorization")))
        if app.state.status != 200:
            return JSONResponse(app.state.fout_body, status_code=app.state.status)
        if request.headers.get("authorization") != f"Bearer {sleutel}":
            return JSONResponse({"fout": "ongeldige_koppelsleutel"}, status_code=401)
        return None

    @app.get("/api/koppeling/v1/info")
    def info(request: Request):
        if (r := _auth(request)) is not None:
            return r
        return {
            "app": slug,
            "naam": "Scouten",
            "versie": "1.2.3",
            "api_versie": "1",
            "bedrijf": "Testkwekerij",
            "datasets": ["waarnemingen"],
            "tijd": "2026-11-16T15:00:00Z",
        }

    @app.get("/api/koppeling/v1/samenvatting")
    def samenvatting(request: Request):
        if (r := _auth(request)) is not None:
            return r
        return {
            "app": slug,
            "gegenereerd_op": "2026-11-16T15:00:00Z",
            "kpis": app.state.kpis,
            "meldingen": app.state.meldingen,
        }

    @app.get("/api/koppeling/v1/datasets/{naam}")
    def dataset(request: Request, naam: str, limiet: int = 100):
        if (r := _auth(request)) is not None:
            return r
        if naam != "waarnemingen":
            return JSONResponse({"fout": "onbekende_dataset"}, status_code=404)
        items = [
            {"id": i, "plaag": "trips", "aantal": i * 2, "bijgewerkt_op": "2026-11-16T10:00:00Z"} for i in range(1, 6)
        ][:limiet]
        return {
            "app": slug,
            "dataset": naam,
            "gegenereerd_op": "2026-11-16T15:00:00Z",
            "items": items,
            "volgende_cursor": None,
        }

    return app
