"""Versleutelde export en terugzetten van de configuratie (bronnen, instellingen, eigen koppelsleutels).

Het hele bestand is versleuteld met een sleutel die met scrypt uit het herstelwachtwoord wordt afgeleid (Fernet:
AES-128-CBC + HMAC-SHA256). Zonder herstelwachtwoord is het bestand onbruikbaar; het is dus ook onafhankelijk
van `geheim.key` en kan op een nieuwe installatie worden teruggezet.

Niet in de export: gebruikers, sessies, auditlog, KPI-historie en de opgehaalde gegevens.
"""

from __future__ import annotations

import base64
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import __version__, instellingen
from .db import Database
from .tijd import iso
from .versleuteling import Kluis

FORMAAT = "bedrijfsdashboard-configuratie"
FORMAAT_VERSIE = 1
MAX_GROOTTE = 5_000_000
KDF = {"naam": "scrypt", "n": 2**15, "r": 8, "p": 1}


class BackUpFout(Exception):
    pass


def _afgeleide_sleutel(wachtwoord: str, zout: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=zout, length=32, n=n, r=r, p=p)
    return base64.urlsafe_b64encode(kdf.derive(wachtwoord.encode("utf-8")))


def exporteer(db: Database, kluis: Kluis, herstelwachtwoord: str) -> bytes:
    with db() as conn:
        bronnen = []
        for b in conn.execute("SELECT * FROM bronnen ORDER BY id"):
            bronnen.append(
                {
                    "naam": b["naam"],
                    "app_slug": b["app_slug"],
                    "basis_url": b["basis_url"],
                    "koppelsleutel": kluis.ontsleutel(b["sleutel_versleuteld"]),
                    "ca_pem": b["ca_pem"],
                    "actief": bool(b["actief"]),
                    "interval_minuten": b["interval_minuten"],
                    "verouderd_na_minuten": b["verouderd_na_minuten"],
                    "aangemaakt_op": b["aangemaakt_op"],
                }
            )
        inst = {r["sleutel"]: r["waarde"] for r in conn.execute("SELECT sleutel, waarde FROM instellingen")}
        sleutels = [
            dict(r)
            for r in conn.execute(
                "SELECT naam, voorvoegsel, hash, aangemaakt_op, aangemaakt_door, ingetrokken_op FROM koppelsleutels"
            )
        ]
    # SMTP-wachtwoord staat lokaal versleuteld met geheim.key; in de export (zelf versleuteld) als platte tekst.
    smtp_v = inst.pop("smtp_wachtwoord_versleuteld", "")
    inst = {k: v for k, v in inst.items() if k not in instellingen.INTERN}
    if smtp_v:
        try:
            inst["smtp_wachtwoord"] = kluis.ontsleutel(smtp_v.encode("ascii"))
        except ValueError:
            pass
    inhoud = {"bronnen": bronnen, "instellingen": inst, "koppelsleutels": sleutels}
    zout = secrets.token_bytes(16)
    sleutel = _afgeleide_sleutel(herstelwachtwoord, zout, KDF["n"], KDF["r"], KDF["p"])
    token = Fernet(sleutel).encrypt(json.dumps(inhoud, ensure_ascii=False).encode("utf-8"))
    bestand = {
        "formaat": FORMAAT,
        "formaat_versie": FORMAAT_VERSIE,
        "app_versie": __version__,
        "gemaakt_op": iso(),
        "kdf": {**KDF, "zout": base64.b64encode(zout).decode("ascii")},
        "versleuteld": token.decode("ascii"),
        "toelichting": "Versleutelde configuratie van het Bedrijfsdashboard. Terugzetten via Beheer → Back-up.",
    }
    return json.dumps(bestand, indent=2, ensure_ascii=False).encode("utf-8")


def ontsleutel(data: bytes, herstelwachtwoord: str) -> dict:
    if len(data) > MAX_GROOTTE:
        raise BackUpFout("Het bestand is te groot.")
    try:
        bestand = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackUpFout("Dit is geen geldig exportbestand van het Bedrijfsdashboard.") from None
    if not isinstance(bestand, dict) or bestand.get("formaat") != FORMAAT:
        raise BackUpFout("Dit is geen geldig exportbestand van het Bedrijfsdashboard.")
    if bestand.get("formaat_versie") != FORMAAT_VERSIE:
        raise BackUpFout("Deze versie van het exportbestand wordt niet ondersteund.")
    kdf = bestand.get("kdf") or {}
    try:
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        if kdf.get("naam") != "scrypt" or not (2**14 <= n <= 2**20 and 1 <= r <= 16 and 1 <= p <= 4):
            raise ValueError
        zout = base64.b64decode(kdf["zout"])
        sleutel = _afgeleide_sleutel(herstelwachtwoord, zout, n, r, p)
        inhoud = json.loads(Fernet(sleutel).decrypt(str(bestand["versleuteld"]).encode("ascii")))
    except InvalidToken:
        raise BackUpFout("Onjuist herstelwachtwoord of beschadigd bestand.") from None
    except (KeyError, ValueError, TypeError):
        raise BackUpFout("Het exportbestand is beschadigd.") from None
    if not isinstance(inhoud, dict):
        raise BackUpFout("Het exportbestand is beschadigd.")
    return inhoud


