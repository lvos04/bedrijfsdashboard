from .conftest import csrf


def test_post_zonder_token_geweigerd(beheerder):
    r = beheerder.post("/beheer/instellingen", data={"interval_minuten": "5", "historie_dagen": "90"})
    assert r.status_code == 403


def test_post_met_verkeerd_token_geweigerd(beheerder):
    r = beheerder.post(
        "/beheer/instellingen", data={"csrf_token": "nep", "interval_minuten": "5", "historie_dagen": "90"}
    )
    assert r.status_code == 403


def test_post_vreemde_oorsprong_geweigerd(beheerder):
    r = beheerder.post(
        "/beheer/instellingen",
        headers={"Origin": "https://kwaadaardig.example"},
        data={"csrf_token": csrf(beheerder), "interval_minuten": "5", "historie_dagen": "90"},
    )
    assert r.status_code == 403


def test_post_juiste_oorsprong_en_token(beheerder):
    r = beheerder.post(
        "/beheer/instellingen",
        headers={"Origin": "http://testserver"},
        data={
            "csrf_token": csrf(beheerder),
            "interval_minuten": "7",
            "historie_dagen": "30",
            "bedrijfsnaam": "Kwekerij",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert beheerder.app.state.db.instelling("interval_minuten") == "7"


def test_aanmelden_zonder_token_geweigerd(client):
    from .conftest import wizard

    wizard(client)
    client.cookies.clear()
    r = client.post("/aanmelden", data={"gebruikersnaam": "beheer", "wachtwoord": "x" * 12})
    assert r.status_code == 403


def test_json_beheer_api_vereist_csrf_header(beheerder):
    assert beheerder.post("/api/beheer/koppelsleutels", json={"naam": "x"}).status_code == 403
    r = beheerder.post("/api/beheer/koppelsleutels", json={"naam": "x"}, headers={"X-CSRF-Token": csrf(beheerder)})
    assert r.status_code == 201
