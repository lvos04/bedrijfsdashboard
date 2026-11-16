# Verbeterplan bedrijfsapps

Stand: 16 november 2026. Gebaseerd op een codereview van elke app tijdens het bouwen van de Bedrijfskoppeling API v1.
Apps: Teeltregistratie / Milieu-registratie (GWB), Gewichtsverwachting, Scouten, TuinWerk OS, en het nieuwe
Bedrijfsdashboard. (*Onderhoud* is bewust buiten scope gelaten.)

## Fase 0 — Eerst samenvoegen (deze week)

| # | Actie | App |
|---|---|---|
| 0.1 | Branch `ccr-f5cf979e-9oiocr` (koppel-API + MFA-fix) samenvoegen met `ccr-35a27342-80qcyj` (GWB-werk, papiermaat). Migratieketen: 0003 → 0004_koppelsleutels. | Milieu |
| 0.2 | Koppel-API-branches reviewen en mergen; nieuwe images bouwen. | alle |
| 0.3 | In elke app een koppelsleutel "Bedrijfsdashboard" aanmaken en in het dashboard invoeren. | alle |

## Fase 1 — Veiligheid (hoogste prioriteit, 1–2 weken)

Terugkerend patroon: dezelfde zwakke plekken komen in meerdere apps voor. Los ze één keer goed op en kopieer de aanpak.

| # | Probleem | Waar | Gevolg | Oplossing |
|---|---|---|---|---|
| 1.1 | Aanmeldbeperking per IP terwijl alles achter proxy/tunnel 127.0.0.1 of één IP is | TuinWerk (`auth.ts login`, geen `trust proxy`), Gewicht (`auth.py login`) | Eén persoon met foute wachtwoorden blokkeert **iedereen** | Vertrouwde proxy instellen voor `X-Forwarded-For`; beperken per account + per echt IP met vertraging |
| 1.2 | MFA niet verplicht voor beheerders | TuinWerk (`mfa.ts`), Milieu, Scouten | Beheerder kan back-ups terugzetten, alles exporteren, koppelsleutels maken | Instelling "MFA verplicht voor beheerders" + herbevestiging bij gevoelige acties |
| 1.3 | Sessietoken in `sessionStorage` als Bearer | Scouten (`apps/web/app/page.tsx`) | Elke XSS = accountovername | HttpOnly SameSite-cookie + CSRF, CSP-header in nginx |
| 1.4 | Uitloggen maakt token niet ongeldig (8 uur geldig) | Milieu | Gestolen cookie blijft bruikbaar | Sessieversie per gebruiker, ophogen bij uitloggen/wachtwoordwijziging; weiger start met standaardsleutel `dev-only-change-me` |
| 1.5 | Auditlog leesbaar met gewoon leesrecht | Milieu (`GET /audit`) | Uitvoerders zien alle voor/na-gegevens | Bestaand recht `audit.read` gebruiken |
| 1.6 | Oorsprongcontrole alleen als `Origin` aanwezig is | Gewicht (`validate_origin`) | Leunt volledig op SameSite | Terugvallen op `Referer` of eigen header vereisen |
| 1.7 | Body-limiet 9 MB op alle routes | TuinWerk (`main.ts`) | Makkelijk geheugen vol te sturen | Kleine standaard, groter alleen voor uploads |
| 1.8 | Tar-uitpakken zonder `filter="data"` | Milieu (`system_routes.py` herstel) | Path-traversal bij kwaadaardig back-upbestand | `filter="data"` toevoegen |

## Fase 2 — Betrouwbaarheid en gegevensintegriteit (2–4 weken)

| # | Probleem | Waar | Oplossing |
|---|---|---|---|
| 2.1 | Auditketen kan splitsen bij gelijktijdige schrijfacties | Milieu (`services/audit.py record`) | `pg_advisory_xact_lock` vóór het lezen van de vorige hash |
| 2.2 | Afhankelijkheden niet vastgezet (veroorzaakte kapotte MFA door pyotp 2.12) | Milieu (`pyproject.toml`) | Lockfile (uv / pip-tools) en image daarop bouwen |
| 2.3 | Ongevalideerde `dict`-invoer → 500-fouten | Milieu (`data_routes.py`, `PUT /settings/{key}`), Scouten (`mfa_confirm`, `create_site`, `register_device`, `resolve_conflict`, `correct_round`) | Pydantic-modellen; whitelist voor instellingen |
| 2.4 | Elke `ValueError` wordt 409 | Milieu (`main.py`) | Domeinfout-klasse → 409, invoerfout → 422 |
| 2.5 | Oogstgewicht niet corrigeerbaar (alleen nieuwe rij) | Gewicht (`add_harvest`) | Correctie met reden en audit, zoals bij planning |
| 2.6 | Back-ups onbeperkt bewaard op dezelfde dataset | Gewicht (`backup_keep_days=0`) | Standaard 30 dagen, waarschuwing over offsite-kopie, schijfruimte tonen |
| 2.7 | Verlopen sessies/aanmeldpogingen nooit opgeruimd; auditlog groeit onbeperkt | TuinWerk, Gewicht, Scouten | Dagelijkse opschoontaak in bestaande worker; paginering op `/audit` |
| 2.8 | Rapporttabellen zonder foreign keys | Scouten (`report_models.py`) | FK's toevoegen via migratie |
| 2.9 | Gebruiker met meerdere organisaties krijgt stil de eerste | Scouten (`auth.current_user`) | Organisatie expliciet vereisen |
| 2.10 | `updated_at` ontbreekt (sync en `sinds` niet waterdicht) | Scouten (`scout_rounds`, `reports`), TuinWerk (`inventory_balances`), Milieu (meststoffen/milieu) | Kolom + index toevoegen |
| 2.11 | Dubbele migratienummers 008; geen checksumcontrole op toegepaste migraties | TuinWerk (`migrate.ts`) | Hernummeren en checksum opslaan |

