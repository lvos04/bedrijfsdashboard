import json
from datetime import timedelta

from starlette.requests import Request

from app import audit, instellingen, onderhoud
from app.beveiliging import client_ip, oorsprong_ok
from app.tijd import iso, nu

from .conftest import WACHTWOORD, csrf, csrf_uit, voeg_bron_toe
from .nepbron import GELDIGE_SLEUTEL


def _request(peer: str, xff: str | None = None, **koppen) -> Request:
    headers = [(b"host", b"dash.lan")]
    if xff:
        headers.append((b"x-forwarded-for", xff.encode()))
    for k, v in koppen.items():
        headers.append((k.replace("_", "-").encode(), v.encode()))
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": headers, "client": (peer, 1234), "query_string": b""}
    )


def test_client_ip_vertrouwde_proxy():
    prive = instellingen.parse_proxies("privé")
    # Direct verbonden (geen proxy): X-Forwarded-For wordt genegeerd als de peer niet vertrouwd is
    assert client_ip(_request("203.0.113.5", "1.2.3.4"), prive) == "203.0.113.5"
    # Achter één reverse proxy op het LAN
    assert client_ip(_request("192.168.1.2", "203.0.113.9"), prive) == "203.0.113.9"
    # Vervalste eerste waarde wordt overgeslagen: rechts-naar-links het eerste niet-vertrouwde adres
    assert client_ip(_request("127.0.0.1", "6.6.6.6, 203.0.113.9, 10.0.0.3"), prive) == "203.0.113.9"
    # 'geen': nooit X-Forwarded-For gebruiken
    assert client_ip(_request("127.0.0.1", "203.0.113.9"), instellingen.parse_proxies("geen")) == "127.0.0.1"
    # Expliciete lijst
    eigen = instellingen.parse_proxies("10.1.0.0/16, 172.20.0.5")
    assert client_ip(_request("172.20.0.5", "192.168.5.5"), eigen) == "192.168.5.5"
    assert client_ip(_request("192.168.1.2", "8.8.8.8"), eigen) == "192.168.1.2"
    assert client_ip(_request("testclient"), prive) == "testclient"


