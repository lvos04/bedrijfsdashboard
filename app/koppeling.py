"""Eigen Bedrijfskoppeling API v1 (slug 'bedrijfsdashboard'): geaggregeerd overzicht voor andere apps."""

from __future__ import annotations

import base64
import secrets
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__, instellingen, overzicht
from .beveiliging import sha256
from .tijd import iso, parse_iso

APP_SLUG = "bedrijfsdashboard"
DATASETS = ["bronnen", "kpis", "meldingen"]
LIMIET_PER_MINUUT = 120


# ---------------------------------------------------------------- sleutelbeheer


def maak_sleutel(conn, naam: str, door: str | None) -> tuple[int, str]:
    sleutel = "bk_" + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO koppelsleutels (naam, voorvoegsel, hash, aangemaakt_op, aangemaakt_door) VALUES (?, ?, ?, ?, ?)",
        (naam, sleutel[:8], sha256(sleutel), iso(), door),
    )
    return cur.lastrowid, sleutel


def lijst_sleutels(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, naam, voorvoegsel, aangemaakt_op, aangemaakt_door, laatst_gebruikt_op, ingetrokken_op "
            "FROM koppelsleutels ORDER BY id DESC"
        )
    ]


def trek_in(conn, sleutel_id: int) -> bool:
    cur = conn.execute(
        "UPDATE koppelsleutels SET ingetrokken_op = ? WHERE id = ? AND ingetrokken_op IS NULL", (iso(), sleutel_id)
    )
    return cur.rowcount > 0


class _Limiter:
    def __init__(self, per_minuut: int):
        self.per_minuut = per_minuut
        self._hits: dict[int, deque] = {}
        self._lock = threading.Lock()

    def toegestaan(self, sleutel_id: int) -> bool:
        t = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(sleutel_id, deque())
            while q and t - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minuut:
                return False
            q.append(t)
            return True


_ONGELDIG = {"fout": "ongeldige_koppelsleutel"}


_UITGESCHAKELD = {"fout": "koppeling_uitgeschakeld"}


def ingeschakeld(db) -> bool:
    return instellingen.aan(db, "koppeling_ingeschakeld")


