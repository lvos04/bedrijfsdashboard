import base64

from app import totp
from app.beveiliging import AanmeldBeperking

from .conftest import WACHTWOORD, csrf, csrf_uit

NIEUW = "nieuw-sterk-wachtwoord-1"


def _afmelden(client):
    client.post("/afmelden", data={"csrf_token": csrf_uit(client.get("/account").text)})
    client.cookies.clear()


def _aanmelden(client, naam, ww):
    token = csrf_uit(client.get("/aanmelden").text)
    return client.post(
        "/aanmelden", data={"csrf_token": token, "gebruikersnaam": naam, "wachtwoord": ww}, follow_redirects=False
    )


def _lezer(beheerder, naam="lezer"):
    beheerder.post(
        "/beheer/gebruikers",
        data={"csrf_token": csrf(beheerder), "gebruikersnaam": naam, "wachtwoord": WACHTWOORD, "rol": "lezer"},
    )


def test_totp_rfc6238_testvectoren():
    geheim = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code(geheim, 59 // 30) == "287082"
    assert totp.code(geheim, 1111111109 // 30) == "081804"
    assert totp.controleer(geheim, "287082", t=59) == 1
    assert totp.controleer(geheim, "287082", laatste_stap=1, t=59) is None  # geen hergebruik
    assert totp.controleer(geheim, "12345", t=59) is None


def test_eigen_wachtwoord_wijzigen(beheerder, app):
    token = csrf(beheerder)
    r = beheerder.post(
        "/account/wachtwoord", data={"csrf_token": token, "huidig": "fout", "nieuw": NIEUW, "nieuw2": NIEUW}
    )
    assert r.status_code == 422 and "huidige wachtwoord klopt niet" in r.text
    r = beheerder.post(
        "/account/wachtwoord", data={"csrf_token": token, "huidig": WACHTWOORD, "nieuw": "kort", "nieuw2": "kort"}
    )
    assert r.status_code == 422
    # Een tweede sessie van dezelfde gebruiker
    from fastapi.testclient import TestClient

    with TestClient(app) as ander:
        assert _aanmelden(ander, "beheer", WACHTWOORD).status_code == 303
        assert ander.get("/beheer").status_code == 200
        r = beheerder.post(
            "/account/wachtwoord",
            data={"csrf_token": token, "huidig": WACHTWOORD, "nieuw": NIEUW, "nieuw2": NIEUW},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert beheerder.get("/beheer").status_code == 200  # huidige sessie blijft
        assert ander.get("/beheer", follow_redirects=False).status_code == 303  # andere sessie afgemeld
    _afmelden(beheerder)
    assert _aanmelden(beheerder, "beheer", WACHTWOORD).status_code == 401
    assert _aanmelden(beheerder, "beheer", NIEUW).status_code == 303


def test_beheerder_reset_wachtwoord_van_gebruiker(beheerder, app):
    _lezer(beheerder)
    with app.state.db() as conn:
        lezer_id = conn.execute("SELECT id FROM gebruikers WHERE gebruikersnaam = 'lezer'").fetchone()[0]
    from fastapi.testclient import TestClient

    with TestClient(app) as lezer:
        assert _aanmelden(lezer, "lezer", WACHTWOORD).status_code == 303
        # Lezer mag niet resetten
        t = csrf_uit(lezer.get("/").text)
        assert (
            lezer.post(
                f"/beheer/gebruikers/{lezer_id}/wachtwoord", data={"csrf_token": t, "wachtwoord": NIEUW}
            ).status_code
            == 403
        )
        assert (
            beheerder.post(
                f"/beheer/gebruikers/{lezer_id}/wachtwoord", data={"csrf_token": csrf(beheerder), "wachtwoord": "kort"}
            ).status_code
            == 422
        )
        r = beheerder.post(
            f"/beheer/gebruikers/{lezer_id}/wachtwoord",
            data={"csrf_token": csrf(beheerder), "wachtwoord": NIEUW},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert lezer.get("/", follow_redirects=False).status_code == 303  # overal afgemeld
        lezer.cookies.clear()
        assert _aanmelden(lezer, "lezer", NIEUW).status_code == 303


def _mfa_aan(client, app, naam="beheer") -> str:
    r = client.post("/account/mfa/start", data={"csrf_token": csrf_uit(client.get("/account").text)})
    assert r.status_code == 200 and "otpauth://totp/" in r.text
    with app.state.db() as conn:
        geheim = app.state.kluis.ontsleutel(
            conn.execute("SELECT totp_nieuw FROM gebruikers WHERE gebruikersnaam = ?", (naam,)).fetchone()[0]
        )
    token = csrf_uit(client.get("/account").text)
    assert client.post("/account/mfa/bevestigen", data={"csrf_token": token, "code": "000000"}).status_code in (
        422,
        303,
    )
    with app.state.db() as conn:
        if conn.execute("SELECT totp_geheim FROM gebruikers WHERE gebruikersnaam = ?", (naam,)).fetchone()[0]:
            return geheim  # (uiterst onwaarschijnlijk) 000000 was toevallig geldig
    r = client.post(
        "/account/mfa/bevestigen",
        data={"csrf_token": token, "code": totp.code(geheim, totp.huidige_stap())},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return geheim


def test_mfa_instellen_en_aanmelden(beheerder, app):
    geheim = _mfa_aan(beheerder, app)
    with app.state.db() as conn:
        rij = conn.execute("SELECT totp_geheim, totp_nieuw FROM gebruikers").fetchone()
    assert rij["totp_geheim"] is not None and rij["totp_nieuw"] is None
    assert geheim.encode() not in bytes(rij["totp_geheim"])  # versleuteld opgeslagen
    _afmelden(beheerder)
    r = _aanmelden(beheerder, "beheer", WACHTWOORD)
    assert r.status_code == 200 and "Verificatiecode" in r.text and "bd_sessie" not in r.headers.get("set-cookie", "")
    assert beheerder.get("/", follow_redirects=False).status_code == 303  # nog niet aangemeld
    token = csrf_uit(r.text)
    r = beheerder.post("/aanmelden/mfa", data={"csrf_token": token, "code": "999999"})
    assert r.status_code == 401
    # Codes van de vorige stap zijn al door het bevestigen 'gebruikt'; neem de volgende stap.
    stap = totp.huidige_stap() + 1
    r = beheerder.post(
        "/aanmelden/mfa", data={"csrf_token": token, "code": totp.code(geheim, stap)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert beheerder.get("/beheer").status_code == 200
    # Zelfde code nogmaals is hergebruik
    _afmelden(beheerder)
    r = _aanmelden(beheerder, "beheer", WACHTWOORD)
    r = beheerder.post("/aanmelden/mfa", data={"csrf_token": csrf_uit(r.text), "code": totp.code(geheim, stap)})
    assert r.status_code == 401


def test_mfa_zonder_uitdaging_geweigerd(beheerder):
    _afmelden(beheerder)
    token = csrf_uit(beheerder.get("/aanmelden").text)
    r = beheerder.post("/aanmelden/mfa", data={"csrf_token": token, "code": "123456"})
    assert r.status_code == 401 and "verlopen" in r.text


def test_mfa_verplicht_voor_beheerders(beheerder, app):
    token = csrf(beheerder)
    # Kan niet aangezet worden zolang de beheerder zelf geen MFA heeft (anders sluit hij zichzelf buiten)
    r = beheerder.post("/beheer/beveiliging", data={"csrf_token": token, "mfa_verplicht_beheerders": "1"})
    assert r.status_code == 422
    assert app.state.db.instelling("mfa_verplicht_beheerders") in (None, "0")
    # Tweede beheerder zonder MFA
    beheerder.post(
        "/beheer/gebruikers",
        data={"csrf_token": token, "gebruikersnaam": "beheer2", "wachtwoord": WACHTWOORD, "rol": "beheerder"},
    )
    app.state.db.zet_instelling("mfa_verplicht_beheerders", "1")
    from fastapi.testclient import TestClient

    with TestClient(app) as b2:
        assert _aanmelden(b2, "beheer2", WACHTWOORD).status_code == 303
        r = b2.get("/beheer", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/account?m=mfa_vereist"
        assert b2.get("/api/beheer/koppelsleutels").json() == {"fout": "mfa_vereist"}
        assert "MFA is verplicht voor beheerders" in b2.get("/").text
        _mfa_aan(b2, app, "beheer2")
        assert b2.get("/beheer").status_code == 200
        # Uitschakelen kan niet zolang het verplicht is
        r = b2.post("/account/mfa/uitschakelen", data={"csrf_token": csrf(b2), "wachtwoord": WACHTWOORD})
        assert r.status_code == 422
    # Standaard (uit) heeft een beheerder zonder MFA gewoon toegang
    app.state.db.zet_instelling("mfa_verplicht_beheerders", "0")
    assert beheerder.get("/beheer").status_code == 200


def test_mfa_uitschakelen_en_reset_door_beheerder(beheerder, app):
    _lezer(beheerder)
    from fastapi.testclient import TestClient

    with TestClient(app) as lezer:
        _aanmelden(lezer, "lezer", WACHTWOORD)
        _mfa_aan(lezer, app, "lezer")
        r = lezer.post(
            "/account/mfa/uitschakelen", data={"csrf_token": csrf_uit(lezer.get("/account").text), "wachtwoord": "fout"}
        )
        assert r.status_code == 422
    with app.state.db() as conn:
        lezer_id = conn.execute("SELECT id FROM gebruikers WHERE gebruikersnaam = 'lezer'").fetchone()[0]
    assert "Ook MFA uitschakelen" in beheerder.get("/beheer").text
    beheerder.post(
        f"/beheer/gebruikers/{lezer_id}/wachtwoord",
        data={"csrf_token": csrf(beheerder), "wachtwoord": NIEUW, "mfa_uitschakelen": "1"},
    )
    with app.state.db() as conn:
        assert conn.execute("SELECT totp_geheim FROM gebruikers WHERE id = ?", (lezer_id,)).fetchone()[0] is None


def test_aanmeldbeperking_per_account_en_ip():
    b = AanmeldBeperking(per_account_ip=5, per_account=20)
    for _ in range(5):
        b.fout("piet", "10.0.0.1")
    assert b.geblokkeerd("piet", "10.0.0.1")
    # Een andere gebruiker achter hetzelfde (proxy-)IP wordt niet geblokkeerd
    assert not b.geblokkeerd("jan", "10.0.0.1")
    # Dezelfde gebruiker vanaf een ander IP ook niet ...
    assert not b.geblokkeerd("PIET", "10.0.0.2")
    # ... tot het totaal per account (vangnet tegen IP-rouleren) bereikt is
    for i in range(15):
        b.fout("piet", f"10.0.1.{i}")
    assert b.geblokkeerd("piet", "10.9.9.9")
    b.reset("piet", "10.0.0.1")
    assert not b.geblokkeerd("piet", "10.9.9.9")
    b.opruimen()


def test_aanmeldbeperking_blokkeert_niet_iedereen(beheerder):
    _lezer(beheerder)
    _afmelden(beheerder)
    for _ in range(5):
        assert _aanmelden(beheerder, "beheer", "fout-fout-fout-fout").status_code == 401
    assert _aanmelden(beheerder, "beheer", WACHTWOORD).status_code == 429
    assert _aanmelden(beheerder, "lezer", WACHTWOORD).status_code == 303
