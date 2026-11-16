import json

from app import overzicht
from app.tijd import iso

from .conftest import voeg_bron_toe


def _b(i, naam, meldingen, verouderd=False):
    return {"id": i, "naam": naam, "app_slug": naam.lower(), "verouderd": verouderd,
            "samenvatting": {"kpis": [], "meldingen": meldingen}}


def test_meldingen_gebundeld_en_gesorteerd():
    lijst = [
        _b(1, "Scouten", [{"niveau": "info", "tekst": "i", "sinds": None},
                          {"niveau": "kritiek", "tekst": "oud kritiek", "sinds": "2026-11-01T00:00:00Z"}]),
        _b(2, "TuinWerk", [{"niveau": "waarschuwing", "tekst": "w", "sinds": "2026-11-10T00:00:00Z"},
                           {"niveau": "kritiek", "tekst": "nieuw kritiek", "sinds": "2026-11-15T00:00:00Z"}],
           verouderd=True),
        {"id": 3, "naam": "Leeg", "app_slug": "x", "verouderd": False, "samenvatting": None},
    ]
    uit = overzicht.gebundelde_meldingen(lijst)
    assert [m["tekst"] for m in uit] == ["nieuw kritiek", "oud kritiek", "w", "i"]
    assert uit[0]["bron_naam"] == "TuinWerk" and uit[0]["verouderd"] is True


def test_overzicht_toont_kritiek_van_meerdere_apps(beheerder, app):
    voeg_bron_toe(beheerder)
    voeg_bron_toe(beheerder, url="http://tuinwerk.lan")
    with app.state.db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM bronnen ORDER BY id")]
        for i, tekst in zip(ids, ["Kas 3 te warm", "Pomp 2 defect"]):
            conn.execute(
                "INSERT INTO bron_status (bron_id, laatste_poging, laatste_succes, samenvatting_json) VALUES (?, ?, ?, ?)",
                (i, iso(), iso(), json.dumps({"kpis": [], "meldingen": [
                    {"niveau": "kritiek", "tekst": tekst, "sinds": None}]})))
    html = beheerder.get("/").text
    blok = html.split('id="kop-apps"')[0]
    assert "Kas 3 te warm" in blok and "Pomp 2 defect" in blok


def test_sparkline_onderbreekt_bij_ontbrekende_waarden():
    svg = overzicht.sparkline([("a", 1.0), ("b", 2.0), ("c", None), ("d", 3.0), ("e", 1.0)])
    assert svg.count("<polyline") == 2
    assert "Te weinig" in overzicht.sparkline([("a", 1.0)])


def test_getal_opmaak():
    assert overzicht.formatteer_getal(1234) == "1.234"
    assert overzicht.formatteer_getal(12.5) == "12,5"
    assert overzicht.formatteer_getal(3.0) == "3"
