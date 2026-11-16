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


class OphaalFout(Exception):
    def __init__(self, tekst: str, status_code: int | None = None, data: Any = None):
        super().__init__(tekst)
        self.tekst = tekst
        self.status_code = status_code
        self.data = data


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
        kpis.append({
            "sleutel": _tekst(k.get("sleutel"), 100),
            "label": _tekst(k.get("label") or k.get("sleutel"), 200),
            "waarde": num,
            # Niet-numerieke tekstwaarde tonen we letterlijk, maar slaan we niet op in de historie.
            "waarde_tekst": None if num is not None or ruwe is None else _tekst(ruwe, 100),
            "eenheid": _tekst(k.get("eenheid"), 30),
            "status": k.get("status") if k.get("status") in STATUSSEN else "onbekend",
            "toelichting": _tekst(k.get("toelichting")),
        })
    meldingen = []
    for m in data.get("meldingen") or []:
        if not isinstance(m, dict) or not m.get("tekst"):
            continue
        meldingen.append({
            "niveau": m.get("niveau") if m.get("niveau") in NIVEAUS else "info",
            "tekst": _tekst(m.get("tekst")),
            "sinds": m.get("sinds") if parse_iso(m.get("sinds")) else None,
        })
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

def maak_client(bron, kluis: Kluis, transport: httpx.AsyncBaseTransport | None = None,
                timeout: float = STANDAARD_TIMEOUT) -> httpx.AsyncClient:
    sleutel = kluis.ontsleutel(bron["sleutel_versleuteld"])
    verify: Any = True
    if bron["ca_pem"]:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=bron["ca_pem"])
        verify = ctx
    return httpx.AsyncClient(
        base_url=bron["basis_url"].rstrip("/") + API_PREFIX,
        headers={"Authorization": f"Bearer {sleutel}", "Accept": "application/json",
                 "User-Agent": "Bedrijfsdashboard"},
        timeout=timeout,
        verify=verify,
        transport=transport,
        follow_redirects=False,
    )


async def _get_json(client: httpx.AsyncClient, pad: str, params: dict | None = None) -> Any:
    try:
        resp = await client.get(pad, params=params)
    except httpx.TimeoutException:
        raise OphaalFout(f"Time-out: geen antwoord binnen {client.timeout.read or STANDAARD_TIMEOUT:.0f} s")
    except httpx.ConnectError as exc:
        tekst = str(exc)
        if "CERTIFICATE" in tekst.upper() or "SSL" in tekst.upper():
            raise OphaalFout("TLS-certificaat niet vertrouwd (stel eventueel een eigen CA (PEM) in)")
        raise OphaalFout(f"Verbinding mislukt: {tekst or exc.__class__.__name__}")
    except httpx.HTTPError as exc:
        raise OphaalFout(f"Verbindingsfout: {exc.__class__.__name__}")
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
    if resp.status_code == 422:
        raise OphaalFout("Ongeldige parameters (422)", 422, data)
    if resp.status_code >= 400:
        raise OphaalFout(f"Bron gaf HTTP {resp.status_code}", resp.status_code, data)
    if data is None:
        raise OphaalFout("Ongeldig antwoord: geen JSON")
    return data


async def test_verbinding(bron, kluis: Kluis, transport=None) -> dict:
    async with maak_client(bron, kluis, transport) as client:
        return normaliseer_info(await _get_json(client, "/info"))


async def haal_dataset(bron, kluis: Kluis, naam: str, params: dict, transport=None) -> dict:
    if not DATASET_NAAM.match(naam):
        raise OphaalFout("Ongeldige datasetnaam", 422)
    async with maak_client(bron, kluis, transport) as client:
        data = await _get_json(client, f"/datasets/{naam}", params=params)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise OphaalFout("Ongeldig antwoord van dataset")
    return data


# ---------------------------------------------------------------- ophalen + opslaan

async def haal_bron_op(db: Database, kluis: Kluis, bron, transport=None,
                       timeout: float = STANDAARD_TIMEOUT) -> str | None:
    """Haalt info+samenvatting op voor één bron en slaat het resultaat op. Geeft foutmelding of None."""
    tijdstip = iso()
    try:
        async with maak_client(bron, kluis, transport, timeout) as client:
            info = normaliseer_info(await _get_json(client, "/info"))
            samenvatting = normaliseer_samenvatting(await _get_json(client, "/samenvatting"))
    except OphaalFout as exc:
        _sla_fout_op(db, bron["id"], tijdstip, exc.tekst)
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
            "fout_sinds = NULL, info_json = excluded.info_json, samenvatting_json = excluded.samenvatting_json",
            (bron["id"], tijdstip, tijdstip, json.dumps(info), json.dumps(samenvatting)),
        )
        for kpi in samenvatting["kpis"]:
            conn.execute(
                "INSERT INTO kpi_historie (bron_id, sleutel, tijd, waarde, status) VALUES (?, ?, ?, ?, ?)",
                (bron["id"], kpi["sleutel"], tijdstip,
                 float(kpi["waarde"]) if kpi["waarde"] is not None else None, kpi["status"]),
            )
    return None


def _sla_fout_op(db: Database, bron_id: int, tijdstip: str, tekst: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO bron_status (bron_id, laatste_poging, fout, fout_sinds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bron_id) DO UPDATE SET laatste_poging = excluded.laatste_poging, fout = excluded.fout, "
            "fout_sinds = COALESCE(bron_status.fout_sinds, excluded.fout_sinds)",
            (bron_id, tijdstip, tekst, tijdstip),
        )


def interval_minuten(db: Database) -> int:
    try:
        return max(1, min(1440, int(db.instelling("interval_minuten", str(STANDAARD_INTERVAL_MIN)))))
    except (TypeError, ValueError):
        return STANDAARD_INTERVAL_MIN


def historie_dagen(db: Database) -> int:
    try:
        return max(1, min(3650, int(db.instelling("historie_dagen", str(STANDAARD_HISTORIE_DAGEN)))))
    except (TypeError, ValueError):
        return STANDAARD_HISTORIE_DAGEN


def ruim_historie_op(db: Database) -> None:
    grens = iso(nu() - timedelta(days=historie_dagen(db)))
    with db() as conn:
        conn.execute("DELETE FROM kpi_historie WHERE tijd < ?", (grens,))


async def haal_alle_op(db: Database, kluis: Kluis, transport=None) -> dict[int, str | None]:
    with db() as conn:
        bronnen = conn.execute("SELECT * FROM bronnen WHERE actief = 1").fetchall()
    resultaten = await asyncio.gather(*(haal_bron_op(db, kluis, b, transport) for b in bronnen))
    ruim_historie_op(db)
    return {b["id"]: r for b, r in zip(bronnen, resultaten)}


class Planner:
    """Achtergrondtaak die elke N minuten alle bronnen ophaalt."""

    def __init__(self, db: Database, kluis: Kluis):
        self.db = db
        self.kluis = kluis
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
        while True:
            try:
                await haal_alle_op(self.db, self.kluis)
            except Exception:  # pragma: no cover
                log.exception("Fout in ophaalronde")
            self.nu_ophalen.clear()
            try:
                await asyncio.wait_for(self.nu_ophalen.wait(), timeout=interval_minuten(self.db) * 60)
            except asyncio.TimeoutError:
                pass
