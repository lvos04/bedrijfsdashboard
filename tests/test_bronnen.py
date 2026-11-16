import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .conftest import csrf, voeg_bron_toe
from .nepbron import GELDIGE_SLEUTEL


def _rij(app):
    with app.state.db() as conn:
        return conn.execute("SELECT * FROM bronnen").fetchone()


def test_sleutel_versleuteld_opgeslagen_en_nooit_getoond(beheerder, app):
    r = voeg_bron_toe(beheerder)
    assert r.status_code == 303
    rij = _rij(app)
    assert GELDIGE_SLEUTEL.encode() not in bytes(rij["sleutel_versleuteld"])
    assert rij["sleutel_voorvoegsel"] == GELDIGE_SLEUTEL[:8]
    assert app.state.kluis.ontsleutel(rij["sleutel_versleuteld"]) == GELDIGE_SLEUTEL
    # Ook in de databasebestanden staat de sleutel niet leesbaar.
    for pad in app.state.config.data_dir.glob("bedrijfsdashboard.db*"):
        assert GELDIGE_SLEUTEL.encode() not in pad.read_bytes()
    assert (app.state.config.data_dir / "geheim.key").stat().st_mode & 0o077 == 0
    for pagina in ("/beheer", f"/beheer/bronnen/{rij['id']}", f"/bronnen/{rij['id']}", "/"):
        html = beheerder.get(pagina).text
        assert GELDIGE_SLEUTEL not in html
    assert GELDIGE_SLEUTEL[:8] in beheerder.get("/beheer").text


def test_validatie(beheerder):
    assert voeg_bron_toe(beheerder, url="ftp://x").status_code == 422
    assert voeg_bron_toe(beheerder, sleutel="").status_code == 422
    r = beheerder.post("/beheer/bronnen", data={"csrf_token": csrf(beheerder), "naam": "x", "app_slug": "Fout Slug!",
                                                "basis_url": "http://x", "koppelsleutel": "bk_1"})
    assert r.status_code == 422
    assert voeg_bron_toe(beheerder, ca_pem="geen certificaat").status_code == 422


def test_bewerken_lege_sleutel_houdt_oude(beheerder, app):
    voeg_bron_toe(beheerder)
    rij = _rij(app)
    r = beheerder.post(f"/beheer/bronnen/{rij['id']}", data={
        "csrf_token": csrf(beheerder), "naam": "Scouten 2", "app_slug": "scouten",
        "basis_url": "https://scouten.lan/", "koppelsleutel": "", "actief": "1"}, follow_redirects=False)
    assert r.status_code == 303
    nieuw = _rij(app)
    assert nieuw["naam"] == "Scouten 2" and nieuw["basis_url"] == "https://scouten.lan"
    assert app.state.kluis.ontsleutel(nieuw["sleutel_versleuteld"]) == GELDIGE_SLEUTEL
    beheerder.post(f"/beheer/bronnen/{rij['id']}", data={
        "csrf_token": csrf(beheerder), "naam": "Scouten 2", "app_slug": "scouten",
        "basis_url": "https://scouten.lan", "koppelsleutel": "bk_nieuwesleutel123", "actief": "1"})
    nieuw = _rij(app)
    assert app.state.kluis.ontsleutel(nieuw["sleutel_versleuteld"]) == "bk_nieuwesleutel123"
    assert nieuw["sleutel_voorvoegsel"] == "bk_nieuw"


def _ca_pem() -> str:
    sleutel = ec.generate_private_key(ec.SECP256R1())
    naam = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    nu = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(naam).issuer_name(naam).public_key(sleutel.public_key())
            .serial_number(1).not_valid_before(nu).not_valid_after(nu + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(sleutel, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_eigen_ca_upload(beheerder, app):
    pem = _ca_pem()
    r = beheerder.post("/beheer/bronnen", data={"csrf_token": csrf(beheerder), "naam": "Scouten",
                                                "app_slug": "scouten", "basis_url": "https://scouten.lan",
                                                "koppelsleutel": GELDIGE_SLEUTEL},
                       files={"ca_bestand": ("ca.pem", pem.encode(), "application/x-pem-file")},
                       follow_redirects=False)
    assert r.status_code == 303
    rij = _rij(app)
    assert "BEGIN CERTIFICATE" in rij["ca_pem"]
    from app.ophalen import maak_client

    client = maak_client(rij, app.state.kluis)  # bouwt SSL-context met eigen CA zonder fout
    assert client.base_url.path.rstrip("/") == "/api/koppeling/v1"
    beheerder.post(f"/beheer/bronnen/{rij['id']}", data={
        "csrf_token": csrf(beheerder), "naam": "Scouten", "app_slug": "scouten",
        "basis_url": "https://scouten.lan", "actief": "1", "ca_wissen": "1"})
    assert _rij(app)["ca_pem"] is None


def test_verwijderen(beheerder, app):
    voeg_bron_toe(beheerder)
    rij = _rij(app)
    beheerder.post(f"/beheer/bronnen/{rij['id']}/verwijderen", data={"csrf_token": csrf(beheerder)})
    assert _rij(app) is None
