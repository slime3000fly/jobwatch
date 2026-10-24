"""Scraper OLX (kategoria Praca) — wewnetrzne API JSON, z ktorego korzysta ich frontend.

    python3 olx.py                         # frazy i miasta z profili w bazie
    python3 olx.py --debug
    python3 olx.py --query magazynier --city Sieradz --radius 30 --pages 2

  GET https://www.olx.pl/api/v1/offers/?category_id=4&query=...&city_id=...&distance=...

Jedna strona = 50 ofert z pelnym opisem i parametrami (umowa, widelki, tryb
pracy), wiec nie trzeba wchodzic w pojedyncze ogloszenia. API nie przyjmuje
wspolrzednych - miasto trzeba najpierw zamienic na city_id przez autocomplete.
OLX nie odda wiecej niz offset 1000 na jedno zapytanie.

Dla profili z "pokazuj prace zdalna" leci dodatkowe zapytanie bez miasta,
z filtrem workplace=remote_work_possibility.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests")
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

BASE = "https://www.olx.pl"
API = f"{BASE}/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
SOURCE = "olx"
CATEGORY_JOBS = 4
PAGE = 50
MAX_OFFSET = 1000

# workplace z OLX -> nazwy jak na pracuj.pl, zeby match.is_remote dzialal tak samo
WORKPLACE = {"on_site": "Praca stacjonarna", "hybrid": "Praca hybrydowa",
             "remote_work_possibility": "Praca zdalna", "mobile": "Praca mobilna"}
WORK_MODES = {"hybrid": "hybrydowa", "remote_work_possibility": "zdalna"}


def get(s, url, params=None):
    r = s.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def clean(x):
    if x in (None, "", [], {}):
        return None
    x = re.sub(r"<(br|/p|/li)\s*/?>", " ", str(x), flags=re.I)
    x = re.sub(r"<[^>]+>", " ", x)
    return re.sub(r"\s+", " ", x).strip() or None


_city_ids = {}


def city_id(s, name):
    """'Sieradz' -> 15533. Pierwszy wynik autocomplete z dokladna nazwa, inaczej pierwszy."""
    key = name.strip().lower()
    if key not in _city_ids:
        try:
            hits = get(s, f"{API}/geo-encoder/location-autocomplete/",
                       {"query": name.strip()}).get("data") or []
        except (requests.RequestException, ValueError) as e:
            print(f"[olx] nie znam miasta {name}: {e}", file=sys.stderr)
            hits = []
        cities = [h["city"] for h in hits if h.get("city")]
        exact = [c for c in cities if c.get("name", "").lower() == key]
        best = (exact or cities or [None])[0]
        _city_ids[key] = best["id"] if best else None
    return _city_ids[key]


def params_of(o):
    return {p.get("key"): p.get("value") or {} for p in o.get("params") or []}


def parse_item(o):
    title = clean(o.get("title"))
    oid = o.get("id")
    url = o.get("url")
    if not title or not oid or not url:
        return None
    p = params_of(o)

    loc = o.get("location") or {}
    city = (loc.get("city") or {}).get("name")
    district = (loc.get("district") or {}).get("name")
    if city and district:
        city = f"{city}, {district}"

    lo = hi = cur = None
    sal = p.get("salary") or {}
    if sal and not sal.get("arranged"):
        lo, hi, cur = sal.get("from"), sal.get("to"), sal.get("currency") or "PLN"
        if sal.get("type") == "hourly":           # stawka godzinowa -> miesiecznie
            lo = lo * 168 if isinstance(lo, (int, float)) else lo
            hi = hi * 168 if isinstance(hi, (int, float)) else hi

    wp_keys = (p.get("workplace") or {}).get("key") or []
    wp_keys = wp_keys if isinstance(wp_keys, list) else [wp_keys]
    modes = [WORK_MODES[k] for k in wp_keys if k in WORK_MODES]

    contract = ", ".join(x for x in (clean((p.get("agreement") or {}).get("label")),
                                     clean((p.get("type") or {}).get("label"))) if x)

    # Parametry doklejamy do opisu - tak samo robi pracuj.py, a match/cv szukaja w opisie.
    extra = []
    for prm in o.get("params") or []:
        label = clean((prm.get("value") or {}).get("label"))
        if prm.get("key") in ("salary", "workplace") or not label:
            continue
        extra.append(f"{prm.get('name')}: {label}")
    if wp_keys:
        extra.append("Tryb pracy: " + ", ".join(WORKPLACE.get(k, k) for k in wp_keys))
    desc = " ".join(x for x in [clean(o.get("description")), ". ".join(extra)] if x)

    user = o.get("user") or {}
    company = clean(user.get("company_name")) or clean(user.get("name"))

    return {
        "schema_version": 1,
        "source": SOURCE,
        "external_id": str(oid),
        "url": url,
        "title": title,
        "company": company,
        "city": city,
        "salary_min": float(lo) if isinstance(lo, (int, float)) else None,
        "salary_max": float(hi) if isinstance(hi, (int, float)) else None,
        "currency": cur,
        "contract": contract or None,
        "description": (desc or "")[:8000] or None,
        "work_modes": modes,
        "published_at": o.get("created_time"),
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "raw": o,
    }


def profile_queries():
    """(fraza, miasto|None, promien, tylko_zdalne) z aktywnych profili."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from common import connect
        rows = connect().execute("SELECT config FROM profiles WHERE active=1").fetchall()
    except Exception as e:
        print(f"[olx] nie czytam profili: {e}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        cfg = json.loads(r["config"])
        cities = [c.strip() for c in cfg.get("cities") or [] if c.strip()]
        radius = cfg.get("radius_km", 30) or 0
        for t in cfg.get("must_titles") or []:
            t = t.strip().lower()
            if len(t) < 3:
                continue
            qs = [(t, c, radius, False) for c in cities] or [(t, None, 0, False)]
            if cities and cfg.get("remote_ok", True):
                qs.append((t, None, 0, True))
            out += [q for q in qs if q not in out]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", action="append", default=[],
                    help="fraza; mozna podac wiele razy")
    ap.add_argument("--city", help="miasto (z --query)")
    ap.add_argument("--radius", type=int, default=30, help="promien w km wokol --city")
    ap.add_argument("--remote", action="store_true", help="tylko oferty z praca zdalna")
    ap.add_argument("--pages", type=int, default=4, help="stron po 50 na jedno zapytanie")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="dla zgodnosci z daily.sh")
    args = ap.parse_args()

    queries = ([(q, args.city, args.radius, args.remote) for q in args.query]
               if args.query else profile_queries())
    if not queries:
        print("[olx] brak fraz: podaj --query albo uzupelnij 'Stanowiska' w profilu",
              file=sys.stderr)
        return 1

    # CloudFront przed API odrzuca odcisk TLS gołego requests (403);
    # cloudscraper ma inny zestaw szyfrow i przechodzi.
    if cloudscraper:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False})
    else:
        print("[olx] brak cloudscraper - pip install cloudscraper, inaczej 403",
              file=sys.stderr)
        s = requests.Session()
        s.headers["User-Agent"] = UA
    s.headers.update({"Accept": "application/json", "Accept-Language": "pl-PL,pl;q=0.9"})

    seen, ok = set(), 0
    for qi, (q, city, radius, remote) in enumerate(queries):
        print(f"[progress] {qi}/{len(queries)}", file=sys.stderr, flush=True)
        prm = {"category_id": CATEGORY_JOBS, "query": q, "limit": PAGE,
               "sort_by": "created_at:desc"}
        label = q
        if city:
            cid = city_id(s, city)
            if not cid:
                continue
            prm.update(city_id=cid, distance=radius)
            label += f" @ {city} +{radius}km"
        if remote:
            prm["filter_enum_workplace[0]"] = "remote_work_possibility"
            label += " (zdalnie)"

        for page in range(1 if args.debug else args.pages):
            prm["offset"] = page * PAGE
            if prm["offset"] >= MAX_OFFSET:
                break
            try:
                data = get(s, f"{API}/offers/", prm)
            except (requests.RequestException, ValueError) as e:
                print(f"[olx] blad '{label}' str.{page + 1}: {e}", file=sys.stderr)
                break
            items = data.get("data") or []
            print(f"[olx] '{label}' str.{page + 1}: {len(items)} ofert", file=sys.stderr)

            for it in items:
                rec = parse_item(it)
                if not rec or rec["external_id"] in seen:
                    continue
                seen.add(rec["external_id"])
                if args.debug:
                    print(json.dumps(rec, ensure_ascii=False, indent=2)[:4000])
                    return 0
                print(json.dumps(rec, ensure_ascii=False))
                ok += 1
                if args.limit and ok >= args.limit:
                    break
            if args.limit and ok >= args.limit:
                break
            time.sleep(args.delay)
            if len(items) < PAGE or not (data.get("links") or {}).get("next"):
                break
        if args.limit and ok >= args.limit:
            break

    print(f"[olx] zapisane={ok}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
