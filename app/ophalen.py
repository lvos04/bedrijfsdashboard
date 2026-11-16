"""Ophalen van /info en /samenvatting bij de bronnen, normaliseren, cachen en historie bijhouden."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .db import Database
from .tijd import iso, nu, parse_iso
from .versleuteling import Kluis

log = logging.getLogger("bedrijfsdashboard.ophalen")

API_PREFIX = "/api/koppeling/v1"
STATUSSEN = ("ok", "let_op", "actie", "onbekend")
NIVEAUS = ("info", "waarschuwing", "kritiek")
DATASET_NAAM = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
STANDAARD_TIMEOUT = 10.0
STANDAARD_INTERVAL_MIN = 5
STANDAARD_HISTORIE_DAGEN = 90


KOPPELING_UIT = "koppeling_uitgeschakeld"

# Waar de schakelaar "Bedrijfskoppeling ingeschakeld" in elke app staat (voor een duidelijke uitleg bij 403).
SCHAKELAAR_LOCATIE = {
    "milieu-registratie": "Instellingen → Koppelingen",
    "gewichtsverwachting": "Beheer → Bedrijfskoppeling",
    "scouten": "Beheer & instellingen → Koppelingen",
    "tuinwerk-os": "Beheer → Koppelsleutels",
    "bedrijfsdashboard": "Beheer → Koppelsleutels van dit dashboard",
}


class OphaalFout(Exception):
    def __init__(self, tekst: str, status_code: int | None = None, data: Any = None, code: str | None = None):
        super().__init__(tekst)
        self.tekst = tekst
        self.status_code = status_code
        self.data = data
        self.code = code


def uitleg_koppeling_uit(bron) -> str:
    naam = bron["naam"] if bron is not None else "de bron-app"
    plek = SCHAKELAAR_LOCATIE.get(bron["app_slug"] if bron is not None else "", "het beheer van koppelsleutels")
    return (
        f"Koppeling staat uit in {naam}: zet hem aan onder {plek} "
        '(schakelaar "Bedrijfskoppeling ingeschakeld") en klik daarna op Verbinding testen.'
    )


# ---------------------------------------------------------------- normaliseren


def getal(waarde: Any) -> float | int | None:
    """JSON-getal of string-getal -> getal; al het andere -> None (nooit verzinnen)."""
    if isinstance(waarde, bool) or waarde is None:
        return None
    if isinstance(waarde, (int, float)):
        return waarde
    if isinstance(waarde, str):
        try:
            d = Decimal(waarde.strip())
        except InvalidOperation:
            return None
        if not d.is_finite():
            return None
        return int(d) if d == d.to_integral_value() and "." not in waarde else float(d)
    return None


def _tekst(waarde: Any, max_len: int = 500) -> str | None:
    if waarde is None:
        return None
    return str(waarde)[:max_len]


def normaliseer_samenvatting(data: Any) -> dict:
    if not isinstance(data, dict):
        raise OphaalFout("Ongeldige samenvatting: geen JSON-object")
    kpis = []
    for k in data.get("kpis") or []:
        if not isinstance(k, dict) or not k.get("sleutel"):
            continue
        ruwe = k.get("waarde")
        num = getal(ruwe)
        kpis.append(
            {
                "sleutel": _tekst(k.get("sleutel"), 100),
                "label": _tekst(k.get("label") or k.get("sleutel"), 200),
                "waarde": num,
                # Niet-numerieke tekstwaarde tonen we letterlijk, maar slaan we niet op in de historie.
                "waarde_tekst": None if num is not None or ruwe is None else _tekst(ruwe, 100),
                "eenheid": _tekst(k.get("eenheid"), 30),
                "status": k.get("status") if k.get("status") in STATUSSEN else "onbekend",
                "toelichting": _tekst(k.get("toelichting")),
            }
        )
    meldingen = []
    for m in data.get("meldingen") or []:
        if not isinstance(m, dict) or not m.get("tekst"):
            continue
        meldingen.append(
            {
                "niveau": m.get("niveau") if m.get("niveau") in NIVEAUS else "info",
                "tekst": _tekst(m.get("tekst")),
                "sinds": m.get("sinds") if parse_iso(m.get("sinds")) else None,
            }
        )
    return {
        "app": _tekst(data.get("app"), 100),
        "gegenereerd_op": data.get("gegenereerd_op") if parse_iso(data.get("gegenereerd_op")) else None,
        "kpis": kpis,
        "meldingen": meldingen,
    }


def normaliseer_info(data: Any) -> dict:
    if not isinstance(data, dict):
        raise OphaalFout("Ongeldige info: geen JSON-object")
    datasets = [d for d in (data.get("datasets") or []) if isinstance(d, str) and DATASET_NAAM.match(d)]
    return {
        "app": _tekst(data.get("app"), 100),
        "naam": _tekst(data.get("naam"), 200),
        "versie": _tekst(data.get("versie"), 50),
        "api_versie": _tekst(data.get("api_versie"), 10),
        "bedrijf": _tekst(data.get("bedrijf"), 200),
        "datasets": datasets,
        "tijd": _tekst(data.get("tijd"), 40),
    }


# ---------------------------------------------------------------- HTTP


def maak_client(
    bron, kluis: Kluis, transport: httpx.AsyncBaseTransport | None = None, timeout: float = STANDAARD_TIMEOUT
) -> httpx.AsyncClient:
    sleutel = kluis.ontsleutel(bron["sleutel_versleuteld"])
    verify: Any = True
    if bron["ca_pem"]:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=bron["ca_pem"])
        verify = ctx
    return httpx.AsyncClient(
        base_url=bron["basis_url"].rstrip("/") + API_PREFIX,
        headers={"Authorization": f"Bearer {sleutel}", "Accept": "application/json", "User-Agent": "Bedrijfsdashboard"},
        timeout=timeout,
        verify=verify,
        transport=transport,
        follow_redirects=False,
    )


async def _get_json(client: httpx.AsyncClient, pad: str, params: dict | None = None, bron=None) -> Any:
    try:
        resp = await client.get(pad, params=params)
    except httpx.TimeoutException:
        raise OphaalFout(f"Time-out: geen antwoord binnen {client.timeout.read or STANDAARD_TIMEOUT:.0f} s") from None
    except httpx.ConnectError as exc:
        tekst = str(exc)
        if "CERTIFICATE" in tekst.upper() or "SSL" in tekst.upper():
            raise OphaalFout("TLS-certificaat niet vertrouwd (stel eventueel een eigen CA (PEM) in)") from None
        raise OphaalFout(f"Verbinding mislukt: {tekst or exc.__class__.__name__}") from None
    except httpx.HTTPError as exc:
        raise OphaalFout(f"Verbindingsfout: {exc.__class__.__name__}") from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code == 401:
        raise OphaalFout("Koppelsleutel geweigerd (401)", 401, data)
    if resp.status_code == 404:
        fout = data.get("fout") if isinstance(data, dict) else None
        if fout == "onbekende_dataset":
            raise OphaalFout("Onbekende dataset (404)", 404, data)
        raise OphaalFout("Koppel-API niet gevonden (404): ondersteunt de app Bedrijfskoppeling API v1?", 404, data)
    if resp.status_code == 403 and isinstance(data, dict) and data.get("fout") == KOPPELING_UIT:
        raise OphaalFout(uitleg_koppeling_uit(bron), 403, data, code=KOPPELING_UIT)
    if resp.status_code == 422:
        raise OphaalFout("Ongeldige parameters (422)", 422, data)
    if resp.status_code >= 400:
        raise OphaalFout(f"Bron gaf HTTP {resp.status_code}", resp.status_code, data)
    if data is None:
        raise OphaalFout("Ongeldig antwoord: geen JSON")
    return data


async def test_verbinding(bron, kluis: Kluis, transport=None) -> dict:
    async with maak_client(bron, kluis, transport) as client:
        return normaliseer_info(await _get_json(client, "/info", bron=bron))


async def haal_dataset(bron, kluis: Kluis, naam: str, params: dict, transport=None) -> dict:
    if not DATASET_NAAM.match(naam):
        raise OphaalFout("Ongeldige datasetnaam", 422)
    async with maak_client(bron, kluis, transport) as client:
        data = await _get_json(client, f"/datasets/{naam}", params=params, bron=bron)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise OphaalFout("Ongeldig antwoord van dataset")
    return data


# ---------------------------------------------------------------- ophalen + opslaan


async def haal_bron_op(
    db: Database, kluis: Kluis, bron, transport=None, timeout: float = STANDAARD_TIMEOUT
) -> str | None:
    """Haalt info+samenvatting op voor één bron en slaat het resultaat op. Geeft foutmelding of None."""
    tijdstip = iso()
    try:
        async with maak_client(bron, kluis, transport, timeout) as client:
            info = normaliseer_info(await _get_json(client, "/info", bron=bron))
            samenvatting = normaliseer_samenvatting(await _get_json(client, "/samenvatting", bron=bron))
    except OphaalFout as exc:
        _sla_fout_op(db, bron["id"], tijdstip, exc.tekst, exc.code)
        return exc.tekst
    except Exception as exc:  # onverwacht, bijv. ontsleutelfout of ongeldige CA
        log.exception("Onverwachte fout bij ophalen bron %s", bron["id"])
        tekst = f"Interne fout: {exc}"
        _sla_fout_op(db, bron["id"], tijdstip, tekst)
        return tekst

    with db.transactie() as conn:
        conn.execute(
            "INSERT INTO bron_status (bron_id, laatste_poging, laatste_succes, fout, fout_sinds, info_json, "
            "samenvatting_json) VALUES (?, ?, ?, NULL, NULL, ?, ?) ON CONFLICT(bron_id) DO UPDATE SET "
            "laatste_poging = excluded.laatste_poging, laatste_succes = excluded.laatste_succes, fout = NULL, "
            "fout_code = NULL, fout_sinds = NULL, info_json = excluded.info_json, samenvatting_json = excluded.samenvatting_json",
            (bron["id"], tijdstip, tijdstip, json.dumps(info), json.dumps(samenvatting)),
        )
        for kpi in samenvatting["kpis"]:
            conn.execute(
                "INSERT INTO kpi_historie (bron_id, sleutel, tijd, waarde, status) VALUES (?, ?, ?, ?, ?)",
                (
                    bron["id"],
                    kpi["sleutel"],
                    tijdstip,
                    float(kpi["waarde"]) if kpi["waarde"] is not None else None,
                    kpi["status"],
                ),
            )
    return None


def _sla_fout_op(db: Database, bron_id: int, tijdstip: str, tekst: str, code: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO bron_status (bron_id, laatste_poging, fout, fout_code, fout_sinds) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(bron_id) DO UPDATE SET laatste_poging = excluded.laatste_poging, fout = excluded.fout, "
            "fout_code = excluded.fout_code, fout_sinds = COALESCE(bron_status.fout_sinds, excluded.fout_sinds)",
            (bron_id, tijdstip, tekst, code, tijdstip),
        )


def interval_minuten(db: Database) -> int:
    try:
        return max(1, min(1440, int(db.instelling("interval_minuten", str(STANDAARD_INTERVAL_MIN)))))
    except (TypeError, ValueError):
        return STANDAARD_INTERVAL_MIN


def verouderd_na_minuten(db: Database) -> int | None:
    """Globale instelling "verouderd na"; None = 3 × het ophaalinterval (oorspronkelijk gedrag)."""
    try:
        waarde = db.instelling("verouderd_na_minuten") or ""
        return max(1, min(100000, int(waarde))) if waarde.strip() else None
    except (TypeError, ValueError):
        return None


def bron_interval(bron, globaal: int) -> int:
    """Ophaalinterval van één bron: eigen waarde, anders het globale interval."""
    eigen = bron["interval_minuten"] if "interval_minuten" in bron.keys() else None
    return int(eigen) if eigen else globaal


def bron_verouderd_na(bron, globaal_interval: int, globaal_verouderd: int | None) -> int:
    eigen = bron["verouderd_na_minuten"] if "verouderd_na_minuten" in bron.keys() else None
    if eigen:
        return int(eigen)
    if globaal_verouderd:
        return globaal_verouderd
    return 3 * bron_interval(bron, globaal_interval)


def historie_dagen(db: Database) -> int:
    try:
        return max(1, min(3650, int(db.instelling("historie_dagen", str(STANDAARD_HISTORIE_DAGEN)))))
    except (TypeError, ValueError):
        return STANDAARD_HISTORIE_DAGEN


def ruim_historie_op(db: Database) -> None:
    grens = iso(nu() - timedelta(days=historie_dagen(db)))
    with db() as conn:
        conn.execute("DELETE FROM kpi_historie WHERE tijd < ?", (grens,))


def _actieve_bronnen(db: Database) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT b.*, s.laatste_poging FROM bronnen b LEFT JOIN bron_status s ON s.bron_id = b.id WHERE b.actief = 1"
        ).fetchall()


def _seconden_tot_volgende(bron, globaal: int) -> float:
    laatste = parse_iso(bron["laatste_poging"])
    if laatste is None:
        return 0.0
    return bron_interval(bron, globaal) * 60 - (nu() - laatste).total_seconds()


async def haal_alle_op(
    db: Database, kluis: Kluis, transport=None, alleen_verlopen: bool = False
) -> dict[int, str | None]:
    """Haalt alle actieve bronnen op (of, met ``alleen_verlopen``, alleen de bronnen waarvan het eigen interval
    verstreken is) en verwerkt daarna nieuwe kritieke meldingen voor e-mail."""
    globaal = interval_minuten(db)
    bronnen = _actieve_bronnen(db)
    if alleen_verlopen:
        bronnen = [b for b in bronnen if _seconden_tot_volgende(b, globaal) <= 5]
    resultaten = await asyncio.gather(*(haal_bron_op(db, kluis, b, transport) for b in bronnen))
    ruim_historie_op(db)
    if bronnen:
        from . import mail

        try:
            await mail.verwerk_nieuwe_kritieke(db, kluis)
        except Exception:  # pragma: no cover - mag het ophalen nooit breken
            log.exception("Verwerken van e-mailmeldingen mislukt")
    return {b["id"]: r for b, r in zip(bronnen, resultaten, strict=True)}


def wachttijd_s(db: Database) -> float:
    """Tijd tot de eerstvolgende bron aan de beurt is (tussen 5 s en het globale interval)."""
    globaal = interval_minuten(db)
    bronnen = _actieve_bronnen(db)
    if not bronnen:
        return globaal * 60
    return max(5.0, min(globaal * 60, min(_seconden_tot_volgende(b, globaal) for b in bronnen)))


class Planner:
    """Achtergrondtaak die elke bron volgens zijn (eigen of globale) interval ophaalt en dagelijks opruimt."""

    def __init__(self, db: Database, kluis: Kluis, onderhoud=None):
        self.db = db
        self.kluis = kluis
        self.onderhoud = onderhoud  # functie zonder argumenten, elke ronde aangeroepen (doet zelf max. 1×/dag)
        self.nu_ophalen = asyncio.Event()
        self._taak: asyncio.Task | None = None

    def start(self) -> None:
        self._taak = asyncio.create_task(self._lus(), name="bronnen-ophalen")

    async def stop(self) -> None:
        if self._taak:
            self._taak.cancel()
            try:
                await self._taak
            except asyncio.CancelledError:
                pass

    async def _lus(self) -> None:
        alles = True  # eerste ronde en na "nu ophalen": alle bronnen
        while True:
            try:
                await haal_alle_op(self.db, self.kluis, alleen_verlopen=not alles)
            except Exception:  # pragma: no cover
                log.exception("Fout in ophaalronde")
            if self.onderhoud is not None:
                try:
                    self.onderhoud()
                except Exception:  # pragma: no cover
                    log.exception("Fout in opschoontaak")
            self.nu_ophalen.clear()
            try:
                await asyncio.wait_for(self.nu_ophalen.wait(), timeout=wachttijd_s(self.db))
                alles = True
            except TimeoutError:
                alles = False
