# Wijzigingen

## 0.2.1 — 16 november 2026

* Priva Bridge (`priva-bridge`) als bekende app; bij een uitgeschakelde koppeling wijst de uitleg naar
  *Beheer → Bedrijfskoppeling* in de Bridge.

## 0.2.0 — 16 november 2026

Alle nieuwe functies die het gedrag veranderen staan achter een instelling. Behalve de eigen koppel-API (zie
hieronder) blijft het bestaande gedrag na de update gelijk.

### Let op bij het bijwerken

* **De eigen koppel-API staat nu standaard uit.** Halen andere apps het totaaloverzicht van het dashboard op, zet dan
  *Beheer → Koppelsleutels van dit dashboard → Bedrijfskoppeling ingeschakeld* aan. Tot die tijd krijgen ze
  `403 {"fout": "koppeling_uitgeschakeld"}`. Bestaande sleutels blijven geldig.
* De database wordt bij het starten automatisch bijgewerkt (migratie 2: alleen nieuwe tabellen en kolommen).
* Uvicorn herschrijft het client-IP niet meer zelf uit `X-Forwarded-For`; de app doet dat aan de hand van de nieuwe
  instelling *Vertrouwde proxy* (standaard `privé`). Wie `FORWARDED_ALLOW_IPS` expliciet had gezet, houdt het oude
  gedrag.

### Nieuw

* **Uitgeschakelde koppeling bij een bron herkennen**: geeft een app `403 koppeling_uitgeschakeld`, dan toont het
  dashboard per bron *"Koppeling staat uit in &lt;app&gt;: zet hem aan onder …"* in plaats van een generieke fout.
* **Schakelaar voor de eigen koppel-API** (standaard uit).
* **Auditlog** (*Beheer → Auditlog*): aanmelden/afmelden (ook mislukte en geblokkeerde pogingen), bronbeheer,
  koppelsleutels, gebruikersbeheer, wachtwoord/MFA, instellingen, tv-link en back-up. Met paginering (50 per pagina),
  filter per actie en JSON (`GET /api/beheer/audit?pagina=&per_pagina=&actie=`). Wachtwoorden, sleutels en tokens
  worden nooit gelogd. Bewaartermijn instelbaar, **standaard onbeperkt**.
* **Account**: eigen wachtwoord wijzigen (andere sessies worden afgemeld).
* **Wachtwoord resetten door een beheerder**, optioneel ook MFA uitschakelen; alle sessies van die gebruiker vervallen.
* **TOTP-MFA per gebruiker** (optioneel) via *Account → MFA*, met elke authenticator-app. Codes kunnen niet
  hergebruikt worden. Instelling **"MFA verplicht voor beheerders"** (standaard uit); kan alleen aan als u zelf MFA
  heeft.
* **Strikte oorsprongcontrole** (standaard uit): weigert formulieren zonder `Origin`- én `Referer`-header.
* **Aanmeldbeperking per account + IP** (5 pogingen / 15 min) plus 20 per account in totaal, in plaats van per IP
  alleen: achter een proxy blokkeert één persoon niet langer iedereen. Instelbare **vertrouwde proxy** voor
  `X-Forwarded-For` (`privé`, `geen` of IP's/CIDR's; ook via `VERTROUWDE_PROXIES`).
* **Dagelijkse opschoontaak**: verlopen sessies en MFA-uitdagingen; het auditlog alleen als er een bewaartermijn is.
* **Ophaalinterval en "verouderd na" per bron** (optioneel; leeg = globaal). Globaal "verouderd na" instelbaar
  (leeg = 3 × interval, zoals voorheen). KPI-historie blijft instelbaar (standaard 90 dagen).
* **E-mailmeldingen** (optioneel, standaard uit): SMTP-instellingen, één mail per nieuwe kritieke melding
  (ontdubbeld; bij inschakelen geen mails over bestaande meldingen), testmail-knop, SMTP-wachtwoord versleuteld.
* **Tv-weergave** (optioneel, standaard uit): alleen-lezen kioskpagina `/tv/<token>` voor een scherm in de kantine of
  op kantoor, ververst elke minuut, zonder interne adressen. Het token wordt alleen als hash opgeslagen en één keer
  getoond; een nieuwe link trekt de oude in.
* **Back-up van de configuratie** (*Beheer → Back-up*): bronnen (met koppelsleutels), instellingen en eigen
  koppelsleutels als één bestand, versleuteld met een zelfgekozen herstelwachtwoord (scrypt + Fernet). Terugzetten
  werkt ook op een nieuwe installatie en verwijdert geen bestaande bronnen. Vereist bevestiging met het eigen
  wachtwoord.

### Onderhoud

* `ruff` (lint) en `ruff format` in CI; code geformatteerd.
* Webroutes opgesplitst in `app/web.py`, `app/routes_auth.py` en `app/routes_beheer.py`.
* Release-workflow: handmatig starten (`workflow_dispatch`) vanaf een andere branch dan `main` publiceert
  `<versie>-rc`, `branch-<naam>` en `sha-<kort>`, nooit `latest`.
* Indexen op `sessies` en `audit_log`.

## 0.1.0

* Eerste versie: overzicht van alle bedrijfsapps via de Bedrijfskoppeling API v1, bronbeheer met versleutelde
  koppelsleutels, KPI-historie, datasets-browser en eigen koppel-API.
