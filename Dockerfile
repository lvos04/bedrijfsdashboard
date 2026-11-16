# syntax=docker/dockerfile:1
FROM python:3.12-slim AS basis

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Niet-root gebruiker met UID/GID 568 (de standaard 'apps'-gebruiker van TrueNAS SCALE),
# zodat een TrueNAS-dataset met rechten voor 'apps' direct schrijfbaar is.
RUN groupadd --system --gid 568 apps \
    && useradd --system --uid 568 --gid 568 --home-dir /app --shell /usr/sbin/nologin apps \
    && mkdir -p /data && chown 568:568 /data && chmod 0770 /data

USER 568:568
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/gezondheid' % os.environ.get('PORT','8080'), timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "app"]
