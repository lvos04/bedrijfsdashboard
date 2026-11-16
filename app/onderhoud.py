"""Dagelijkse opschoontaak: verlopen sessies/MFA-uitdagingen, aanmeldtellers en (optioneel) het auditlog."""

from __future__ import annotations

import logging
from datetime import timedelta

from . import audit
from .beveiliging import AanmeldBeperking, ruim_verlopen_op
from .db import Database
from .tijd import iso, nu, parse_iso

log = logging.getLogger("bedrijfsdashboard.onderhoud")
INTERVAL = timedelta(hours=24)


def opschonen(db: Database, beperking: AanmeldBeperking | None = None) -> dict:
    with db() as conn:
        sessies = ruim_verlopen_op(conn)
    audit_rijen = audit.ruim_op(db)
    if beperking is not None:
        beperking.opruimen()
    db.zet_instelling("laatste_opschoning", iso())
    if sessies or audit_rijen:
        log.info("Opgeschoond: %d verlopen sessies/uitdagingen, %d auditregels", sessies, audit_rijen)
    return {"sessies": sessies, "audit": audit_rijen}


def misschien_opschonen(db: Database, beperking: AanmeldBeperking | None = None) -> dict | None:
    laatste = parse_iso(db.instelling("laatste_opschoning"))
    if laatste is not None and nu() - laatste < INTERVAL:
        return None
    return opschonen(db, beperking)
