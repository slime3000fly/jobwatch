# syntax=docker/dockerfile:1

# ---------- baza: aplikacja web + lekkie scrapery ----------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw

# sqlite3 do backupow, tzdata zeby cron chodzil wg polskiego czasu
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 jobwatch

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Kod nalezy do roota i jest tylko do odczytu - aplikacja pisze wylacznie do /data.
COPY . .
RUN chmod 0755 run-daily.sh daily.sh

# Dane trzymamy w /data (wolumen), nie w obrazie.
RUN mkdir -p /data/raw /data/quarantine && chown -R jobwatch:jobwatch /data
ENV JOBWATCH_DATA=/data

USER jobwatch
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login',timeout=4).status==200 else 1)"

# --timeout 120: zapis profilu przelicza dopasowanie od razu i przy duzej bazie
# potrafi przekroczyc domyslne 30 s, po ktorych gunicorn zabija workera.
CMD ["gunicorn", "--chdir", "/app/src", "-w", "2", "--timeout", "120", \
     "-b", "0.0.0.0:8000", "--access-logfile", "-", "app:app"]


# ---------- wariant ze scraperem przegladarkowym ----------
# Buduj tylko jesli probe_pracuj.py pokazal, ze cloudscraper nie wystarcza.
# Obraz rosnie z ~200 MB do ~1,5 GB, wiec nie rob tego "na wszelki wypadek".
FROM base AS playwright

USER root
# Sciezka przegladarek ustawiona PRZED instalacja - inaczej Chromium laduje w
# /root/.cache, niedostepnym dla uzytkownika jobwatch (i pobiera sie dwa razy).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install playwright==1.47.0 \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

USER jobwatch
HEALTHCHECK NONE
CMD ["/app/run-daily.sh"]
