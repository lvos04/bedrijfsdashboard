import json

import pytest

from app.tijd import iso

from .conftest import WACHTWOORD, csrf, csrf_uit, voeg_bron_toe


@pytest.fixture(autouse=True)
def koppeling_aan(app):
    """De eigen koppel-API staat standaard uit; deze tests gaan over het gedrag als hij aan staat."""
    app.state.db.zet_instelling("koppeling_ingeschakeld", "1")


def _maak_sleutel(client, naam="TuinWerk OS"):
    r = client.post("/api/beheer/koppelsleutels", json={"naam": naam}, headers={"X-CSRF-Token": csrf(client)})
    assert r.status_code == 201
    return r.json()


def test_401_zonder_of_met_ongeldige_sleutel(client):
    for kop in (
        {},
        {"Authorization": "Bearer bk_bestaatniet"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
    ):
        for pad in ("/info", "/samenvatting", "/datasets/bronnen"):
            r = client.get("/api/koppeling/v1" + pad, headers=kop)
            assert r.status_code == 401
            assert r.json() == {"fout": "ongeldige_koppelsleutel"}


def test_sleutel_eenmalig_getoond_en_gehasht(beheerder, app):
    s = _maak_sleutel(beheerder)
    assert s["sleutel"].startswith("bk_") and len(s["sleutel"]) >= 3 + 43
    lijst = beheerder.get("/api/beheer/koppelsleutels").json()
    assert lijst[0]["voorvoegsel"] == s["sleutel"][:8]
    assert "sleutel" not in lijst[0] and "hash" not in lijst[0]
    with app.state.db() as conn:
        rij = conn.execute("SELECT hash FROM koppelsleutels").fetchone()
    import hashlib

    assert rij["hash"] == hashlib.sha256(s["sleutel"].encode()).hexdigest()
    assert s["sleutel"] not in beheerder.get("/beheer").text


def test_sleutel_via_ui(beheerder):
    r = beheerder.post("/beheer/koppelsleutels", data={"csrf_token": csrf(beheerder), "naam": "Scouten"})
    assert r.status_code == 200 and "bk_" in r.text and "maar één keer" in r.text


def test_info_en_samenvatting(beheerder, app):
    s = _maak_sleutel(beheerder)
    kop = {"Authorization": f"Bearer {s['sleutel']}"}
    info = beheerder.get("/api/koppeling/v1/info", headers=kop).json()
    assert info["app"] == "bedrijfsdashboard" and info["api_versie"] == "1"
    assert info["bedrijf"] == "Testkwekerij" and "bronnen" in info["datasets"]
    sam = beheerder.get("/api/koppeling/v1/samenvatting", headers=kop).json()
    # Zonder bronnen: geen verzonnen nullen
    kpis = {k["sleutel"]: k for k in sam["kpis"]}
    assert kpis["bronnen_bereikbaar"]["waarde"] is None and kpis["bronnen_bereikbaar"]["status"] == "onbekend"
    assert kpis["kritieke_meldingen"]["waarde"] is None

    voeg_bron_toe(beheerder)
    with app.state.db() as conn:
        bron_id = conn.execute("SELECT id FROM bronnen").fetchone()[0]
        conn.execute(
            "INSERT INTO bron_status (bron_id, laatste_poging, laatste_succes, samenvatting_json) VALUES (?, ?, ?, ?)",
            (
                bron_id,
                iso(),
                iso(),
                json.dumps(
                    {
                        "kpis": [{"sleutel": "a", "label": "A", "waarde": 1, "status": "actie"}],
                        "meldingen": [{"niveau": "kritiek", "tekst": "Kas te warm", "sinds": None}],
                    }
                ),
            ),
        )
    sam = beheerder.get("/api/koppeling/v1/samenvatting", headers=kop).json()
    kpis = {k["sleutel"]: k for k in sam["kpis"]}
    assert kpis["bronnen_bereikbaar"]["waarde"] == 1 and kpis["bronnen_bereikbaar"]["status"] == "ok"
    assert kpis["kritieke_meldingen"]["waarde"] == 1 and kpis["kritieke_meldingen"]["status"] == "actie"
    assert kpis["kpis_actie"]["waarde"] == 1
    assert sam["meldingen"][0] == {"niveau": "kritiek", "tekst": "Scouten: Kas te warm", "sinds": None}
    with app.state.db() as conn:
        assert conn.execute("SELECT laatst_gebruikt_op FROM koppelsleutels").fetchone()[0] is not None


def test_datasets_limiet_cursor_sinds_404_422(beheerder):
    s = _maak_sleutel(beheerder)
    kop = {"Authorization": f"Bearer {s['sleutel']}"}
    voeg_bron_toe(beheerder)
    voeg_bron_toe(beheerder, url="http://b.lan")
    voeg_bron_toe(beheerder, url="http://c.lan")
    r = beheerder.get("/api/koppeling/v1/datasets/bronnen?limiet=2", headers=kop).json()
    assert r["dataset"] == "bronnen" and len(r["items"]) == 2 and r["volgende_cursor"]
    r2 = beheerder.get(f"/api/koppeling/v1/datasets/bronnen?limiet=2&cursor={r['volgende_cursor']}", headers=kop).json()
    assert len(r2["items"]) == 1 and r2["volgende_cursor"] is None
    # Nog nooit opgehaald -> geen bijgewerkt_op -> valt buiten 'sinds'
    r3 = beheerder.get("/api/koppeling/v1/datasets/bronnen?sinds=2020-01-01T00:00:00Z", headers=kop).json()
    assert r3["items"] == []
    assert beheerder.get("/api/koppeling/v1/datasets/geheim", headers=kop).status_code == 404
    assert beheerder.get("/api/koppeling/v1/datasets/geheim", headers=kop).json() == {"fout": "onbekende_dataset"}
    assert beheerder.get("/api/koppeling/v1/datasets/bronnen?limiet=0", headers=kop).status_code == 422
    assert beheerder.get("/api/koppeling/v1/datasets/bronnen?limiet=501", headers=kop).status_code == 422
    assert beheerder.get("/api/koppeling/v1/datasets/bronnen?sinds=gisteren", headers=kop).status_code == 422
    assert beheerder.get("/api/koppeling/v1/datasets/bronnen?cursor=!!", headers=kop).status_code == 422


def test_ingetrokken_sleutel_401(beheerder):
    s = _maak_sleutel(beheerder)
    kop = {"Authorization": f"Bearer {s['sleutel']}"}
    assert beheerder.get("/api/koppeling/v1/info", headers=kop).status_code == 200
    r = beheerder.delete(f"/api/beheer/koppelsleutels/{s['id']}", headers={"X-CSRF-Token": csrf(beheerder)})
    assert r.status_code == 204
    assert beheerder.get("/api/koppeling/v1/info", headers=kop).status_code == 401


def test_sessie_geeft_geen_toegang_tot_koppel_api(beheerder):
    assert beheerder.get("/api/koppeling/v1/info").status_code == 401


def test_lezer_kan_geen_sleutels_beheren(beheerder):
    beheerder.post(
        "/beheer/gebruikers",
        data={"csrf_token": csrf(beheerder), "gebruikersnaam": "lezer", "wachtwoord": WACHTWOORD, "rol": "lezer"},
    )
    beheerder.post("/afmelden", data={"csrf_token": csrf(beheerder)})
    beheerder.cookies.clear()
    token = csrf_uit(beheerder.get("/aanmelden").text)
    beheerder.post("/aanmelden", data={"csrf_token": token, "gebruikersnaam": "lezer", "wachtwoord": WACHTWOORD})
    token = csrf_uit(beheerder.get("/").text)
    assert beheerder.get("/api/beheer/koppelsleutels").status_code == 403
    assert (
        beheerder.post("/api/beheer/koppelsleutels", json={"naam": "x"}, headers={"X-CSRF-Token": token}).status_code
        == 403
    )
    assert beheerder.post("/beheer/koppelsleutels", data={"csrf_token": token, "naam": "x"}).status_code == 403
    assert beheerder.delete("/api/beheer/koppelsleutels/1", headers={"X-CSRF-Token": token}).status_code == 403


def test_niet_aangemeld_beheer_api_401(client):
    assert client.get("/api/beheer/koppelsleutels").status_code == 401


def test_koppeling_standaard_uit_geeft_403_ook_met_geldige_sleutel(beheerder, app):
    app.state.db.zet_instelling("koppeling_ingeschakeld", "0")
    s = _maak_sleutel(beheerder)  # sleutelbeheer blijft werken
    kop = {"Authorization": f"Bearer {s['sleutel']}"}
    for pad in ("/info", "/samenvatting", "/datasets/bronnen", "/datasets/bronnen?limiet=0", "/datasets/onbekend"):
        for headers in (kop, {}):
            r = beheerder.get("/api/koppeling/v1" + pad, headers=headers)
            assert r.status_code == 403, pad
            assert r.json() == {"fout": "koppeling_uitgeschakeld"}
    # De schakelaar staat zichtbaar boven het sleutelbeheer
    html = beheerder.get("/beheer").text
    assert "Bedrijfskoppeling ingeschakeld" in html and "Sleutel aanmaken" in html
    # Aanzetten via Beheer
    r = beheerder.post(
        "/beheer/koppeling", data={"csrf_token": csrf(beheerder), "koppeling_ingeschakeld": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert beheerder.get("/api/koppeling/v1/info", headers=kop).status_code == 200
    beheerder.post("/beheer/koppeling", data={"csrf_token": csrf(beheerder)})
    assert beheerder.get("/api/koppeling/v1/info", headers=kop).status_code == 403


def test_nieuwe_installatie_koppeling_uit(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.main import create_app

    nieuw = create_app(Config(data_dir=tmp_path / "nieuw", achtergrond=False))
    with TestClient(nieuw) as c:
        r = c.get("/api/koppeling/v1/info", headers={"Authorization": "Bearer bk_x"})
        assert r.status_code == 403 and r.json() == {"fout": "koppeling_uitgeschakeld"}
