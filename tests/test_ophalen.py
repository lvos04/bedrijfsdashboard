import asyncio

import httpx

from app import overzicht
from app.ophalen import getal, haal_alle_op, normaliseer_samenvatting

from .conftest import csrf, voeg_bron_toe


def _bron(app):
    with app.state.db() as conn:
        return conn.execute("SELECT * FROM bronnen").fetchone()


def _haal(app, transport=None):
    return asyncio.run(haal_alle_op(app.state.db, app.state.kluis, transport or app.state.http_transport))


def test_getal_normalisatie():
    assert getal(4) == 4 and getal("12.50") == 12.5 and getal("7") == 7
    assert getal(None) is None and getal("abc") is None and getal(True) is None and getal("NaN") is None


def test_normaliseer_onbekende_status():
    s = normaliseer_samenvatting(
        {"kpis": [{"sleutel": "a", "waarde": 1, "status": "raar"}], "meldingen": [{"niveau": "??", "tekst": "x"}]}
    )
    assert s["kpis"][0]["status"] == "onbekend"
    assert s["meldingen"][0]["niveau"] == "info"


def test_ophalen_succes_cache_en_historie(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    bron = _bron(app)
    resultaat = _haal(app)
    assert resultaat == {bron["id"]: None}
    # De bron kreeg de ontsleutelde sleutel als Bearer-token
    assert met_nepbron.state.aanroepen[0] == ("/api/koppeling/v1/info", "Bearer bk_" + "a" * 43)
    b = overzicht.bron(app.state.db, bron["id"])
    assert b["bereikbaar"] and not b["verouderd"]
    kpis = {k["sleutel"]: k for k in b["samenvatting"]["kpis"]}
    assert kpis["gemiddeld_gewicht"]["waarde"] == 12.5
    assert kpis["onbekend"]["waarde"] is None and kpis["onbekend"]["status"] == "onbekend"
    _haal(app)
    hist = overzicht.kpi_historie(app.state.db, bron["id"])
    assert len(hist["open_waarnemingen"]) == 2
    assert hist["onbekend"] == [(hist["onbekend"][0][0], None), (hist["onbekend"][1][0], None)]
    html = beheerder.get("/").text
    assert "Open waarnemingen" in html and "Trips boven drempel" in html
    assert "12,5" in html
    detail = beheerder.get(f"/bronnen/{bron['id']}").text
    assert '<svg class="sparkline"' in detail


def test_fout_401_bewaart_laatste_gegevens_als_verouderd(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    bron = _bron(app)
    _haal(app)
    met_nepbron.state.status = 401
    resultaat = _haal(app)
    assert "401" in resultaat[bron["id"]]
    b = overzicht.bron(app.state.db, bron["id"])
    assert not b["bereikbaar"] and b["verouderd"]
    assert b["samenvatting"]["kpis"]  # laatst bekende gegevens nog aanwezig
    eerste_fout_sinds = b["fout_sinds"]
    _haal(app)
    assert overzicht.bron(app.state.db, bron["id"])["fout_sinds"] == eerste_fout_sinds
    html = beheerder.get("/").text
    assert "Niet bereikbaar sinds" in html and "verouderd" in html
    # Herstel wist de fout
    met_nepbron.state.status = 200
    _haal(app)
    b = overzicht.bron(app.state.db, bron["id"])
    assert b["bereikbaar"] and b["fout"] is None and b["fout_sinds"] is None


def test_verkeerde_sleutel_401(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder, sleutel="bk_verkeerd")
    assert "Koppelsleutel geweigerd (401)" in list(_haal(app).values())[0]


def test_timeout(beheerder, app):
    voeg_bron_toe(beheerder)

    def handler(request):
        raise httpx.ReadTimeout("te traag", request=request)

    fout = list(_haal(app, httpx.MockTransport(handler)).values())[0]
    assert fout.startswith("Time-out")
    b = overzicht.bron(app.state.db, _bron(app)["id"])
    assert b["samenvatting"] is None and not b["bereikbaar"]
    html = beheerder.get("/").text
    assert "Niet bereikbaar sinds" in html


def test_verbindingsfout_en_500_en_geen_json(beheerder, app):
    voeg_bron_toe(beheerder)

    def weigert(request):
        raise httpx.ConnectError("Connection refused", request=request)

    assert "Verbinding mislukt" in list(_haal(app, httpx.MockTransport(weigert)).values())[0]
    assert "HTTP 500" in list(_haal(app, httpx.MockTransport(lambda r: httpx.Response(500))).values())[0]
    assert (
        "geen JSON" in list(_haal(app, httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))).values())[0]
    )
    assert "404" in list(_haal(app, httpx.MockTransport(lambda r: httpx.Response(404))).values())[0]


def test_verbinding_testen_knop(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    bron = _bron(app)
    r = beheerder.post(f"/beheer/bronnen/{bron['id']}/testen", data={"csrf_token": csrf(beheerder)})
    assert r.status_code == 200
    assert "Verbinding gelukt" in r.text and "1.2.3" in r.text
    met_nepbron.state.status = 401
    r = beheerder.post(f"/beheer/bronnen/{bron['id']}/testen", data={"csrf_token": csrf(beheerder)})
    assert "Verbinding mislukt" in r.text and "401" in r.text


def test_nu_ophalen_knop_en_historie_opruimen(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    with app.state.db() as conn:
        conn.execute(
            "INSERT INTO kpi_historie (bron_id, sleutel, tijd, waarde) VALUES (?, 'oud', '2000-01-01T00:00:00Z', 1)",
            (_bron(app)["id"],),
        )
    r = beheerder.post("/beheer/ophalen", data={"csrf_token": csrf(beheerder)}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kpi_historie WHERE sleutel = 'oud'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kpi_historie").fetchone()[0] == 3


def test_planner_draait_in_achtergrond(tmp_path, nepbron):
    from fastapi.testclient import TestClient

    import app.ophalen as oph
    from app.config import Config
    from app.main import create_app

    app = create_app(Config(data_dir=tmp_path / "d", achtergrond=True))
    transport = httpx.ASGITransport(app=nepbron)
    origineel = oph.haal_alle_op

    async def met_transport(db, kluis, transport_=None):
        return await origineel(db, kluis, transport)

    oph.haal_alle_op = met_transport
    try:
        with TestClient(app) as c:
            from .conftest import wizard

            wizard(c)
            voeg_bron_toe(c)  # zet nu_ophalen -> planner haalt direct op
            import time

            for _ in range(50):
                b = overzicht.bronnen(app.state.db)[0]
                if b["laatste_succes"]:
                    break
                time.sleep(0.05)
            assert b["bereikbaar"]
    finally:
        oph.haal_alle_op = origineel
