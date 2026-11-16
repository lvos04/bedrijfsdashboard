"""Instellingen uit omgevingsvariabelen (allemaal optioneel)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    poort: int = 8080
    # "auto": Secure-vlag op de cookie alleen bij https (of X-Forwarded-Proto: https).
    cookie_secure: str = "auto"
    achtergrond: bool = True

    @property
    def db_pad(self) -> Path:
        return self.data_dir / "bedrijfsdashboard.db"

    @property
    def sleutel_pad(self) -> Path:
        return self.data_dir / "geheim.key"


def laad_config() -> Config:
    return Config(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        poort=int(os.environ.get("PORT", "8080")),
        cookie_secure=os.environ.get("COOKIE_SECURE", "auto").lower(),
        achtergrond=os.environ.get("ACHTERGROND_OPHALEN", "1") not in ("0", "false", "nee"),
    )
