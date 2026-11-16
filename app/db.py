"""SQLite-toegang met eenvoudige, versiebeheerde migraties (PRAGMA user_version)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIES: list[str] = [
    # 1: basis
    """
    CREATE TABLE gebruikers (
        id INTEGER PRIMARY KEY,
        gebruikersnaam TEXT NOT NULL UNIQUE COLLATE NOCASE,
        wachtwoord_hash TEXT NOT NULL,
        rol TEXT NOT NULL CHECK (rol IN ('beheerder', 'lezer')),
        aangemaakt_op TEXT NOT NULL
    );
    CREATE TABLE sessies (
        token_hash TEXT PRIMARY KEY,
        gebruiker_id INTEGER NOT NULL REFERENCES gebruikers(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        aangemaakt_op TEXT NOT NULL,
        verloopt_op TEXT NOT NULL
    );
    CREATE TABLE instellingen (
        sleutel TEXT PRIMARY KEY,
        waarde TEXT NOT NULL
    );
    CREATE TABLE bronnen (
        id INTEGER PRIMARY KEY,
        naam TEXT NOT NULL,
        app_slug TEXT NOT NULL,
        basis_url TEXT NOT NULL,
        sleutel_versleuteld BLOB NOT NULL,
        sleutel_voorvoegsel TEXT NOT NULL,
        ca_pem TEXT,
        actief INTEGER NOT NULL DEFAULT 1,
        aangemaakt_op TEXT NOT NULL
    );
    CREATE TABLE bron_status (
        bron_id INTEGER PRIMARY KEY REFERENCES bronnen(id) ON DELETE CASCADE,
        laatste_poging TEXT,
        laatste_succes TEXT,
        fout TEXT,
        fout_sinds TEXT,
        info_json TEXT,
        samenvatting_json TEXT
    );
    CREATE TABLE kpi_historie (
        id INTEGER PRIMARY KEY,
        bron_id INTEGER NOT NULL REFERENCES bronnen(id) ON DELETE CASCADE,
        sleutel TEXT NOT NULL,
        tijd TEXT NOT NULL,
        waarde REAL,
        status TEXT
    );
    CREATE INDEX idx_kpi_historie ON kpi_historie (bron_id, sleutel, tijd);
    CREATE TABLE koppelsleutels (
        id INTEGER PRIMARY KEY,
        naam TEXT NOT NULL,
        voorvoegsel TEXT NOT NULL,
        hash TEXT NOT NULL UNIQUE,
        aangemaakt_op TEXT NOT NULL,
        aangemaakt_door TEXT,
        laatst_gebruikt_op TEXT,
        ingetrokken_op TEXT
    );
    """,
    # 2: auditlog, MFA, per-bron interval, ontdubbeling van e-mailmeldingen (0.2.0)
    """
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        tijd TEXT NOT NULL,
        gebruiker TEXT,
        actie TEXT NOT NULL,
        details TEXT,
        ip TEXT
    );
    CREATE INDEX idx_audit_tijd ON audit_log (tijd);
    ALTER TABLE gebruikers ADD COLUMN totp_geheim BLOB;
    ALTER TABLE gebruikers ADD COLUMN totp_nieuw BLOB;
    ALTER TABLE gebruikers ADD COLUMN totp_laatste_stap INTEGER;
    CREATE TABLE mfa_uitdagingen (
        token_hash TEXT PRIMARY KEY,
        gebruiker_id INTEGER NOT NULL REFERENCES gebruikers(id) ON DELETE CASCADE,
        verloopt_op TEXT NOT NULL,
        pogingen INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_sessies_verloopt ON sessies (verloopt_op);
    CREATE INDEX idx_sessies_gebruiker ON sessies (gebruiker_id);
    ALTER TABLE bronnen ADD COLUMN interval_minuten INTEGER;
    ALTER TABLE bronnen ADD COLUMN verouderd_na_minuten INTEGER;
    ALTER TABLE bron_status ADD COLUMN fout_code TEXT;
    CREATE TABLE gezien_meldingen (
        hash TEXT PRIMARY KEY,
        bron_id INTEGER NOT NULL REFERENCES bronnen(id) ON DELETE CASCADE,
        eerst_gezien TEXT NOT NULL,
        laatst_gezien TEXT NOT NULL
    );
    """,
]


def verbind(pad: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(pad, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migreer(pad: Path) -> int:
    conn = verbind(pad)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        versie = conn.execute("PRAGMA user_version").fetchone()[0]
        for nummer, sql in enumerate(MIGRATIES, start=1):
            if nummer <= versie:
                continue
            conn.execute("BEGIN")
            try:
                for stmt in _splits(sql):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {nummer}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            versie = nummer
        return versie
    finally:
        conn.close()


def _splits(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


class Database:
    def __init__(self, pad: Path):
        self.pad = pad

    @contextmanager
    def __call__(self) -> Iterator[sqlite3.Connection]:
        conn = verbind(self.pad)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transactie(self) -> Iterator[sqlite3.Connection]:
        conn = verbind(self.pad)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def instelling(self, sleutel: str, standaard: str | None = None) -> str | None:
        with self() as conn:
            rij = conn.execute("SELECT waarde FROM instellingen WHERE sleutel = ?", (sleutel,)).fetchone()
        return rij["waarde"] if rij else standaard

    def zet_instelling(self, sleutel: str, waarde: str) -> None:
        with self() as conn:
            conn.execute(
                "INSERT INTO instellingen (sleutel, waarde) VALUES (?, ?) "
                "ON CONFLICT(sleutel) DO UPDATE SET waarde = excluded.waarde",
                (sleutel, waarde),
            )
