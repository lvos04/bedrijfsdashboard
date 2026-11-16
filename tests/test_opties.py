"""Optionele functies: interval per bron, e-mailmeldingen, tv-weergave en configuratie-back-up."""

import asyncio
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import mail, overzicht
from app.config import Config
from app.main import create_app
from app.ophalen import haal_alle_op, wachttijd_s
from app.tijd import iso, nu

from .conftest import WACHTWOORD, csrf, voeg_bron_toe, wizard
from .nepbron import GELDIGE_SLEUTEL


def _haal(app, **kw):
    return asyncio.run(haal_alle_op(app.state.db, app.state.kluis, app.state.http_transport, **kw))


def _bron_id(app):
    with app.state.db() as conn:
        return conn.execute("SELECT id FROM bronnen ORDER BY id").fetchone()[0]


# ---------------------------------------------------------------- interval en verouderd per bron


def test_interval_en_verouderd_per_bron(beheerder, app, met_nepbron):
    assert voeg_bron_toe(beheerder, interval_minuten="0").status_code == 422
    assert voeg_bron_toe(beheerder, interval_minuten="60", verouderd_na_minuten="10").status_code == 303
    bron_id = _bron_id(app)
    b = overzicht.bron(app.state.db, bron_id)
    assert b["interval_minuten"] == 60 and b["effectief_interval"] == 60 and b["effectief_verouderd_na"] == 10
    assert _haal(app) == {bron_id: None}
    # Net opgehaald: met alleen_verlopen wordt deze bron overgeslagen
    assert _haal(app, alleen_verlopen=True) == {}
    assert wachttijd_s(app.state.db) <= 5 * 60  # niet langer dan het globale interval
    # 11 minuten geleden geslaagd -> verouderd volgens de eigen drempel (10), niet volgens 3 × 60
    with app.state.db() as conn:
        oud = iso(nu() - timedelta(minutes=11))
        conn.execute("UPDATE bron_status SET laatste_succes = ?, laatste_poging = ?", (oud, oud))
    assert overzicht.bron(app.state.db, bron_id)["verouderd"]
    with app.state.db() as conn:
        conn.execute("UPDATE bron_status SET laatste_poging = ?", (iso(nu() - timedelta(minutes=61)),))
    assert _haal(app, alleen_verlopen=True) == {bron_id: None}
    # Leeg maken = weer globaal
    r = beheerder.post(
        f"/beheer/bronnen/{bron_id}",
        data={
            "csrf_token": csrf(beheerder),
            "naam": "Scouten",
            "app_slug": "scouten",
            "basis_url": "http://scouten.lan:30092",
            "actief": "1",
            "interval_minuten": "",
            "verouderd_na_minuten": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    b = overzicht.bron(app.state.db, bron_id)
    assert b["interval_minuten"] is None and b["effectief_interval"] == 5 and b["effectief_verouderd_na"] == 15


def test_globaal_verouderd_na(beheerder, app):
    r = beheerder.post(
        "/beheer/instellingen",
        data={
            "csrf_token": csrf(beheerder),
            "interval_minuten": "5",
            "historie_dagen": "90",
            "verouderd_na_minuten": "60",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    voeg_bron_toe(beheerder)
    assert overzicht.bron(app.state.db, _bron_id(app))["effectief_verouderd_na"] == 60
    assert (
        beheerder.post(
            "/beheer/instellingen",
            data={
                "csrf_token": csrf(beheerder),
                "interval_minuten": "5",
                "historie_dagen": "90",
                "verouderd_na_minuten": "x",
            },
        ).status_code
        == 422
    )


# ---------------------------------------------------------------- e-mailmeldingen


class NepSMTP:
    verzonden: list = []

    def __init__(self, host, poort, timeout=None, **kw):
        self.host, self.poort = host, poort

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, gebruiker, wachtwoord):
        self.login_met = (gebruiker, wachtwoord)

    def send_message(self, bericht):
        NepSMTP.verzonden.append(bericht)


@pytest.fixture
def nep_smtp(monkeypatch):
    NepSMTP.verzonden = []
    monkeypatch.setattr(mail.smtplib, "SMTP", NepSMTP)
    return NepSMTP


def _email_aan(beheerder, aan="1"):
    data = {
        "csrf_token": csrf(beheerder),
        "smtp_host": "smtp.lan",
        "smtp_poort": "587",
        "smtp_beveiliging": "starttls",
        "smtp_gebruiker": "dash",
        "smtp_wachtwoord": "smtp-geheim-123",
        "email_afzender": "dashboard@kwekerij.nl",
        "email_ontvangers": "chef@kwekerij.nl, teler@kwekerij.nl",
    }
    if aan:
        data["email_ingeschakeld"] = aan
    return beheerder.post("/beheer/email", data=data, follow_redirects=False)


def test_email_standaard_uit_en_geen_stortvloed(beheerder, app, met_nepbron, nep_smtp):
    voeg_bron_toe(beheerder)
    _haal(app)
    assert nep_smtp.verzonden == []  # standaard uit
    assert _email_aan(beheerder).status_code == 303
    _haal(app)
    assert nep_smtp.verzonden == []  # bestaande melding was al gezien: geen mail na inschakelen
    met_nepbron.state.meldingen = [
        *met_nepbron.state.meldingen,
        {"niveau": "kritiek", "tekst": "Ketel 2 storing", "sinds": "2026-11-16T10:00:00Z"},
        {"niveau": "waarschuwing", "tekst": "Geen kritiek", "sinds": None},
    ]
    _haal(app)
    assert len(nep_smtp.verzonden) == 1
    bericht = nep_smtp.verzonden[0]
    assert "1 nieuwe kritieke melding" in bericht["Subject"]
    assert "Scouten: Ketel 2 storing" in bericht.get_content() and "Geen kritiek" not in bericht.get_content()
    assert bericht["To"] == "chef@kwekerij.nl, teler@kwekerij.nl"
    _haal(app)
    _haal(app)
    assert len(nep_smtp.verzonden) == 1  # ontdubbeld


def test_email_validatie_test_en_wachtwoord_niet_getoond(beheerder, app, nep_smtp):
    assert (
        beheerder.post("/beheer/email", data={"csrf_token": csrf(beheerder), "email_ingeschakeld": "1"}).status_code
        == 422
    )
    assert (
        beheerder.post(
            "/beheer/email", data={"csrf_token": csrf(beheerder), "smtp_host": "x", "email_ontvangers": "geen-adres"}
        ).status_code
        == 422
    )
    _email_aan(beheerder)
    assert "smtp-geheim-123" not in beheerder.get("/beheer").text
    assert app.state.db.instelling("smtp_wachtwoord_versleuteld") and "smtp-geheim-123" not in app.state.db.instelling(
        "smtp_wachtwoord_versleuteld"
    )
    r = beheerder.post("/beheer/email/test", data={"csrf_token": csrf(beheerder)}, follow_redirects=False)
    assert r.status_code == 303 and len(nep_smtp.verzonden) == 1
    # Leeg wachtwoordveld houdt het opgeslagen wachtwoord
    beheerder.post(
        "/beheer/email",
        data={
            "csrf_token": csrf(beheerder),
            "smtp_host": "smtp.lan",
            "email_afzender": "a@b.nl",
            "email_ontvangers": "c@d.nl",
        },
    )
    assert mail.smtp_config(app.state.db, app.state.kluis)["wachtwoord"] == "smtp-geheim-123"


def test_email_fout_wordt_getoond_en_opnieuw_geprobeerd(beheerder, app, met_nepbron, monkeypatch):
    class Kapot(NepSMTP):
        def __init__(self, *a, **kw):
            raise OSError("verbinding geweigerd")

    monkeypatch.setattr(mail.smtplib, "SMTP", Kapot)
    _email_aan(beheerder)
    voeg_bron_toe(beheerder)
    _haal(app)
    assert "Versturen mislukt" in beheerder.get("/beheer").text
    monkeypatch.setattr(mail.smtplib, "SMTP", NepSMTP)
    NepSMTP.verzonden = []
    _haal(app)
    assert len(NepSMTP.verzonden) == 1


# ---------------------------------------------------------------- tv-weergave


def test_tv_weergave(beheerder, app, met_nepbron):
    voeg_bron_toe(beheerder)
    _haal(app)
    r = beheerder.post("/beheer/tv/nieuwe-link", data={"csrf_token": csrf(beheerder)})
    link = re.search(r'<code id="tv-link">([^<]+)</code>', r.text).group(1)
    pad = link.replace("http://testserver", "")
    anoniem = TestClient(app)
    assert anoniem.get(pad).status_code == 404  # standaard uit
    beheerder.post("/beheer/tv", data={"csrf_token": csrf(beheerder), "tv_ingeschakeld": "1"})
    r = anoniem.get(pad)
    assert r.status_code == 200
    assert "Open waarnemingen" in r.text and "Trips boven drempel" in r.text
    assert 'http-equiv="refresh"' in r.text and "Afmelden" not in r.text
    assert "scouten.lan" not in r.text  # geen interne adressen
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert anoniem.get("/tv/fout-token").status_code == 404
    assert anoniem.get("/beheer", follow_redirects=False).status_code == 303  # verder geen toegang
    # Nieuwe link maakt de oude ongeldig
    beheerder.post("/beheer/tv/nieuwe-link", data={"csrf_token": csrf(beheerder)})
    assert anoniem.get(pad).status_code == 404
    with app.state.db() as conn:
        assert pad.split("/")[-1] not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM instellingen")])


# ---------------------------------------------------------------- back-up van de configuratie


def _exporteer(beheerder, herstel="herstel-wachtwoord-1", eigen=WACHTWOORD):
    return beheerder.post(
        "/beheer/back-up/exporteren",
        data={
            "csrf_token": csrf(beheerder),
            "herstelwachtwoord": herstel,
            "herstelwachtwoord2": herstel,
            "eigen_wachtwoord": eigen,
        },
    )


def test_back_up_exporteren_en_terugzetten(beheerder, app, tmp_path, nep_smtp):
    voeg_bron_toe(beheerder, interval_minuten="15")
    _email_aan(beheerder)
    r = beheerder.post(
        "/api/beheer/koppelsleutels", json={"naam": "TuinWerk"}, headers={"X-CSRF-Token": csrf(beheerder)}
    )
    eigen_sleutel = r.json()["sleutel"]
    assert _exporteer(beheerder, eigen="fout").status_code == 422
    assert _exporteer(beheerder, herstel="kort").status_code == 422
    r = _exporteer(beheerder)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.content
    for geheim in (GELDIGE_SLEUTEL.encode(), b"smtp-geheim-123", b"scouten.lan"):
        assert geheim not in data  # alles versleuteld
    assert json.loads(data)["formaat"] == "bedrijfsdashboard-configuratie"

    nieuw = create_app(Config(data_dir=tmp_path / "nieuw", achtergrond=False))
    with TestClient(nieuw) as c:
        wizard(c)

        def zet_terug(herstel, eigen=WACHTWOORD):
            return c.post(
                "/beheer/back-up/terugzetten",
                data={"csrf_token": csrf(c), "herstelwachtwoord": herstel, "eigen_wachtwoord": eigen},
                files={"bestand": ("export.json", data, "application/json")},
            )

        assert "Onjuist herstelwachtwoord" in zet_terug("verkeerd-wachtwoord").text
        assert zet_terug("herstel-wachtwoord-1", eigen="fout").status_code == 422
        r = zet_terug("herstel-wachtwoord-1")
        assert r.status_code == 200 and "Configuratie teruggezet" in r.text
        with nieuw.state.db() as conn:
            bron = conn.execute("SELECT * FROM bronnen").fetchone()
        assert bron["interval_minuten"] == 15
        assert nieuw.state.kluis.ontsleutel(bron["sleutel_versleuteld"]) == GELDIGE_SLEUTEL  # nieuwe geheim.key
        assert mail.smtp_config(nieuw.state.db, nieuw.state.kluis)["wachtwoord"] == "smtp-geheim-123"
        nieuw.state.db.zet_instelling("koppeling_ingeschakeld", "1")
        assert c.get("/api/koppeling/v1/info", headers={"Authorization": f"Bearer {eigen_sleutel}"}).status_code == 200
        # Nogmaals terugzetten: geen dubbele bron
        zet_terug("herstel-wachtwoord-1")
        with nieuw.state.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM bronnen").fetchone()[0] == 1
        r = c.post(
            "/beheer/back-up/terugzetten",
            data={"csrf_token": csrf(c), "herstelwachtwoord": "x", "eigen_wachtwoord": WACHTWOORD},
            files={"bestand": ("x.json", b"geen json", "application/json")},
        )
        assert "geen geldig exportbestand" in r.text
