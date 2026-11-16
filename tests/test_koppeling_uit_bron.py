"""De bron-apps kunnen hun koppeling uitzetten (403 koppeling_uitgeschakeld): toon een duidelijke uitleg."""

import asyncio

from app import overzicht
from app.ophalen import haal_alle_op

from .conftest import csrf, voeg_bron_toe


def _zet_uit(nepbron):
    nepbron.state.status = 403
    nepbron.state.fout_body = {"fout": "koppeling_uitgeschakeld"}


def _haal(app):
    return asyncio.run(haal_alle_op(app.state.db, app.state.kluis, app.state.http_transport))


def _bron_id(app):
    with app.state.db() as conn:
        return conn.execute("SELECT id FROM bronnen").fetchone()[0]


def test_uitleg_in_overzicht_en_detail(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    _zet_uit(met_nepbron)
    fout = list(_haal(app).values())[0]
    assert fout.startswith("Koppeling staat uit in Scouten: zet hem aan onder Beheer & instellingen → Koppelingen")
    b = overzicht.bron(app.state.db, _bron_id(app))
    assert b["koppeling_uit"] and b["fout_code"] == "koppeling_uitgeschakeld"
    html = beheerder.get("/").text
    assert "Koppeling uit" in html and "Koppeling staat uit in Scouten" in html
    assert "Bedrijfskoppeling ingeschakeld" in html
    assert "is niet bereikbaar sinds" not in html  # geen generieke fout
    assert "Koppeling staat uit" in beheerder.get(f"/bronnen/{b['id']}").text
    # Na aanzetten in de bron-app verdwijnt de uitleg
    met_nepbron.state.status = 200
    _haal(app)
    b = overzicht.bron(app.state.db, b["id"])
    assert not b["koppeling_uit"] and b["fout_code"] is None and b["bereikbaar"]


def test_uitleg_per_app(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder, naam="Milieu", app_slug="milieu-registratie")
    _zet_uit(met_nepbron)
    assert "Koppeling staat uit in Milieu: zet hem aan onder Instellingen → Koppelingen" in list(_haal(app).values())[0]
    with app.state.db() as conn:
        conn.execute("UPDATE bronnen SET app_slug = 'eigen-app', naam = 'Eigen'")
    assert "zet hem aan onder het beheer van koppelsleutels" in list(_haal(app).values())[0]


def test_verbinding_testen_en_dataset(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    _haal(app)
    bron_id = _bron_id(app)
    _zet_uit(met_nepbron)
    r = beheerder.post(f"/beheer/bronnen/{bron_id}/testen", data={"csrf_token": csrf(beheerder)})
    assert "Koppeling staat uit." in r.text and "Koppeling staat uit in Scouten" in r.text
    r = beheerder.get(f"/api/bronnen/{bron_id}/datasets/waarnemingen")
    assert r.status_code == 502 and r.json()["code"] == "koppeling_uitgeschakeld"
    assert "Koppeling staat uit in Scouten" in r.json()["fout"]


def test_andere_403_blijft_generiek(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    met_nepbron.state.status = 403
    assert list(_haal(app).values())[0] == "Bron gaf HTTP 403"
