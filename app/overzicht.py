"""Samenstellen van het overzicht: tegels per bron en gebundelde meldingen."""

from __future__ import annotations

import json
from datetime import timedelta

from markupsafe import Markup, escape

from .db import Database
from .ophalen import KOPPELING_UIT, bron_interval, bron_verouderd_na, interval_minuten, verouderd_na_minuten
from .tijd import nu, parse_iso

NIVEAU_VOLGORDE = {"kritiek": 0, "waarschuwing": 1, "info": 2}
STATUS_LABEL = {"ok": "In orde", "let_op": "Let op", "actie": "Actie nodig", "onbekend": "Onbekend"}

BRON_KOLOMMEN = (
    "b.id, b.naam, b.app_slug, b.basis_url, b.sleutel_voorvoegsel, b.ca_pem IS NOT NULL AS heeft_ca, "
    "b.actief, b.aangemaakt_op, s.laatste_poging, s.laatste_succes, s.fout, s.fout_sinds, s.info_json, "
    "s.samenvatting_json, s.fout_code, b.interval_minuten, b.verouderd_na_minuten"
)


def _bron_model(rij, interval: int, verouderd_globaal: int | None = None) -> dict:
    info = json.loads(rij["info_json"]) if rij["info_json"] else None
    samenvatting = json.loads(rij["samenvatting_json"]) if rij["samenvatting_json"] else None
    laatste_succes = parse_iso(rij["laatste_succes"])
    te_oud = laatste_succes is None or nu() - laatste_succes > timedelta(
        minutes=bron_verouderd_na(rij, interval, verouderd_globaal)
    )
    heeft_fout = rij["fout"] is not None
    return {
        "id": rij["id"],
        "naam": rij["naam"],
        "app_slug": rij["app_slug"],
        "basis_url": rij["basis_url"],
        "sleutel_voorvoegsel": rij["sleutel_voorvoegsel"],
        "heeft_ca": bool(rij["heeft_ca"]),
        "actief": bool(rij["actief"]),
        "laatste_poging": rij["laatste_poging"],
        "laatste_succes": rij["laatste_succes"],
        "fout": rij["fout"],
        "fout_sinds": rij["fout_sinds"],
        "fout_code": rij["fout_code"],
        "koppeling_uit": rij["fout_code"] == KOPPELING_UIT,
        "interval_minuten": rij["interval_minuten"],
        "verouderd_na_minuten": rij["verouderd_na_minuten"],
        "effectief_interval": bron_interval(rij, interval),
        "effectief_verouderd_na": bron_verouderd_na(rij, interval, verouderd_globaal),
        "info": info,
        "samenvatting": samenvatting,
        "nooit_opgehaald": rij["laatste_poging"] is None,
        "bereikbaar": not heeft_fout and laatste_succes is not None,
        # Gegevens zijn verouderd als het laatste ophalen mislukte of te lang geleden is.
        "verouderd": samenvatting is not None and (heeft_fout or te_oud),
    }


def bronnen(db: Database, alleen_actief: bool = True) -> list[dict]:
    interval = interval_minuten(db)
    sql = f"SELECT {BRON_KOLOMMEN} FROM bronnen b LEFT JOIN bron_status s ON s.bron_id = b.id"
    if alleen_actief:
        sql += " WHERE b.actief = 1"
    sql += " ORDER BY b.naam COLLATE NOCASE"
    with db() as conn:
        rijen = conn.execute(sql).fetchall()
    verouderd = verouderd_na_minuten(db)
    return [_bron_model(r, interval, verouderd) for r in rijen]


def bron(db: Database, bron_id: int) -> dict | None:
    with db() as conn:
        rij = conn.execute(
            f"SELECT {BRON_KOLOMMEN} FROM bronnen b LEFT JOIN bron_status s ON s.bron_id = b.id WHERE b.id = ?",
            (bron_id,),
        ).fetchone()
    return _bron_model(rij, interval_minuten(db), verouderd_na_minuten(db)) if rij else None


def gebundelde_meldingen(lijst: list[dict]) -> list[dict]:
    """Alle meldingen van alle bronnen, kritiek eerst, daarbinnen nieuwste eerst."""
    uit = []
    for b in lijst:
        for m in (b["samenvatting"] or {}).get("meldingen", []):
            uit.append(
                {**m, "bron_id": b["id"], "bron_naam": b["naam"], "app": b["app_slug"], "verouderd": b["verouderd"]}
            )
    uit.sort(key=lambda m: m["sinds"] or "", reverse=True)
    uit.sort(key=lambda m: NIVEAU_VOLGORDE.get(m["niveau"], 9))
    return uit


def kpi_historie(db: Database, bron_id: int, dagen: int = 90) -> dict[str, list[tuple[str, float | None]]]:
    from .tijd import iso

    grens = iso(nu() - timedelta(days=dagen))
    uit: dict[str, list[tuple[str, float | None]]] = {}
    with db() as conn:
        for r in conn.execute(
            "SELECT sleutel, tijd, waarde FROM kpi_historie WHERE bron_id = ? AND tijd >= ? ORDER BY tijd",
            (bron_id, grens),
        ):
            uit.setdefault(r["sleutel"], []).append((r["tijd"], r["waarde"]))
    return uit


def sparkline(punten: list[tuple[str, float | None]], breedte: int = 160, hoogte: int = 36, label: str = "") -> Markup:
    """Kleine inline SVG; ontbrekende waarden onderbreken de lijn (er wordt niets geïnterpoleerd)."""
    waarden = [w for _, w in punten if w is not None]
    if len(waarden) < 2:
        return Markup('<span class="sparkline-leeg">Te weinig historie</span>')
    lo, hi = min(waarden), max(waarden)
    span = (hi - lo) or 1.0
    marge = 3
    n = len(punten)
    segmenten: list[list[str]] = [[]]
    laatste = None
    for i, (_, w) in enumerate(punten):
        if w is None:
            if segmenten[-1]:
                segmenten.append([])
            continue
        x = marge + (breedte - 2 * marge) * (i / (n - 1))
        y = hoogte - marge - (hoogte - 2 * marge) * ((w - lo) / span)
        segmenten[-1].append(f"{x:.1f},{y:.1f}")
        laatste = (x, y)
    lijnen = "".join(
        f'<polyline points="{" ".join(s)}" fill="none" stroke="currentColor" stroke-width="1.75" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        for s in segmenten
        if len(s) >= 2
    )
    punt = f'<circle cx="{laatste[0]:.1f}" cy="{laatste[1]:.1f}" r="2.5" fill="currentColor"/>' if laatste else ""
    titel = escape(f"{label}: min {_fmt(lo)}, max {_fmt(hi)}, {len(waarden)} metingen")
    return Markup(
        f'<svg class="sparkline" viewBox="0 0 {breedte} {hoogte}" width="{breedte}" height="{hoogte}" '
        f'role="img" aria-label="{titel}"><title>{titel}</title>{lijnen}{punt}</svg>'
    )


def _fmt(w: float | int | None) -> str:
    if w is None:
        return "–"
    if isinstance(w, float) and w.is_integer():
        w = int(w)
    if isinstance(w, int):
        return f"{w:,}".replace(",", ".")
    return f"{w:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").rstrip("0").rstrip(",")


formatteer_getal = _fmt