## Fase 3 — Performance (na fase 1–2, wanneer data groeit)

| # | Probleem | Waar | Oplossing |
|---|---|---|---|
| 3.1 | Alles in geheugen laden en in code tellen | TuinWerk (`reports/summary`), Gewicht (`dashboard_data`) | Aggregeren in SQL |
| 3.2 | N+1-queries | Milieu (`inventory.py`, `gwb_summary`), Gewicht (`measurement_obj`), Scouten (`coverage()` per ronde) | Gegroepeerde queries |
| 3.3 | Elke minuut zware herberekening | Gewicht (`refresh_due_forecasts`) | Alleen herberekenen na wijziging (dirty-vlag) |
| 3.4 | Ontbrekende indexen | TuinWerk (`time_entries`, `sessions`, `tasks.category`), Scouten (`observations(location_id, method_id)`) | Migratie met indexen |
| 3.5 | Volledige sync elke keer | Scouten (`/sync/pull full_refresh`) | Incrementeel op `updated_at` (na 2.10) |
| 3.6 | Frontend-bundel 487 kB in één stuk | Milieu | `React.lazy` per pagina (sneller op kas-tablets) |

## Fase 4 — Onderhoudbaarheid (doorlopend)

| # | Probleem | Waar | Oplossing |
|---|---|---|---|
| 4.1 | Zeer grote of extreem compacte bestanden | TuinWerk (`page.tsx` 3.600 r.), Gewicht (`core.py`, `app.js` ~2.900 r.), Scouten (`manage.tsx` ~30 KB/regel) | Opsplitsen per scherm/module |
| 4.2 | Geen lint/formatter in CI | alle | ruff + ruff-format (Python), ESLint + Prettier (TS) in CI |
| 4.3 | Versie op meerdere plaatsen | Gewicht, TuinWerk (Swagger "0.2.0") | Eén bron (`importlib.metadata` / `APP_VERSION`) |
| 4.4 | Handmatige `esc()` als enige XSS-bescherming | Gewicht (`app.js`) | Template-helper die standaard escapet + CSP |
| 4.5 | In-memory limiters/locks per proces | TuinWerk, Scouten, Gewicht | Prima bij één proces; documenteren of naar DB verplaatsen bij schalen |

## Fase 5 — Samenhang tussen de apps

1. **Gedeelde basis**: de vier apps hebben elk een eigen implementatie van login, MFA, back-up/OneDrive, audit en koppelsleutels. Overweeg één gedeelde bibliotheek per taal (Python: Milieu/Gewicht/Scouten; TypeScript: TuinWerk), of op termijn single sign-on (één login via het Bedrijfsdashboard of een OIDC-provider zoals Authentik op de TrueNAS).
2. **Stamgegevens delen**: kassen/afdelingen/kappen, middelen en medewerkers komen in meerdere apps terug. Een gedeelde lijst (bijv. via de koppel-API: dataset `locaties`) voorkomt dubbele invoer — bijvoorbeeld scoutingwaarnemingen (Scouten) koppelen aan spuitplanning (Milieu), en storingen aan klimaat/gewicht (TuinWerk ↔ Gewicht).
3. **Koppel-API v2 (later)**: webhooks/gebeurtenissen (bijv. "drempel overschreden" in Scouten → voorstel spuitplanning in Milieu), en schrijfbare endpoints alleen met expliciete scopes per sleutel.
4. **Eén release-aanpak**: overal dezelfde GHCR-workflow, versienummering en TrueNAS-installatiehandleiding met poorttabel (Milieu 8443, Gewicht 30800→8000, Scouten 8090, TuinWerk 30091, Dashboard 30095).

## Volgorde in het kort

1. Merges en koppelsleutels (fase 0).
2. 1.1 aanmeldblokkade, 1.2 MFA beheerders, 1.3 tokenopslag Scouten, 2.1 auditketen, 2.2 lockfile.
3. Opschoontaken en invoervalidatie (2.3, 2.7).
4. Performance zodra seizoensdata groeit.
5. Lint/opsplitsing doorlopend, bij elke wijziging in dat bestand.
