import asyncio

from app.ophalen import haal_alle_op

from .conftest import voeg_bron_toe
from .nepbron import GELDIGE_SLEUTEL


def _klaar(beheerder, app):
    voeg_bron_toe(beheerder)
    asyncio.run(haal_alle_op(app.state.db, app.state.kluis, app.state.http_transport))
    with app.state.db() as conn:
        return conn.execute("SELECT id FROM bronnen").fetchone()[0]


def test_dataset_json_proxy(beheerder, app, met_nepbron):
    bron_id = _klaar(beheerder, app)
    r = beheerder.get(f"/api/bronnen/{bron_id}/datasets/waarnemingen?limiet=3")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 3 and data["items"][0]["plaag"] == "trips"
    assert GELDIGE_SLEUTEL not in r.text
    # De sleutel ging wel server-naar-server mee
    assert met_nepbron.state.aanroepen[-1] == ("/api/koppeling/v1/datasets/waarnemingen", f"Bearer {GELDIGE_SLEUTEL}")


def test_dataset_html_tabel(beheerder, app, met_nepbron):
    bron_id = _klaar(beheerder, app)
    html = beheerder.get(f"/bronnen/{bron_id}/datasets/waarnemingen").text
    assert "<table" in html and "plaag" in html and "trips" in html
    assert GELDIGE_SLEUTEL not in html
    assert f"/bronnen/{bron_id}/datasets/waarnemingen" in beheerder.get(f"/bronnen/{bron_id}").text


def test_dataset_niet_aangeboden_en_ongeldige_naam(beheerder, app, met_nepbron):
    bron_id = _klaar(beheerder, app)
    r = beheerder.get(f"/api/bronnen/{bron_id}/datasets/geheim")
    assert r.status_code == 404
    assert beheerder.get(f"/api/bronnen/{bron_id}/datasets/..%2Finfo").status_code in (404, 422)
    assert beheerder.get("/api/bronnen/999/datasets/waarnemingen").status_code == 404


def test_dataset_bron_fout_502(beheerder, app, met_nepbron):
    bron_id = _klaar(beheerder, app)
    met_nepbron.state.status = 401
    r = beheerder.get(f"/api/bronnen/{bron_id}/datasets/waarnemingen")
    assert r.status_code == 502 and "401" in r.json()["fout"]
    html = beheerder.get(f"/bronnen/{bron_id}/datasets/waarnemingen").text
    assert "Ophalen mislukt" in html


def test_dataset_vereist_aanmelding(client):
    assert client.get("/api/bronnen/1/datasets/x").status_code == 401