def _authenticeer(request: Request) -> int | JSONResponse:
    # Staat de koppeling uit (standaard), dan weigeren we alles, ook met een geldige sleutel.
    if not ingeschakeld(request.app.state.db):
        return JSONResponse(_UITGESCHAKELD, status_code=403)
    kop = request.headers.get("authorization", "")
    soort, _, sleutel = kop.partition(" ")
    if soort.lower() != "bearer" or not sleutel.strip():
        return JSONResponse(_ONGELDIG, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    db = request.app.state.db
    with db() as conn:
        rij = conn.execute(
            "SELECT id FROM koppelsleutels WHERE hash = ? AND ingetrokken_op IS NULL", (sha256(sleutel.strip()),)
        ).fetchone()
        if rij is None:
            return JSONResponse(_ONGELDIG, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        conn.execute("UPDATE koppelsleutels SET laatst_gebruikt_op = ? WHERE id = ?", (iso(), rij["id"]))
    if not request.app.state.koppel_limiter.toegestaan(rij["id"]):
        return JSONResponse({"fout": "te_veel_verzoeken"}, status_code=429, headers={"Retry-After": "60"})
    return rij["id"]


# ---------------------------------------------------------------- gegevens


def samenvatting(db) -> dict:
    lijst = overzicht.bronnen(db)
    totaal = len(lijst)
    bereikbaar = sum(1 for b in lijst if b["bereikbaar"])
    meldingen = overzicht.gebundelde_meldingen(lijst)
    kritiek = sum(1 for m in meldingen if m["niveau"] == "kritiek")
    waarschuwing = sum(1 for m in meldingen if m["niveau"] == "waarschuwing")
    kpi_actie = sum(1 for b in lijst for k in (b["samenvatting"] or {}).get("kpis", []) if k["status"] == "actie")
    met_gegevens = any(b["samenvatting"] for b in lijst)

    def st(aantal: int, bij_meer: str) -> str:
        return "onbekend" if not met_gegevens else ("ok" if aantal == 0 else bij_meer)

    kpis = [
        {
            "sleutel": "bronnen_bereikbaar",
            "label": "Bronnen bereikbaar",
            "waarde": bereikbaar if totaal else None,
            "eenheid": None,
            "status": "onbekend"
            if not totaal
            else ("ok" if bereikbaar == totaal else ("actie" if bereikbaar == 0 else "let_op")),
            "toelichting": f"van {totaal} gekoppelde apps" if totaal else "nog geen bronnen gekoppeld",
        },
        {
            "sleutel": "kritieke_meldingen",
            "label": "Kritieke meldingen",
            "waarde": kritiek if met_gegevens else None,
            "eenheid": None,
            "status": st(kritiek, "actie"),
            "toelichting": None,
        },
        {
            "sleutel": "waarschuwingen",
            "label": "Waarschuwingen",
            "waarde": waarschuwing if met_gegevens else None,
            "eenheid": None,
            "status": st(waarschuwing, "let_op"),
            "toelichting": None,
        },
        {
            "sleutel": "kpis_actie",
            "label": "KPI's met actie nodig",
            "waarde": kpi_actie if met_gegevens else None,
            "eenheid": None,
            "status": st(kpi_actie, "actie"),
            "toelichting": None,
        },
    ]
    uit_meldingen = []
    for b in lijst:
        if b["fout"]:
            uit_meldingen.append(
                {
                    "niveau": "waarschuwing",
                    "tekst": f"{b['naam']}: niet bereikbaar ({b['fout']})",
                    "sinds": b["fout_sinds"],
                }
            )
    for m in meldingen:
        tekst = f"{m['bron_naam']}: {m['tekst']}" + (" (verouderd)" if m["verouderd"] else "")
        uit_meldingen.append({"niveau": m["niveau"], "tekst": tekst, "sinds": m["sinds"]})
    return {"app": APP_SLUG, "gegenereerd_op": iso(), "kpis": kpis, "meldingen": uit_meldingen}


def _dataset_items(db, naam: str) -> list[dict]:
    lijst = overzicht.bronnen(db)
    if naam == "bronnen":
        return [
            {
                "id": b["id"],
                "naam": b["naam"],
                "app": b["app_slug"],
                "bereikbaar": b["bereikbaar"],
                "verouderd": b["verouderd"],
                "laatste_succes": b["laatste_succes"],
                "fout": b["fout"],
                "bijgewerkt_op": b["laatste_poging"],
            }
            for b in lijst
        ]
    if naam == "kpis":
        items = []
        for b in lijst:
            for k in (b["samenvatting"] or {}).get("kpis", []):
                items.append(
                    {
                        "id": f"{b['id']}:{k['sleutel']}",
                        "bron_id": b["id"],
                        "app": b["app_slug"],
                        "sleutel": k["sleutel"],
                        "label": k["label"],
                        "waarde": k["waarde"],
                        "eenheid": k["eenheid"],
                        "status": k["status"],
                        "verouderd": b["verouderd"],
                        "bijgewerkt_op": b["laatste_succes"],
                    }
                )
        return items
    items = []
    for m in overzicht.gebundelde_meldingen(lijst):
        ident = sha256(f"{m['bron_id']}|{m['niveau']}|{m['tekst']}|{m['sinds']}")[:16]
        items.append(
            {
                "id": ident,
                "bron_id": m["bron_id"],
                "app": m["app"],
                "niveau": m["niveau"],
                "tekst": m["tekst"],
                "sinds": m["sinds"],
                "verouderd": m["verouderd"],
                "bijgewerkt_op": m["sinds"],
            }
        )
    return items


def _cursor_naar_offset(cursor: str | None) -> int | None:
    if not cursor:
        return 0
    try:
        waarde = base64.urlsafe_b64decode(cursor.encode() + b"==").decode()
        if not waarde.startswith("o:"):
            return None
        offset = int(waarde[2:])
        return offset if offset >= 0 else None
    except (ValueError, UnicodeDecodeError):
        return None


def _offset_naar_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


# ---------------------------------------------------------------- routes


def _vereis_ingeschakeld(request: Request) -> None:
    """Router-afhankelijkheid: draait vóór de validatie van queryparameters, zodat ook ongeldige verzoeken 403 geven."""
    if not ingeschakeld(request.app.state.db):
        raise HTTPException(403, _UITGESCHAKELD["fout"])


router = APIRouter(prefix="/api/koppeling/v1", dependencies=[Depends(_vereis_ingeschakeld)])


@router.get("/info")
def info(request: Request):
    auth = _authenticeer(request)
    if isinstance(auth, JSONResponse):
        return auth
    db = request.app.state.db
    return {
        "app": APP_SLUG,
        "naam": "Bedrijfsdashboard",
        "versie": __version__,
        "api_versie": "1",
        "bedrijf": db.instelling("bedrijfsnaam") or None,
        "datasets": DATASETS,
        "tijd": iso(),
    }


@router.get("/samenvatting")
def samenvatting_route(request: Request):
    auth = _authenticeer(request)
    if isinstance(auth, JSONResponse):
        return auth
    return samenvatting(request.app.state.db)


@router.get("/datasets/{naam}")
def dataset(
    request: Request,
    naam: str,
    sinds: str | None = Query(None),
    limiet: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
):
    auth = _authenticeer(request)
    if isinstance(auth, JSONResponse):
        return auth
    if naam not in DATASETS:
        return JSONResponse({"fout": "onbekende_dataset"}, status_code=404)
    sinds_dt = None
    if sinds is not None:
        sinds_dt = parse_iso(sinds)
        if sinds_dt is None:
            return JSONResponse({"fout": "ongeldige_parameter", "parameter": "sinds"}, status_code=422)
    offset = _cursor_naar_offset(cursor)
    if offset is None:
        return JSONResponse({"fout": "ongeldige_parameter", "parameter": "cursor"}, status_code=422)
    items = _dataset_items(request.app.state.db, naam)
    if sinds_dt is not None:
        items = [i for i in items if (d := parse_iso(i.get("bijgewerkt_op"))) is not None and d >= sinds_dt]
    items.sort(key=lambda i: i["id"])
    pagina = items[offset : offset + limiet]
    volgende = _offset_naar_cursor(offset + limiet) if offset + limiet < len(items) else None
    return {"app": APP_SLUG, "dataset": naam, "gegenereerd_op": iso(), "items": pagina, "volgende_cursor": volgende}
