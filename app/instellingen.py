"""Centrale lijst van instellingen (tabel `instellingen`) met hun standaardwaarden.

Elke optionele functie staat standaard uit, zodat een update het bestaande gedrag niet verandert.
"""

from __future__ import annotations

import ipaddress
import os

from .db import Database

STANDAARD: dict[str, str] = {
    # Algemeen
    "bedrijfsnaam": "",
    "interval_minuten": "5",
    "historie_dagen": "90",
    "verouderd_na_minuten": "",  # leeg = 3 × ophaalinterval
    # Eigen koppel-API
    "koppeling_ingeschakeld": "0",
    # Beveiliging
    "mfa_verplicht_beheerders": "0",
    "oorsprong_strikt": "0",
    "vertrouwde_proxies": "privé",
    "audit_bewaardagen": "0",  # 0 = onbeperkt bewaren
    # E-mailmeldingen
    "email_ingeschakeld": "0",
    "smtp_host": "",
    "smtp_poort": "587",
    "smtp_beveiliging": "starttls",
    "smtp_gebruiker": "",
    "smtp_wachtwoord_versleuteld": "",
    "email_afzender": "",
    "email_ontvangers": "",
    # Tv-weergave
    "tv_ingeschakeld": "0",
    "tv_token_hash": "",
}

# Interne boekhouding; niet in de configuratie-export.
INTERN = {"laatste_opschoning", "laatste_mail_fout", "laatste_mail_op"}

SMTP_BEVEILIGING = ("starttls", "ssl", "geen")


def waarde(db: Database, sleutel: str) -> str:
    v = db.instelling(sleutel)
    return v if v is not None else STANDAARD.get(sleutel, "")


def aan(db: Database, sleutel: str) -> bool:
    return waarde(db, sleutel) == "1"


def geheel(db: Database, sleutel: str, minimum: int, maximum: int) -> int | None:
    try:
        v = int(waarde(db, sleutel))
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, v))


# ---------------------------------------------------------------- vertrouwde proxy

PRIVE_NETTEN = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


def parse_proxies(tekst: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """'privé' (standaard), 'geen' of een komma-lijst van IP-adressen/CIDR's. Gooit ValueError bij fouten."""
    tekst = (tekst or "").strip().lower()
    if tekst in ("", "privé", "prive"):
        return list(PRIVE_NETTEN)
    if tekst == "geen":
        return []
    netten = []
    for deel in tekst.split(","):
        deel = deel.strip()
        if deel in ("privé", "prive"):
            netten.extend(PRIVE_NETTEN)
        elif deel:
            netten.append(ipaddress.ip_network(deel, strict=False))
    return netten


def proxies_uit_omgeving() -> str | None:
    return os.environ.get("VERTROUWDE_PROXIES") or None


def vertrouwde_proxies(db: Database) -> list:
    tekst = proxies_uit_omgeving() or waarde(db, "vertrouwde_proxies")
    try:
        return parse_proxies(tekst)
    except ValueError:
        return list(PRIVE_NETTEN)