def test_vertrouwde_proxy_instelling_validatie(beheerder, app):
    r = beheerder.post("/beheer/beveiliging", data={"csrf_token": csrf(beheerder), "vertrouwde_proxies": "onzin!"})
    assert r.status_code == 422
    r = beheerder.post(
        "/beheer/beveiliging",
        data={"csrf_token": csrf(beheerder), "vertrouwde_proxies": "10.0.0.1", "audit_bewaardagen": "30"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert app.state.db.instelling("vertrouwde_proxies") == "10.0.0.1"
    assert app.state.db.instelling("audit_bewaardagen") == "30"


def test_oorsprong_strikt():
    assert oorsprong_ok(_request("1.1.1.1"))  # standaard: zonder Origin/Referer toegestaan (CSRF-token beslist)
    assert not oorsprong_ok(_request("1.1.1.1"), strikt=True)
    assert oorsprong_ok(_request("1.1.1.1", origin="http://dash.lan"), strikt=True)
    assert oorsprong_ok(_request("1.1.1.1", referer="http://dash.lan/beheer"), strikt=True)
    assert not oorsprong_ok(_request("1.1.1.1", origin="http://kwaad.example"), strikt=True)


def test_oorsprong_strikt_via_instelling(beheerder, app):
    data = {"csrf_token": csrf(beheerder), "interval_minuten": "5", "historie_dagen": "90"}
    assert beheerder.post("/beheer/instellingen", data=data, follow_redirects=False).status_code == 303
    app.state.db.zet_instelling("oorsprong_strikt", "1")
    assert beheerder.post("/beheer/instellingen", data=data).status_code == 403
    r = beheerder.post(
        "/beheer/instellingen", data=data, headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_auditlog_registreert_en_logt_geen_geheimen(beheerder, app):
    voeg_bron_toe(beheerder)
    with app.state.db() as conn:
        bron_id = conn.execute("SELECT id FROM bronnen").fetchone()[0]
    r = beheerder.post(
        "/api/beheer/koppelsleutels", json={"naam": "TuinWerk"}, headers={"X-CSRF-Token": csrf(beheerder)}
    )
    eigen_sleutel = r.json()["sleutel"]
    beheerder.post(
        "/beheer/gebruikers",
        data={"csrf_token": csrf(beheerder), "gebruikersnaam": "lezer", "wachtwoord": WACHTWOORD, "rol": "lezer"},
    )
    beheerder.post(
        "/beheer/email",
        data={
            "csrf_token": csrf(beheerder),
            "smtp_host": "smtp.lan",
            "smtp_wachtwoord": "smtp-geheim-123",
            "email_afzender": "a@b.nl",
            "email_ontvangers": "c@d.nl",
        },
    )
    beheerder.post(f"/beheer/bronnen/{bron_id}/verwijderen", data={"csrf_token": csrf(beheerder)})
    beheerder.post("/afmelden", data={"csrf_token": csrf(beheerder)})
    beheerder.cookies.clear()
    t = csrf_uit(beheerder.get("/aanmelden").text)
    beheerder.post(
        "/aanmelden", data={"csrf_token": t, "gebruikersnaam": "beheer", "wachtwoord": "verkeerd-wachtwoord"}
    )
    beheerder.post("/aanmelden", data={"csrf_token": t, "gebruikersnaam": "beheer", "wachtwoord": WACHTWOORD})
    with app.state.db() as conn:
        rijen = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]
    acties = [r["actie"] for r in rijen]
    for verwacht in (
        "welkom.beheerder_aangemaakt",
        "bron.toegevoegd",
        "koppelsleutel.aangemaakt",
        "gebruiker.toegevoegd",
        "instellingen.gewijzigd",
        "bron.verwijderd",
        "afmelden",
        "aanmelden.mislukt",
        "aanmelden.gelukt",
    ):
        assert verwacht in acties, verwacht
    alles = json.dumps(rijen)
    for geheim in (GELDIGE_SLEUTEL, eigen_sleutel, WACHTWOORD, "verkeerd-wachtwoord", "smtp-geheim-123"):
        assert geheim not in alles
    assert "SMTP-wachtwoord" in alles  # wel dát het gewijzigd is
    html = beheerder.get("/beheer/audit").text
    assert "<td>Bron toegevoegd</td>" in html and "<td>Aanmelden mislukt</td>" in html
    assert "Bron toegevoegd" in beheerder.get("/beheer/audit?actie=bron").text
    assert "<td>Aanmelden mislukt</td>" not in beheerder.get("/beheer/audit?actie=bron").text


def test_audit_paginering_en_lezer_geen_toegang(beheerder, app):
    for i in range(120):
        audit.schrijf(app.state.db, "test.actie", gebruiker="x", details={"i": i})
    r = beheerder.get("/api/beheer/audit?pagina=1").json()
    assert r["per_pagina"] == 50 and r["totaal"] >= 120 and r["paginas"] >= 3 and len(r["items"]) == 50
    assert r["items"][0]["id"] > r["items"][-1]["id"]  # nieuwste eerst
    r3 = beheerder.get(f"/api/beheer/audit?pagina={r['paginas']}").json()
    assert 0 < len(r3["items"]) <= 50
    assert "Pagina 2 van" in beheerder.get("/beheer/audit?pagina=2").text
    assert beheerder.get("/api/beheer/audit?per_pagina=x").status_code == 422
    beheerder.post(
        "/beheer/gebruikers",
        data={"csrf_token": csrf(beheerder), "gebruikersnaam": "lezer", "wachtwoord": WACHTWOORD, "rol": "lezer"},
    )
    beheerder.post("/afmelden", data={"csrf_token": csrf(beheerder)})
    beheerder.cookies.clear()
    t = csrf_uit(beheerder.get("/aanmelden").text)
    beheerder.post("/aanmelden", data={"csrf_token": t, "gebruikersnaam": "lezer", "wachtwoord": WACHTWOORD})
    assert beheerder.get("/beheer/audit").status_code == 403
    assert beheerder.get("/api/beheer/audit").status_code == 403


def test_opschonen_sessies_en_audit_bewaartermijn(beheerder, app):
    db = app.state.db
    with db() as conn:
        gid = conn.execute("SELECT id FROM gebruikers").fetchone()[0]
        conn.execute(
            "INSERT INTO sessies VALUES ('oud', ?, 'x', ?, ?)",
            (gid, iso(nu() - timedelta(days=30)), iso(nu() - timedelta(days=1))),
        )
        conn.execute("INSERT INTO audit_log (tijd, actie) VALUES (?, 'oud.actie')", (iso(nu() - timedelta(days=400)),))
    # Standaard: auditlog onbeperkt bewaren, verlopen sessies wel weg
    assert onderhoud.opschonen(db) == {"sessies": 1, "audit": 0}
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actie = 'oud.actie'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessies").fetchone()[0] == 1  # de actieve sessie blijft
    assert beheerder.get("/beheer").status_code == 200
    # Maximaal één keer per dag
    assert onderhoud.misschien_opschonen(db) is None
    db.zet_instelling("audit_bewaardagen", "365")
    db.zet_instelling("laatste_opschoning", iso(nu() - timedelta(days=2)))
    assert onderhoud.misschien_opschonen(db)["audit"] == 1
