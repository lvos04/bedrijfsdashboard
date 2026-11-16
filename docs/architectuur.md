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
* **Code**: `app/main.py` (app-fabriek, dashboard, datasets, tv), `app/routes_auth.py` (wizard, aanmelden, MFA,
  account), `app/routes_beheer.py` (beheer), `app/web.py` (gedeelde hulpfuncties), `app/koppeling.py` (eigen
  koppel-API), `app/ophalen.py`, `app/audit.py`, `app/mail.py`, `app/back_up.py`, `app/totp.py`,
  `app/instellingen.py` (alle instellingen met standaardwaarden).
* **Beveiliging**: argon2id-wachtwoordhashes (≥ 12 tekens), sessietoken in HTTP-only cookie (SameSite=Lax, Secure
  bij https), CSRF-token per sessie op alle muterende routes plus Origin/Referer-controle (optioneel strikt:
  zonder beide headers geweigerd), strikte Content-Security-Policy, optionele TOTP-MFA (geheim versleuteld met
  `geheim.key`, codes niet herbruikbaar) en optioneel *MFA verplicht voor beheerders*.
* **Aanmeldbeperking**: 5 mislukte pogingen per 15 minuten per gebruikersnaam + IP, en 20 per gebruikersnaam in
  totaal; nooit per IP alleen (achter een proxy delen gebruikers vaak één IP). De tellers staan in het geheugen
  van het enige proces (prima voor één container; bij meerdere processen naar de database verplaatsen).
* **Echt client-IP**: `X-Forwarded-For` wordt alleen gebruikt als de directe verbinding van een *vertrouwde proxy*
  komt (instelling of `VERTROUWDE_PROXIES`; standaard `privé` = 127.0.0.0/8, 10/8, 172.16/12, 192.168/16, link-local
  en IPv6-equivalenten). De lijst wordt van rechts naar links gelezen; het eerste niet-vertrouwde adres telt. Dat
  werkt direct (geen header) en achter één reverse proxy op het LAN/de TrueNAS-host. Staat de proxy elders (publiek
  IP), vul dan zijn adres in; `geen` negeert de header altijd.
* **Auditlog**: tabel `audit_log` (tijd, gebruiker, actie, details als JSON zonder geheimen, IP). Een dagelijkse
  opschoontaak in de ophaal-planner verwijdert verlopen sessies/MFA-uitdagingen en, alleen als er een bewaartermijn
  is ingesteld, oude auditregels.
* **Ophalen per bron**: de planner wordt wakker zodra de eerstvolgende bron aan de beurt is (eigen interval of het
  globale) en haalt alleen die bronnen op. Na elke ronde worden nieuwe kritieke meldingen geregistreerd
  (`gezien_meldingen`) en, als e-mail aan staat, één keer gemaild.

## Omgevingsvariabelen (allemaal optioneel)

| variabele | standaard | betekenis |
|---|---|---|
| `DATA_DIR` | `/data` | map voor database en sleutelbestand |
| `PORT` | `8080` | luisterpoort in de container |
| `COOKIE_SECURE` | `auto` | `auto` (Secure bij https / `X-Forwarded-Proto: https`), `true` of `false` |
| `ACHTERGROND_OPHALEN` | `1` | `0` schakelt de periodieke ophaaltaak uit |
| `LOG_LEVEL` | `INFO` | logniveau |
| `TZ` | (UTC) | tijdzone voor weergave van tijden, bijv. `Europe/Amsterdam` |
| `VERTROUWDE_PROXIES` | (instelling, standaard `privé`) | overschrijft *Beheer → Beveiliging → Vertrouwde proxy* |
| `FORWARDED_ALLOW_IPS` | (niet gezet) | alleen voor het oude gedrag: laat uvicorn zelf `X-Forwarded-*` toepassen |
