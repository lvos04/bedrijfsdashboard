from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

from .nepbron import GELDIGE_SLEUTEL, maak_nepbron

WACHTWOORD = "correct-paard-batterij"


def csrf_uit(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "geen csrf_token in pagina"
    return m.group(1)


@pytest.fixture
def app(tmp_path):
    return create_app(Config(data_dir=tmp_path / "data", achtergrond=False))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def wizard(client, naam="beheer", ww=WACHTWOORD):
    token = csrf_uit(client.get("/welkom").text)
    return client.post("/welkom", data={"csrf_token": token, "gebruikersnaam": naam, "wachtwoord": ww,
                                        "wachtwoord2": ww, "bedrijfsnaam": "Testkwekerij"},
                       follow_redirects=False)


@pytest.fixture
def beheerder(client):
    r = wizard(client)
    assert r.status_code == 303
    return client


def csrf(client) -> str:
    return csrf_uit(client.get("/beheer").text)


@pytest.fixture
def nepbron():
    return maak_nepbron()


@pytest.fixture
def met_nepbron(app, nepbron):
    app.state.http_transport = httpx.ASGITransport(app=nepbron)
    return nepbron


def voeg_bron_toe(client, sleutel=GELDIGE_SLEUTEL, url="http://scouten.lan:30092", **extra):
    data = {"csrf_token": csrf(client), "naam": "Scouten", "app_slug": "scouten", "basis_url": url,
            "koppelsleutel": sleutel, **extra}
    return client.post("/beheer/bronnen", data=data, follow_redirects=False)
