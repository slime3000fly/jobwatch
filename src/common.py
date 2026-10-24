"""Wspolne funkcje: baza, normalizacja, hashowanie. Tylko stdlib."""
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

ROOT = Path(__file__).parent
# Dane (baza, surowe pliki) moga lezec poza kodem - w Dockerze to wolumen /data.
DATA_DIR = Path(os.environ.get("JOBWATCH_DATA") or ROOT)
DB_PATH = DATA_DIR / "jobwatch.db"
RAW_DIR = DATA_DIR / "raw"
QUARANTINE_DIR = DATA_DIR / "quarantine"
STATUS_PATH = DATA_DIR / "pipeline_status.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS offers (
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT,
    city          TEXT,
    salary_min    REAL,
    salary_max    REAL,
    currency      TEXT,
    contract      TEXT,
    description   TEXT,
    published_at  TEXT,
    work_mode     TEXT,                -- "zdalna", "hybrydowa", "zdalna, hybrydowa" albo NULL
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_hash ON offers(content_hash);
CREATE INDEX IF NOT EXISTS idx_offers_company ON offers(company);

-- jeden wiersz na (oferta, dzien) - stad liczymy ile dni ogloszenie wisi
CREATE TABLE IF NOT EXISTS sightings (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    seen_date   TEXT NOT NULL,
    PRIMARY KEY (source, external_id, seen_date)
);

-- === konta ===
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    pass_hash   TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_login  TEXT
);

-- === profile: jeden wiersz na szukane stanowisko (konto moze miec kilka) ===
CREATE TABLE IF NOT EXISTS profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL DEFAULT 0,
    name       TEXT NOT NULL,
    config     TEXT NOT NULL,          -- JSON: titles, cities, blacklist, min_salary
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- dopasowanie liczone OSOBNO dla kazdego profilu
CREATE TABLE IF NOT EXISTS match_results (
    profile_id  INTEGER NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    score       INTEGER NOT NULL,
    reasons     TEXT,
    scored_at   TEXT,
    PRIMARY KEY (profile_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_match_profile ON match_results(profile_id, score);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id);

-- co osoba kliknela
CREATE TABLE IF NOT EXISTS feedback (
    profile_id  INTEGER NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    status      TEXT NOT NULL,   -- nowa|zapisana|wyslana|rozmowa|odrzucona|cisza|nieciekawa
    note        TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (profile_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS scores (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    ghost_score  INTEGER,
    ghost_reasons TEXT,
    scored_at    TEXT,
    PRIMARY KEY (source, external_id)
);

-- co juz wyslalismy, zeby nie powtarzac
CREATE TABLE IF NOT EXISTS delivered (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

-- wspolrzedne miejscowosci z Nominatim; lat NULL = geokoder nie znalazl
CREATE TABLE IF NOT EXISTS geo_cache (
    place TEXT PRIMARY KEY,
    lat   REAL,
    lon   REAL
);

CREATE TABLE IF NOT EXISTS runs (
    run_at    TEXT NOT NULL,
    source    TEXT NOT NULL,
    n_records INTEGER NOT NULL,
    PRIMARY KEY (run_at, source)
);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # kilku uzytkownikow naraz
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Dokladanie kolumn do baz zalozonych wczesniej."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(offers)")}
    if "work_mode" not in cols:
        conn.execute("ALTER TABLE offers ADD COLUMN work_mode TEXT")
        rows = conn.execute("SELECT source, external_id, city, description FROM offers")
        conn.executemany(
            "UPDATE offers SET work_mode=? WHERE source=? AND external_id=?",
            [(work_mode(dict(r)), r["source"], r["external_id"]) for r in rows.fetchall()])
        conn.commit()


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize_text(s) -> str:
    """Tekst do porownan: bez ogonkow, bez interpunkcji, bez liczb, jedna spacja.

    Liczby lecą celowo - te same ogloszenia roznia sie ID rekrutacji czy data.
    """
    if not s:
        return ""
    s = strip_accents(str(s).lower())
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


REMOTE_TEXT = ("praca zdalna", "pracy zdalnej", "prace zdalna", "zdalny model",
               "zdalnie", "full remote", "fully remote", "remote work", "work remotely",
               "remote first", "remote position")
HYBRID_TEXT = ("hybryd", "hybrid")
MODES_ORDER = ("zdalna", "hybrydowa")


def work_mode(rec: dict) -> str | None:
    """Tryb pracy do wyswietlenia: 'zdalna', 'hybrydowa', 'zdalna, hybrydowa' albo None.

    Scraper moze podac jawne rec["work_modes"] (z danych portalu) - wtedy wierzymy
    jemu. Inaczej zgadujemy z miasta i opisu.
    """
    modes = rec.get("work_modes")
    if modes is None:
        modes = set()
        city = normalize_text(rec.get("city"))
        blob = normalize_text(rec.get("description"))
        if any(w in city for w in ("zdalnie", "zdalna", "remote")) or any(
                p in blob for p in REMOTE_TEXT):
            modes.add("zdalna")
        if any(p in blob for p in HYBRID_TEXT):
            modes.add("hybrydowa")
    return ", ".join(m for m in MODES_ORDER if m in modes) or None


def content_hash(rec: dict) -> str:
    """Odcisk TRESCI, nie ogloszenia.

    Ten sam hash pod innym external_id = repost. To jest caly mechanizm
    wykrywania widm, wiec nie dokladaj tu pol zmiennych (daty, ID, licznikow).
    """
    parts = [
        normalize_text(rec.get("title")),
        normalize_text(rec.get("company")),
        normalize_text(rec.get("description"))[:2000],
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def load_config(path=None) -> dict:
    path = Path(path or ROOT / "config.json")
    if not path.exists():
        path = ROOT / "config.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path):
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                yield lineno, line