def zet_terug(db: Database, kluis: Kluis, inhoud: dict, valideer_bron) -> dict:
    """Niet-destructief: bronnen met dezelfde app-slug + basis-URL worden bijgewerkt, andere toegevoegd; bronnen die
    niet in het bestand staan blijven bestaan. Instellingen uit het bestand overschrijven de huidige. Eigen
    koppelsleutels worden toegevoegd als hun hash nog niet bestaat.

    ``valideer_bron(dict) -> str | None`` controleert elke bron (zelfde regels als het formulier).
    """
    bronnen = inhoud.get("bronnen") or []
    inst = inhoud.get("instellingen") or {}
    sleutels = inhoud.get("koppelsleutels") or []
    if not isinstance(bronnen, list) or not isinstance(inst, dict) or not isinstance(sleutels, list):
        raise BackUpFout("Het exportbestand is beschadigd.")
    for b in bronnen:
        if not isinstance(b, dict) or not isinstance(b.get("koppelsleutel"), str):
            raise BackUpFout("Het exportbestand bevat een ongeldige bron.")
        fout = valideer_bron(b)
        if fout:
            raise BackUpFout(f"Bron '{b.get('naam')}': {fout}")
    toegestaan = set(instellingen.STANDAARD) | {"smtp_wachtwoord"}
    teller = {"bronnen_nieuw": 0, "bronnen_bijgewerkt": 0, "instellingen": 0, "koppelsleutels": 0}
    with db.transactie() as conn:
        for b in bronnen:
            velden = (
                b["naam"][:100],
                kluis.versleutel(b["koppelsleutel"]),
                b["koppelsleutel"][:8],
                b.get("ca_pem") or None,
                int(bool(b.get("actief", True))),
                _int_of_none(b.get("interval_minuten"), 1, 1440),
                _int_of_none(b.get("verouderd_na_minuten"), 1, 100000),
            )
            bestaand = conn.execute(
                "SELECT id FROM bronnen WHERE app_slug = ? AND basis_url = ?", (b["app_slug"], b["basis_url"])
            ).fetchone()
            if bestaand:
                conn.execute(
                    "UPDATE bronnen SET naam = ?, sleutel_versleuteld = ?, sleutel_voorvoegsel = ?, ca_pem = ?, "
                    "actief = ?, interval_minuten = ?, verouderd_na_minuten = ? WHERE id = ?",
                    (*velden, bestaand["id"]),
                )
                teller["bronnen_bijgewerkt"] += 1
            else:
                conn.execute(
                    "INSERT INTO bronnen (naam, sleutel_versleuteld, sleutel_voorvoegsel, ca_pem, actief, "
                    "interval_minuten, verouderd_na_minuten, app_slug, basis_url, aangemaakt_op) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*velden, b["app_slug"], b["basis_url"], iso()),
                )
                teller["bronnen_nieuw"] += 1
        for k, v in inst.items():
            if k not in toegestaan or not isinstance(v, str):
                continue
            if k == "smtp_wachtwoord":
                k, v = "smtp_wachtwoord_versleuteld", kluis.versleutel(v).decode("ascii") if v else ""
            conn.execute(
                "INSERT INTO instellingen (sleutel, waarde) VALUES (?, ?) "
                "ON CONFLICT(sleutel) DO UPDATE SET waarde = excluded.waarde",
                (k, v[:5000]),
            )
            teller["instellingen"] += 1
        for s in sleutels:
            if not isinstance(s, dict) or not all(isinstance(s.get(k), str) for k in ("naam", "voorvoegsel", "hash")):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO koppelsleutels (naam, voorvoegsel, hash, aangemaakt_op, aangemaakt_door, "
                "ingetrokken_op) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    s["naam"][:100],
                    s["voorvoegsel"][:8],
                    s["hash"][:64],
                    s.get("aangemaakt_op") or iso(),
                    s.get("aangemaakt_door"),
                    s.get("ingetrokken_op"),
                ),
            )
            teller["koppelsleutels"] += cur.rowcount
    return teller


def _int_of_none(v, lo: int, hi: int) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return None
