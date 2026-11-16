# Bedrijfsdashboard

Eén overzicht van alle bedrijfsapps van de kwekerij. Het dashboard haalt server-naar-server de kerncijfers (KPI's)
en meldingen op bij:

| app | slug |
|---|---|
| Teeltregistratie / milieu | `milieu-registratie` |
| Gewichtsverwachting | `gewichtsverwachting` |
| Scouten | `scouten` |
| TuinWerk OS | `tuinwerk-os` |

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
kunnen gebruiken.

## Installatie op TrueNAS SCALE (Custom App)

1. Maak een dataset voor de gegevens, bijvoorbeeld `pool/apps/bedrijfsdashboard`. Geef de gebruiker **apps**
   (UID/GID 568) lees- en schrijfrechten (*Datasets → Edit Permissions*, of kies bij het aanmaken het preset
   *Apps*).
2. Ga naar *Apps → Discover Apps → Custom App* en vul het formulier in:

| veld | waarde |
|---|---|
| Application Name | `bedrijfsdashboard` |
| Image → Repository | `ghcr.io/lvos04/bedrijfsdashboard` |
| Image → Tag | `latest` (of een versie, bijv. `0.1.0`) |
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
   * **App-slug**: kies uit de lijst (`milieu-registratie`, `gewichtsverwachting`, `scouten`, `tuinwerk-os`)
     of vul de slug van een andere app in.
   * **Basis-URL**: het adres waarop u de app opent, zonder pad, bijv. `http://192.168.1.10:30092`.
     Het dashboard voegt zelf `/api/koppeling/v1` toe.
   * **Koppelsleutel**: de gekopieerde sleutel. Die wordt versleuteld opgeslagen en daarna nooit meer getoond
     (alleen de eerste 8 tekens).
   * **Eigen CA (PEM)**: alleen nodig als de app via https draait met een zelfondertekend certificaat of een eigen
     CA. Upload het CA-certificaat (of het zelfondertekende certificaat zelf). TLS-verificatie blijft altijd aan.
3. Klik bij de bron op **Verbinding testen**. Bij succes ziet u de naam en versie van de app; de gegevens worden
   meteen opgehaald.

Het ophaalinterval (standaard 5 minuten) en de bewaartermijn van de historie (standaard 90 dagen) stelt u in onder
*Beheer → Instellingen*. Met **Nu alles ophalen** forceert u een ophaalronde.

### Dashboard als bron voor andere apps

Onder *Beheer → Koppelsleutels van dit dashboard* maakt u een sleutel aan voor een app die het totaaloverzicht wil
ophalen. Zie [docs/koppeling-api.md](docs/koppeling-api.md) voor de KPI's en datasets.

## Gebruikers en rollen

* **Beheerder**: alles, inclusief bronnen, instellingen, gebruikers en koppelsleutels.
* **Lezer**: alleen het overzicht, detailpagina's en datasets bekijken.

Gebruikers voegt u toe onder *Beheer → Gebruikers*.

## Ontwikkelen

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
DATA_DIR=./data python -m app        # http://localhost:8080
```

Zie ook [docs/architectuur.md](docs/architectuur.md) (opbouw, beveiliging, omgevingsvariabelen).

## Releases

* Elke push en pull request: tests en een `docker build` (`.github/workflows/ci.yml`).
* Een push naar `main` publiceert `ghcr.io/lvos04/bedrijfsdashboard:latest`; een tag `v1.2.3` publiceert ook
  `1.2.3` en `1.2` (`.github/workflows/release.yml`).

## Verbeterplan

Zie [docs/verbeterplan.md](docs/verbeterplan.md) voor het geprioriteerde verbeterplan van alle bedrijfsapps.
