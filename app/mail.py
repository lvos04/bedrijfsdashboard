"""Optionele e-mailmeldingen: één mail bij nieuwe kritieke meldingen (ontdubbeld). Standaard uit."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import instellingen
from .beveiliging import sha256
from .db import Database
from .tijd import iso, leesbaar, nu
from .versleuteling import Kluis

log = logging.getLogger("bedrijfsdashboard.mail")
SMTP_TIMEOUT = 15
GEZIEN_BEWAREN = timedelta(days=7)


class MailFout(Exception):
    pass


def smtp_config(db: Database, kluis: Kluis) -> dict:
    ww_v = instellingen.waarde(db, "smtp_wachtwoord_versleuteld")
    try:
        wachtwoord = kluis.ontsleutel(ww_v.encode("ascii")) if ww_v else ""
    except ValueError:
        wachtwoord = ""
    try:
        poort = int(instellingen.waarde(db, "smtp_poort") or 587)
    except ValueError:
        poort = 587
    return {
        "host": instellingen.waarde(db, "smtp_host"),
        "poort": poort,
        "beveiliging": instellingen.waarde(db, "smtp_beveiliging") or "starttls",
        "gebruiker": instellingen.waarde(db, "smtp_gebruiker"),
        "wachtwoord": wachtwoord,
        "afzender": instellingen.waarde(db, "email_afzender"),
        "ontvangers": [o.strip() for o in instellingen.waarde(db, "email_ontvangers").split(",") if o.strip()],
    }


def volledig(cfg: dict) -> bool:
    return bool(cfg["host"] and cfg["afzender"] and cfg["ontvangers"])


def verstuur(cfg: dict, onderwerp: str, tekst: str) -> None:
    """Synchroon versturen (aanroepen via asyncio.to_thread). Gooit MailFout met een leesbare melding."""
    if not volledig(cfg):
        raise MailFout("SMTP-server, afzender en minstens één ontvanger zijn verplicht.")
    bericht = EmailMessage()
    bericht["Subject"] = onderwerp
    bericht["From"] = cfg["afzender"]
    bericht["To"] = ", ".join(cfg["ontvangers"])
    bericht["Date"] = formatdate(localtime=True)
    bericht["Message-ID"] = make_msgid(domain="bedrijfsdashboard")
    bericht.set_content(tekst)
    ctx = ssl.create_default_context()
    try:
        if cfg["beveiliging"] == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["poort"], timeout=SMTP_TIMEOUT, context=ctx)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["poort"], timeout=SMTP_TIMEOUT)
        with smtp:
            if cfg["beveiliging"] == "starttls":
                smtp.starttls(context=ctx)
            if cfg["gebruiker"]:
                smtp.login(cfg["gebruiker"], cfg["wachtwoord"])
            smtp.send_message(bericht)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailFout(f"Versturen mislukt: {exc.__class__.__name__}: {str(exc)[:200]}") from None


def melding_hash(m: dict) -> str:
    return sha256(f"{m['bron_id']}|{m['niveau']}|{m['tekst']}|{m['sinds']}")


def registreer_kritieke(db: Database) -> list[dict]:
    """Registreert de huidige kritieke meldingen en geeft de nieuwe (nog nooit geziene) terug.

    Dit gebeurt ook als e-mail uit staat, zodat het inschakelen geen stortvloed aan mails over bestaande meldingen
    geeft. Meldingen van een bron die op dat moment niet bereikbaar is (verouderd) tellen niet als nieuw.
    """
    from . import overzicht

    lijst = overzicht.bronnen(db)
    kritiek = [m for m in overzicht.gebundelde_meldingen(lijst) if m["niveau"] == "kritiek" and not m["verouderd"]]
    nieuw = []
    tijd = iso()
    with db.transactie() as conn:
        for m in kritiek:
            h = melding_hash(m)
            cur = conn.execute(
                "INSERT OR IGNORE INTO gezien_meldingen (hash, bron_id, eerst_gezien, laatst_gezien) VALUES (?, ?, ?, ?)",
                (h, m["bron_id"], tijd, tijd),
            )
            if cur.rowcount:
                nieuw.append(m)
            else:
                conn.execute("UPDATE gezien_meldingen SET laatst_gezien = ? WHERE hash = ?", (tijd, h))
        conn.execute("DELETE FROM gezien_meldingen WHERE laatst_gezien < ?", (iso(nu() - GEZIEN_BEWAREN),))
    return nieuw


def opmaak(meldingen: list[dict], bedrijfsnaam: str | None) -> tuple[str, str]:
    n = len(meldingen)
    onderwerp = f"[Bedrijfsdashboard] {n} nieuwe kritieke melding{'' if n == 1 else 'en'}"
    if bedrijfsnaam:
        onderwerp += f" – {bedrijfsnaam}"
    regels = [f"Er {'is' if n == 1 else 'zijn'} {n} nieuwe kritieke melding{'' if n == 1 else 'en'}:", ""]
    for m in meldingen:
        sinds = f" (sinds {leesbaar(m['sinds'])})" if m.get("sinds") else ""
        regels.append(f"* {m['bron_naam']}: {m['tekst']}{sinds}")
    regels += [
        "",
        "Deze mail is automatisch verstuurd door het Bedrijfsdashboard.",
        "U krijgt per melding maar één mail; uitschakelen kan onder Beheer → E-mailmeldingen.",
    ]
    return onderwerp, "\n".join(regels)


async def verwerk_nieuwe_kritieke(db: Database, kluis: Kluis) -> int:
    """Na elke ophaalronde: nieuwe kritieke meldingen mailen als e-mailmeldingen aan staan. Geeft aantal terug."""
    nieuw = registreer_kritieke(db)
    if not nieuw or not instellingen.aan(db, "email_ingeschakeld"):
        return 0
    cfg = smtp_config(db, kluis)
    onderwerp, tekst = opmaak(nieuw, db.instelling("bedrijfsnaam"))
    try:
        await asyncio.to_thread(verstuur, cfg, onderwerp, tekst)
    except MailFout as exc:
        log.warning("E-mailmelding niet verstuurd: %s", exc)
        db.zet_instelling("laatste_mail_fout", f"{iso()} {exc}")
        # Niet als gezien markeren, zodat de volgende ophaalronde het opnieuw probeert.
        with db() as conn:
            conn.executemany("DELETE FROM gezien_meldingen WHERE hash = ?", [(melding_hash(m),) for m in nieuw])
        return 0
    db.zet_instelling("laatste_mail_op", iso())
    db.zet_instelling("laatste_mail_fout", "")
    return len(nieuw)
