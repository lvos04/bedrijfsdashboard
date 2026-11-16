# Bedrijfsdashboard

Eén overzicht van alle bedrijfsapps van de kwekerij. Het dashboard haalt server-naar-server de kerncijfers (KPI's)
en meldingen op bij:

| app | slug |
|---|---|
| Teeltregistratie / milieu | `milieu-registratie` |
| Gewichtsverwachting | `gewichtsverwachting` |
| Scouten | `scouten` |
| TuinWerk OS | `tuinwerk-os` |
| Priva Bridge | `priva-bridge` |

Elke app die de [Bedrijfskoppeling API v1](docs/koppeling-api.md) aanbiedt kan als bron worden toegevoegd.

**Wat u ziet**

* Bovenaan alle **kritieke meldingen** van alle apps gebundeld, plus welke apps niet bereikbaar zijn.
* Per app een tegel met KPI's en een statuskleur (*In orde*, *Let op*, *Actie nodig*, *Onbekend*), het tijdstip van
  bijwerken en een link naar de app.
* Per app een detailpagina met trendlijnen (standaard 90 dagen historie) en een datasets-browser.
* Lukt het ophalen niet, dan ziet u "Niet bereikbaar sinds …" en blijven de laatst bekende gegevens zichtbaar,
  duidelijk gemarkeerd als **verouderd**. Er worden nooit waarden verzonnen.
* Werkt op telefoon en in donkere modus.

Het dashboard biedt zelf ook de koppel-API aan (slug `bedrijfsdashboard`), zodat andere apps het totaaloverzicht
kunnen gebruiken. Die staat standaard **uit** (zie hieronder).

Optioneel (alles standaard uit): MFA per gebruiker en *MFA verplicht voor beheerders*, e-mailmeldingen bij nieuwe
kritieke meldingen, een tv-weergave voor een scherm in de kantine, strikte oorsprongcontrole en een bewaartermijn
voor het auditlog. Wijzigingen per versie: [docs/changelog.md](docs/changelog.md).

## Installatie op TrueNAS SCALE (Custom App)

1. Maak een dataset voor de gegevens, bijvoorbeeld `pool/apps/bedrijfsdashboard`. Geef de gebruiker **apps**
   (UID/GID 568) lees- en schrijfrechten (*Datasets → Edit Permissions*, of kies bij het aanmaken het preset
   *Apps*).
2. Ga naar *Apps → Discover Apps → Custom App* en vul het formulier in:

| veld | waarde |
|---|---|
| Application Name | `bedrijfsdashboard` |
| Image → Repository | `ghcr.io/lvos04/bedrijfsdashboard` |
| Image → Tag | `latest` (of een versie, bijv. `0.2.0`) |
| Pull Policy | *Always pull image from registry* (alleen bij `latest`) |
| Container Environment Variables | niet nodig; optioneel `TZ` = `Europe/Amsterdam` |
| Network → Ports: Container Port | `8080` |
| Network → Ports: Node (host) Port | `30095` |
| Protocol | TCP |
| Storage → Host Path Volume: Host Path | `/mnt/pool/apps/bedrijfsdashboard` (de dataset uit stap 1) |
| Storage → Mount Path | `/data` |
| Security Context → Run As User / Group | `568` / `568` (standaard; mag ook een andere gebruiker met schrijfrechten op de dataset zijn) |
| Restart Policy | *Unless stopped* |

3. Klik *Install*. Open daarna `http://<truenas-ip>:30095`.
4. De **welkomstwizard** vraagt om de eerste beheerder (wachtwoord minimaal 12 tekens) en optioneel de bedrijfsnaam.

Als de container direct stopt met *"DATA_DIR '/data' is niet schrijfbaar"*, dan heeft de containergebruiker geen
schrijfrechten op de dataset: pas de rechten aan (stap 1) of stel *Run As User* in op de eigenaar van de dataset.

**Back-up**: maak een back-up van de hele dataset. Naast de database staat daar `geheim.key`, waarmee de
koppelsleutels versleuteld zijn. Zonder dat bestand moeten alle koppelsleutels opnieuw worden ingevoerd.

### Zonder TrueNAS (docker run)

```bash
docker run -d --name bedrijfsdashboard -p 30095:8080 -v /pad/naar/data:/data \
  ghcr.io/lvos04/bedrijfsdashboard:latest
```

## Apps koppelen

Voor elke app herhaalt u:

1. **In de app** (bijv. Scouten): meld u aan als beheerder, ga naar *Beheer → Koppelsleutels* en maak een sleutel
   met de naam `Bedrijfsdashboard`. Kopieer de sleutel (`bk_…`): hij wordt maar één keer getoond.
2. **In het dashboard**: *Beheer → Bron toevoegen*:
   * **Naam**: zoals u de app wilt zien, bijv. `Scouten`.
   * **App-slug**: kies uit de lijst (`milieu-registratie`, `gewichtsverwachting`, `scouten`, `tuinwerk-os`, `priva-bridge`)
     of vul de slug van een andere app in.
   * **Basis-URL**: het adres waarop u de app opent, zonder pad, bijv. `http://192.168.1.10:30092`.
     Het dashboard voegt zelf `/api/koppeling/v1` toe.
   * **Koppelsleutel**: de gekopieerde sleutel. Die wordt versleuteld opgeslagen en daarna nooit meer getoond
     (alleen de eerste 8 tekens).
   * **Eigen CA (PEM)**: alleen nodig als de app via https draait met een zelfondertekend certificaat of een eigen
     CA. Upload het CA-certificaat (of het zelfondertekende certificaat zelf). TLS-verificatie blijft altijd aan.
3. Klik bij de bron op **Verbinding testen**. Bij succes ziet u de naam en versie van de app; de gegevens worden
   meteen opgehaald.

Ziet u bij een bron **"Koppeling staat uit in …"**, dan is in die app de schakelaar *Bedrijfskoppeling ingeschakeld*
nog uit (de app antwoordt `403 koppeling_uitgeschakeld`). De melding noemt waar de schakelaar staat; zet hem aan en
klik opnieuw op **Verbinding testen**.

Het ophaalinterval (standaard 5 minuten), de bewaartermijn van de historie (standaard 90 dagen) en wanneer gegevens
als *verouderd* gelden (standaard 3 × het interval) stelt u in onder *Beheer → Instellingen*. Per bron kunt u bij
*Bewerken* een eigen interval en "verouderd na" opgeven (leeg = globaal). Met **Nu alles ophalen** forceert u een
ophaalronde.

### Dashboard als bron voor andere apps

Onder *Beheer → Koppelsleutels van dit dashboard* zet u eerst **Bedrijfskoppeling ingeschakeld** aan (standaard
uit; dan krijgt iedereen `403 koppeling_uitgeschakeld`) en maakt u een sleutel aan voor een app die het
totaaloverzicht wil ophalen. Zie [docs/koppeling-api.md](docs/koppeling-api.md) voor de KPI's en datasets.

## Optionele functies

| functie | waar | standaard |
|---|---|---|
| Eigen koppel-API | Beheer → Koppelsleutels → *Bedrijfskoppeling ingeschakeld* | uit |
| MFA (TOTP) per gebruiker | Account → Tweestapsverificatie | uit |
| MFA verplicht voor beheerders | Beheer → Beveiliging | uit |
| Strikte oorsprongcontrole (Origin/Referer verplicht) | Beheer → Beveiliging | uit |
| Vertrouwde proxy voor `X-Forwarded-For` | Beheer → Beveiliging of `VERTROUWDE_PROXIES` | `privé` |
| Bewaartermijn auditlog | Beheer → Beveiliging | 0 = onbeperkt |
| E-mailmeldingen bij nieuwe kritieke meldingen | Beheer → E-mailmeldingen | uit |
| Tv-weergave (alleen-lezen kiosk-URL) | Beheer → Tv-weergave | uit |
| Interval / "verouderd na" per bron | Beheer → bron bewerken | globaal |

**Tv-weergave**: zet de schakelaar aan, klik *Link maken* en open de getoonde link (`http://<server>:30095/tv/…`)
op het scherm. De pagina ververst elke minuut en toont KPI's en meldingen, zonder knoppen of interne adressen.
Iedereen met de link kan meekijken; maak een nieuwe link om de oude in te trekken.

**E-mailmeldingen**: vul SMTP-server, poort, beveiliging (STARTTLS/SSL/geen), eventueel gebruiker en wachtwoord,
afzender en ontvangers in en test met *Testmail versturen*. Er wordt alleen gemaild bij een **nieuwe kritieke**
melding, één keer per melding.

**Back-up van de configuratie**: onder *Beheer → Back-up* downloadt u bronnen (met koppelsleutels), instellingen en
de koppelsleutels van dit dashboard als één bestand, versleuteld met een herstelwachtwoord dat u zelf kiest. Daarmee
kunt u de configuratie op dezelfde of een nieuwe installatie terugzetten (bestaande bronnen worden bijgewerkt, niet
verwijderd). Gebruikers, auditlog en historie zitten er niet in; maak daarvoor een back-up van de hele dataset.

## Gebruikers en rollen

* **Beheerder**: alles, inclusief bronnen, instellingen, gebruikers en koppelsleutels.
* **Lezer**: alleen het overzicht, detailpagina's en datasets bekijken.

Gebruikers voegt u toe onder *Beheer → Gebruikers*; daar kan een beheerder ook het wachtwoord van een gebruiker
resetten (en zo nodig MFA uitschakelen, bijv. bij een verloren telefoon). Iedereen wijzigt zijn eigen wachtwoord en
MFA onder *Account*. Alle beheeracties staan in *Beheer → Auditlog*.

## Ontwikkelen

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
DATA_DIR=./data python -m app        # http://localhost:8080
```

Zie ook [docs/architectuur.md](docs/architectuur.md) (opbouw, beveiliging, omgevingsvariabelen).

## Releases

* Elke push en pull request: `ruff` (lint + format), tests en een `docker build` (`.github/workflows/ci.yml`).
* Een push naar `main` publiceert `ghcr.io/lvos04/bedrijfsdashboard:latest`; een tag `v1.2.3` publiceert ook
  `1.2.3` en `1.2` (`.github/workflows/release.yml`).
* Handmatig starten (*Actions → Release → Run workflow*) vanaf een andere branch publiceert een testimage met de
  tags `<versie>-rc` (bijv. `0.2.0-rc`), `branch-<branchnaam>` en `sha-<korte commit>`, zonder `latest` te
  overschrijven.

## Verbeterplan

Zie [docs/verbeterplan.md](docs/verbeterplan.md) voor het geprioriteerde verbeterplan van alle bedrijfsapps.
