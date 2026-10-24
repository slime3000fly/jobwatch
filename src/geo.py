"""Wspolrzedne miejscowosci + odleglosci. Geokoder: Nominatim (OpenStreetMap).

    python3 geo.py               # dociagnij wspolrzedne dla wszystkich ofert z bazy

Kazde zapytanie trafia do cache w bazie (takze nieudane), wiec Nominatim widzi
kazda miejscowosc raz na zawsze. Regulamin Nominatim: max 1 zapytanie/s i
wlasny User-Agent - pilnujemy obu.

Format pola city w ofertach:
  "Warszawa, Wola"                    -> miasto, dzielnica (bierzemy miasto)
  "Wola Wezykowa (pow. łaski)"        -> powiat idzie jako kontekst do geokodera
  "Sieradz; Łódź"                     -> kilka lokalizacji jednej oferty
"""
import json
import math
import re
import sys
import time

import requests

from common import connect, normalize_text

NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "JobWatch/1.0 (self-hosted job aggregator)"
REMOTE_WORDS = ("zdalnie", "zdalna", "remote", "cala polska", "home office")

_last_call = [0.0]


def places(city: str | None) -> list[str]:
    """'Warszawa, Wola; Kraków' -> ['Warszawa', 'Kraków'] (z kontekstem powiatu)."""
    out = []
    for part in re.split(r"[;|/]", city or ""):
        part = part.strip()
        if not part:
            continue
        m = re.search(r"\((?:pow\.|powiat)\s*([^)]+)\)", part)
        name = re.sub(r"\(.*?\)", "", part).split(",")[0].strip()
        if not name or any(w in name.lower() for w in REMOTE_WORDS):
            continue
        out.append(f"{name}, powiat {m.group(1).strip()}" if m else name)
    return out


def _key(place: str) -> str:
    return place.strip().lower()


def cached(conn, place):
    """(lat, lon) | None jesli nieznane | False jesli geokoder nie znalazl."""
    r = conn.execute("SELECT lat, lon FROM geo_cache WHERE place=?", (_key(place),)).fetchone()
    if r is None:
        return None
    return (r["lat"], r["lon"]) if r["lat"] is not None else False


def lookup(conn, place):
    """Z cache albo z Nominatim. Zwraca (lat, lon) albo None."""
    c = cached(conn, place)
    if c is not None:
        return c or None
    wait = 1.1 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    lat = lon = None
    try:
        r = requests.get(NOMINATIM, params={"q": place, "countrycodes": "pl",
                                            "format": "json", "limit": 1},
                         headers={"User-Agent": UA, "Accept-Language": "pl"}, timeout=20)
        r.raise_for_status()
        hits = r.json()
        if hits:
            lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
    except (requests.RequestException, ValueError) as e:
        print(f"[geo] {place}: {e}", file=sys.stderr)
        return None                       # blad sieci - nie cache'ujemy, sprobujemy jutro
    conn.execute("INSERT OR REPLACE INTO geo_cache (place, lat, lon) VALUES (?,?,?)",
                 (_key(place), lat, lon))
    conn.commit()
    return (lat, lon) if lat is not None else None


def km(a, b):
    """Odleglosc po kuli ziemskiej (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def main():
    conn = connect()
    profs = [json.loads(r["config"])
             for r in conn.execute("SELECT config FROM profiles WHERE active=1")]
    # Odleglosc liczymy tylko ofertom, ktore przejda filtr stanowiska (match.py) -
    # reszta i tak odpadnie, a kazda miejscowosc to sekunda czekania na Nominatim.
    # Profil bez stanowisk = bierze wszystko, wiec wtedy geokodujemy wszystko.
    titles = [normalize_text(t) for p in profs for t in p.get("must_titles") or []]
    take_all = not profs or any(not p.get("must_titles") for p in profs)
    todo = set()
    for r in conn.execute("SELECT DISTINCT title, city FROM offers WHERE city IS NOT NULL"):
        if take_all or any(t and t in normalize_text(r["title"]) for t in titles):
            todo.update(p for p in places(r["city"]) if cached(conn, p) is None)
    for p in profs:
        todo.update(c for c in p.get("cities") or [] if cached(conn, c) is None)
    print(f"[geo] do zgeokodowania: {len(todo)} miejscowosci (~{len(todo)}s)", file=sys.stderr)
    found = 0
    for i, p in enumerate(sorted(todo)):
        print(f"[progress] {i}/{len(todo)}", file=sys.stderr, flush=True)
        found += bool(lookup(conn, p))
    print(f"[geo] znalezione: {found}/{len(todo)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
