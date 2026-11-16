from .conftest import WACHTWOORD, csrf, csrf_uit, wizard


def test_zonder_gebruikers_naar_welkom(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welkom"
    assert client.get("/aanmelden", follow_redirects=False).headers["location"] == "/welkom"


def test_wizard_wachtwoord_te_kort(client):
    r = wizard(client, ww="kort")
    assert r.status_code == 422
    assert "minimaal 12 tekens" in r.text


def test_wizard_maakt_beheerder_en_sessie(client, app):
    r = wizard(client)
    assert r.status_code == 303 and r.headers["location"] == "/beheer"
    cookie = r.headers["set-cookie"]
    assert "bd_sessie=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/beheer").status_code == 200
    with app.state.db() as conn:
        rij = conn.execute("SELECT rol, wachtwoord_hash FROM gebruikers").fetchone()
    assert rij["rol"] == "beheerder"
    assert rij["wachtwoord_hash"].startswith("$argon2")


def test_wizard_maar_een_keer(beheerder):
    r = beheerder.get("/welkom", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/aanmelden"
    # Ook een directe POST maakt geen tweede beheerder aan.
    token = csrf(beheerder)
    r = beheerder.post("/welkom", data={"csrf_token": token, "gebruikersnaam": "indringer",
                                        "wachtwoord": WACHTWOORD, "wachtwoord2": WACHTWOORD},
                       follow_redirects=False)
    assert r.status_code == 303
    with beheerder.app.state.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gebruikers").fetchone()[0] == 1


def _afmelden(client):
    client.post("/afmelden", data={"csrf_token": csrf(client)})
    client.cookies.clear()


def _aanmelden(client, naam, ww):
    token = csrf_uit(client.get("/aanmelden").text)
    return client.post("/aanmelden", data={"csrf_token": token, "gebruikersnaam": naam, "wachtwoord": ww},
                       follow_redirects=False)


def test_aanmelden_en_afmelden(beheerder):
    _afmelden(beheerder)
    assert beheerder.get("/", follow_redirects=False).headers["location"] == "/aanmelden"
    assert _aanmelden(beheerder, "beheer", "verkeerd-wachtwoord").status_code == 401
    r = _aanmelden(beheerder, "beheer", WACHTWOORD)
    assert r.status_code == 303
    assert beheerder.get("/").status_code == 200


def test_aanmeldbeperking(beheerder):
    _afmelden(beheerder)
    for _ in range(5):
        assert _aanmelden(beheerder, "beheer", "fout-fout-fout-fout").status_code == 401
    r = _aanmelden(beheerder, "beheer", WACHTWOORD)
    assert r.status_code == 429
    assert "Te veel" in r.text


def test_lezer_rol(beheerder):
    r = beheerder.post("/beheer/gebruikers", data={"csrf_token": csrf(beheerder), "gebruikersnaam": "lezer",
                                                   "wachtwoord": WACHTWOORD, "rol": "lezer"}, follow_redirects=False)
    assert r.status_code == 303
    # Te kort wachtwoord bij toevoegen wordt geweigerd
    r = beheerder.post("/beheer/gebruikers", data={"csrf_token": csrf(beheerder), "gebruikersnaam": "x",
                                                   "wachtwoord": "kort", "rol": "lezer"})
    assert r.status_code == 422
    _afmelden(beheerder)
    assert _aanmelden(beheerder, "lezer", WACHTWOORD).status_code == 303
    assert beheerder.get("/").status_code == 200
    assert beheerder.get("/beheer").status_code == 403
    token = csrf_uit(beheerder.get("/").text)
    r = beheerder.post("/beheer/bronnen", data={"csrf_token": token, "naam": "x", "app_slug": "x",
                                                "basis_url": "http://x", "koppelsleutel": "bk_x"})
    assert r.status_code == 403
    assert beheerder.get("/api/beheer/koppelsleutels").status_code == 403


def test_gezondheid(client):
    r = client.get("/gezondheid")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_beveiligingskoppen(client):
    r = client.get("/welkom")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
