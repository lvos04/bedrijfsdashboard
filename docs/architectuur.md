# Architectuur

* **Eén proces** (uvicorn + FastAPI) op poort 8080. Server-gerenderde Jinja2-pagina's met een paar regels
  vanilla JavaScript (bevestigen, kopiëren); alles werkt ook zonder JavaScript. Geen build-keten.
* **Opslag**: SQLite in `DATA_DIR` (`/data/bedrijfsdashboard.db`, WAL-modus). Migraties staan als genummerde
  SQL-lijst in `app/db.py` en worden bij het starten toegepast (`PRAGMA user_version`).
* **Geheimen**: `DATA_DIR/geheim.key` (Fernet, rechten 0600) wordt bij de eerste start gegenereerd. Koppelsleutels
  van bronnen worden daarmee versleuteld; de UI ziet alleen het voorvoegsel (eerste 8 tekens). **Maak een back-up
  van de hele `/data`-map**: zonder `geheim.key` moeten alle koppelsleutels opnieuw ingevoerd worden.
* **Ophalen** (`app/ophalen.py`): een asyncio-achtergrondtaak haalt elke N minuten (instelbaar, standaard 5) per
  actieve bron `/info` en `/samenvatting` op met httpx (time-out 10 s, TLS-verificatie altijd aan, optioneel een
  eigen CA per bron). Het resultaat, het tijdstip en de foutmelding worden gecachet in `bron_status`; KPI-waarden
  gaan naar `kpi_historie` (standaard 90 dagen bewaard). Bij een fout blijven de laatste gegevens staan en worden
  ze als *verouderd* getoond met "niet bereikbaar sinds …". Waarden die geen getal zijn worden nooit tot 0
  omgezet.
* **Datasets-proxy**: `/api/bronnen/{id}/datasets/{naam}` en de HTML-pagina `/bronnen/{id}/datasets/{naam}`
  halen op verzoek gegevens op bij de bron; de koppelsleutel verlaat de server nooit.
* **Eigen koppel-API**: `/api/koppeling/v1/*` met eigen koppelsleutels (alleen SHA-256-hash opgeslagen), zie
  [koppeling-api.md](koppeling-api.md).
* **Beveiliging**: argon2id-wachtwoordhashes (≥ 12 tekens), sessietoken in HTTP-only cookie (SameSite=Lax, Secure
  bij https), CSRF-token per sessie op alle muterende routes plus Origin/Referer-controle, aanmeldbeperking
  (5 mislukte pogingen per 15 minuten per IP en per gebruikersnaam), strikte Content-Security-Policy.

## Omgevingsvariabelen (allemaal optioneel)

| variabele | standaard | betekenis |
|---|---|---|
| `DATA_DIR` | `/data` | map voor database en sleutelbestand |
| `PORT` | `8080` | luisterpoort in de container |
| `COOKIE_SECURE` | `auto` | `auto` (Secure bij https / `X-Forwarded-Proto: https`), `true` of `false` |
| `ACHTERGROND_OPHALEN` | `1` | `0` schakelt de periodieke ophaaltaak uit |
| `LOG_LEVEL` | `INFO` | logniveau |
| `TZ` | (UTC) | tijdzone voor weergave van tijden, bijv. `Europe/Amsterdam` |
