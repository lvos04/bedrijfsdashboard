"""Versleuteling van koppelsleutels van bronnen met Fernet.

De Fernet-sleutel wordt bij de eerste start gegenereerd in DATA_DIR/geheim.key (rechten 0600).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class Kluis:
    def __init__(self, pad: Path):
        self.pad = pad
        self._fernet = Fernet(self._laad_of_maak())

    def _laad_of_maak(self) -> bytes:
        if self.pad.exists():
            return self.pad.read_bytes().strip()
        sleutel = Fernet.generate_key()
        fd = os.open(self.pad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(sleutel)
        return sleutel

    def versleutel(self, tekst: str) -> bytes:
        return self._fernet.encrypt(tekst.encode("utf-8"))

    def ontsleutel(self, data: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(data)).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - alleen bij verwisseld sleutelbestand
            raise ValueError("Koppelsleutel kan niet worden ontsleuteld (is geheim.key vervangen?)") from exc
