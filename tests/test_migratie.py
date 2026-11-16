from app import db as dbmod
from app.config import Config
from app.main import create_app


def test_upgrade_vanaf_0_1_behoudt_gegevens(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    pad = data / "bedrijfsdashboard.db"
    monkeypatch.setattr(dbmod, "MIGRATIES", dbmod.MIGRATIES[:1])
    assert dbmod.migreer(pad) == 1
    conn = dbmod.verbind(pad)
    conn.execute(
        "INSERT INTO bronnen (naam, app_slug, basis_url, sleutel_versleuteld, sleutel_voorvoegsel, "
        "aangemaakt_op) VALUES ('Oud', 'scouten', 'http://x', x'00', 'bk_x', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO instellingen VALUES ('interval_minuten', '7')")
    conn.close()
    monkeypatch.undo()
    app = create_app(Config(data_dir=data, achtergrond=False))
    with app.state.db() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIES)
        rij = conn.execute("SELECT naam, interval_minuten, verouderd_na_minuten FROM bronnen").fetchone()
    assert rij["naam"] == "Oud" and rij["interval_minuten"] is None and rij["verouderd_na_minuten"] is None
    assert app.state.db.instelling("interval_minuten") == "7"
