"""Auditlog: wie deed wat en wanneer. Er worden nooit geheimen (wachtwoorden, sleutels, tokens) gelogd."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from . import instellingen
from .db import Database
from .tijd import iso, nu

log = logging.getLogger("bedrijfsdashboard.audit")

# Sleutelnamen in `details` die op een geheim wijzen worden altijd weggelaten (vangnet).
_GEHEIM = re.compile(r"wachtwoord|password|token|geheim|secret|^sleutel$|^koppelsleutel$|hash", re.IGNORECASE)
PER_PAGINA = 50

ACTIES = {
    "welkom.beheerder_aangemaakt": "Eerste beheerder aangemaakt",
    "aanmelden.gelukt": "Aangemeld",
    "aanmelden.mislukt": "Aanmelden mislukt",
    "aanmelden.geblokkeerd": "Aanmelden geblokkeerd (te veel pogingen)",
    "aanmelden.mfa_gevraagd": "Wachtwoord juist, MFA-code gevraagd",
    "aanmelden.mfa_mislukt": "Onjuiste MFA-code",
    "afmelden": "Afgemeld",
    "account.wachtwoord_gewijzigd": "Eigen wachtwoord gewijzigd",
    "account.mfa_ingeschakeld": "MFA ingeschakeld",
    "account.mfa_uitgeschakeld": "MFA uitgeschakeld",
    "bron.toegevoegd": "Bron toegevoegd",
    "bron.bijgewerkt": "Bron bijgewerkt",
    "bron.verwijderd": "Bron verwijderd",
    "koppelsleutel.aangemaakt": "Koppelsleutel aangemaakt",
    "koppelsleutel.ingetrokken": "Koppelsleutel ingetrokken",
    "gebruiker.toegevoegd": "Gebruiker toegevoegd",
    "gebruiker.verwijderd": "Gebruiker verwijderd",
    "gebruiker.wachtwoord_gereset": "Wachtwoord van gebruiker gereset",
    "instellingen.gewijzigd": "Instellingen gewijzigd",
    "tv.link_vernieuwd": "Nieuwe tv-link gemaakt",
    "email.test": "Testmail verstuurd",
    "back_up.geexporteerd": "Configuratie geëxporteerd",
    "back_up.teruggezet": "Configuratie teruggezet",
}


def _schoon(details):
    if isinstance(details, dict):
        return {k: _schoon(v) for k, v in details.items() if not _GEHEIM.search(str(k))}
    if isinstance(details, list):
        return [_schoon(v) for v in details]
    return details


def schrijf(
    db_of_conn, actie: str, gebruiker: str | None = None, ip: str | None = None, details: dict | None = None
) -> None:
    """Schrijf een auditregel. Een fout in het auditlog mag de actie zelf nooit laten mislukken."""
    tekst = json.dumps(_schoon(details), ensure_ascii=False)[:4000] if details else None
    sql = "INSERT INTO audit_log (tijd, gebruiker, actie, details, ip) VALUES (?, ?, ?, ?, ?)"
    args = (iso(), (gebruiker or None) and gebruiker[:64], actie, tekst, ip)
    try:
        if isinstance(db_of_conn, Database):
            with db_of_conn() as conn:
                conn.execute(sql, args)
        else:
            db_of_conn.execute(sql, args)
    except Exception:  # pragma: no cover
        log.exception("Auditregel schrijven mislukt (%s)", actie)


def pagina(db: Database, nummer: int = 1, per_pagina: int = PER_PAGINA, actie: str | None = None) -> dict:
    nummer = max(1, nummer)
    waar, args = "", []
    if actie:
        waar, args = "WHERE actie = ? OR actie LIKE ?", [actie, actie + ".%"]
    with db() as conn:
        totaal = conn.execute(f"SELECT COUNT(*) FROM audit_log {waar}", args).fetchone()[0]
        rijen = [
            dict(r)
            for r in conn.execute(
                f"SELECT id, tijd, gebruiker, actie, details, ip FROM audit_log {waar} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, per_pagina, (nummer - 1) * per_pagina],
            )
        ]
    for r in rijen:
        try:
            r["details"] = json.loads(r["details"]) if r["details"] else None
        except ValueError:
            pass
        r["omschrijving"] = ACTIES.get(r["actie"], r["actie"])
    paginas = max(1, -(-totaal // per_pagina))
    return {"items": rijen, "pagina": nummer, "paginas": paginas, "totaal": totaal, "per_pagina": per_pagina}


def ruim_op(db: Database) -> int:
    """Alleen als er een bewaartermijn is ingesteld (standaard 0 = onbeperkt bewaren)."""
    dagen = instellingen.geheel(db, "audit_bewaardagen", 0, 36500) or 0
    if dagen <= 0:
        return 0
    with db() as conn:
        return conn.execute("DELETE FROM audit_log WHERE tijd < ?", (iso(nu() - timedelta(days=dagen)),)).rowcount
