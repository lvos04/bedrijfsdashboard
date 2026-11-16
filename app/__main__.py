"""Start de server: python -m app"""

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Het echte client-IP (voor de aanmeldbeperking en het auditlog) bepaalt de app zelf aan de hand van de
    # instelling "Vertrouwde proxy" (zie docs/architectuur.md). Uvicorn herschrijft het IP daarom alleen als
    # FORWARDED_ALLOW_IPS expliciet is gezet (oud gedrag).
    forwarded = os.environ.get("FORWARDED_ALLOW_IPS")
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=bool(forwarded),
        forwarded_allow_ips=forwarded or None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
