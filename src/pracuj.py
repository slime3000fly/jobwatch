"""Scraper pracuj.pl — dane z __NEXT_DATA__, bez wchodzenia w kazda oferte.

    python3 pracuj.py                    # frazy z profili w bazie
    python3 pracuj.py --debug
    python3 pracuj.py --query ksiegowa --pages 5
    python3 pracuj.py --query magazynier --query ksiegowa --pages 3

Strona wynikow niesie komplet danych 50 ofert w
  $.props.pageProps.dehydratedState.queries[*].state.data.groupedOffers
wiec jedna strona = jeden request = 50 ofert. To radykalnie taniej niz
pobieranie ogloszen pojedynczo.

CENA TEJ OSZCZEDNOSCI: lista wynikow nie zawiera pelnego opisu stanowiska,
tylko tytul, firme, lokalizacje, widelki, typ umowy i krotkie podsumowanie.
Do dopasowania po tytule i do wykrywania widm to wystarcza. Jesli chcesz
pelne opisy (lepsze kafelki kompetencji), dolacz --details — wtedy scraper
dociaga strony pojedynczych ofert i robi sie kilkadziesiat razy wolniejszy.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

BASE = "https://www.pracuj.pl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
SOURCE = "pracuj"


def get(s, url):
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return r


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def grouped_offers(nd):
    """Wszystkie listy groupedOffers z dehydratedState, sklejone."""
    out = []
    queries = (nd.get("props", {}).get("pageProps", {})
                 .get("dehydratedState", {}).get("queries", []))
    for q in queries:
        # state.data bywa tez intem albo lista (liczniki, slowniki) - pomijamy
        data = (q.get("state") or {}).get("data")
        if not isinstance(data, dict):
            continue
        go = data.get("groupedOffers")
        if isinstance(go, list):
            out += go
    if out:
        return out
    # awaryjnie: szukaj klucza gdziekolwiek
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("groupedOffers"), list):
                out.extend(o["groupedOffers"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(nd)
    return out


def clean(x):
    """Listy skladamy przecinkami, nie JSON-em - to ma byc czytelny tekst."""
    if x in (None, "", [], {}):
        return None
    if isinstance(x, list):
        parts = [clean(i) for i in x]
        x = ", ".join(p for p in parts if p)
    elif isinstance(x, dict):
        x = x.get("name") or x.get("value") or json.dumps(x, ensure_ascii=False)
    x = re.sub(r"<[^>]+>", " ", str(x))
    return re.sub(r"\s+", " ", x).strip() or None


SAL_RE = re.compile(r"(\d[\d\s  ]*)\s*[-–—]\s*(\d[\d\s  ]*)")
SAL_ONE = re.compile(r"(\d[\d\s  ]*)")


def parse_salary(text):
    """'7 000–9 000 zl brutto / mies.' -> (7000.0, 9000.0, 'PLN')"""
    if not text:
        return None, None, None
    t = str(text).replace("\u00a0", " ").replace("\u202f", " ")
    cur = "PLN" if "zł" in t or "zl" in t.lower() else (
        "EUR" if "eur" in t.lower() else "USD" if "usd" in t.lower() else "PLN")
    m = SAL_RE.search(t)
    if m:
        try:
            return (float(m.group(1).replace(" ", "")),
                    float(m.group(2).replace(" ", "")), cur)
        except ValueError:
            pass
    m = SAL_ONE.search(t)
    if m:
        try:
            v = float(m.group(1).replace(" ", ""))
            return (v, None, cur) if v >= 1000 else (None, None, None)
        except ValueError:
            pass
    return None, None, cur


def first_sub(item, *keys):
    """Pierwsza wartosc z listy podofert, np. adres albo miasto."""
    for sub in item.get("offers") or []:
        if isinstance(sub, dict):
            for k in keys:
                if sub.get(k):
                    return sub[k]
    return None


def parse_item(item):
    title = clean(item.get("jobTitle") or item.get("title"))
    if not title:
        return None

    url = (item.get("offerAbsoluteUri") or first_sub(item, "offerAbsoluteUri", "offerUri")
           or item.get("offerUrl"))
    oid = str(item.get("groupId") or item.get("commonOfferId")
              or item.get("offerId") or "")
    if not url and oid:
        url = f"{BASE}/praca/,oferta,{oid}"
    if not url or not oid:
        return None

    # wszystkie lokalizacje oferty, "; " miedzy nimi (przecinek to dzielnica)
    places = []
    for sub in item.get("offers") or []:
        w = clean((sub or {}).get("displayWorkplace"))
        if w and w not in places:
            places.append(w)
        if (sub or {}).get("isWholePoland") and "cała Polska" not in places:
            places.append("cała Polska")
    city = "; ".join(places[:15]) or clean(item.get("displayWorkplace"))
    lo, hi, cur = parse_salary(item.get("salaryDisplayText") or item.get("salary"))
    wm = " ".join(str(m) for m in item.get("workModes") or []).lower()
    modes = [m for m, hint in (("zdalna", "zdaln"), ("hybrydowa", "hybryd")) if hint in wm]

    # Opis sklejamy z tego, co jest na liscie wynikow.
    bits = [clean(item.get("aiSummary")),
            clean(item.get("jobDescription")),
            clean(item.get("typesOfContract")),
            clean(item.get("positionLevels")),
            clean(item.get("workSchedules")),
            clean(item.get("workModes")),
            clean(item.get("appliedProducts") and None)]
    desc = " ".join(b for b in bits if b) or None

    return {
        "schema_version": 1,
        "source": SOURCE,
        "external_id": oid,
        "url": url,
        "title": title,
        "company": clean(item.get("companyName")),
        "city": city,
        "salary_min": lo,
        "salary_max": hi,
        "currency": cur,
        "contract": clean(item.get("typesOfContract")),
        "description": (desc or "")[:8000] or None,
        "work_modes": modes,
        "published_at": item.get("lastPublicated") or item.get("initialPublicated"),
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "raw": item,
    }


def fetch_details(s, rec, delay):
    """Opcjonalnie: pelny opis ze strony oferty."""
    try:
        nd = next_data(get(s, rec["url"]).text)
        if nd:
            txt = json.dumps(nd, ensure_ascii=False)
            txt = re.sub(r"<[^>]+>", " ", txt)
            body = clean(txt)
            if body and len(body) > len(rec.get("description") or ""):
                rec["description"] = body[:8000]
    except Exception as e:
        print(f"[pracuj] szczegoly {rec['external_id']}: {e}", file=sys.stderr)
    time.sleep(delay)
    return rec


def profile_queries():
    """(fraza, miasto, promien) z aktywnych profili. Bez miast = cala Polska."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from common import connect
        conn = connect()
        rows = conn.execute("SELECT config FROM profiles WHERE active=1").fetchall()
    except Exception as e:
        print(f"[pracuj] nie czytam profili: {e}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        cfg = json.loads(r["config"])
        cities = [c.strip() for c in cfg.get("cities") or [] if c.strip()] or [None]
        radius = cfg.get("radius_km", 30) or 0
        for t in cfg.get("must_titles") or []:
            t = t.strip().lower()
            if len(t) < 3:
                continue
            for c in cities:
                q = (t, c, radius if c else 0)
                if q not in out:
                    out.append(q)
    return out


def search_url(query, page, city=None, radius=0):
    u = f"{BASE}/praca/{quote(query.strip(), safe='')};kw"
    if city:
        u += f"/{quote(city.strip().lower(), safe='')};wp"
    params = []
    if city and radius:
        params.append(f"rd={radius}")
    if page > 1:
        params.append(f"pn={page}")
    return u + ("?" + "&".join(params) if params else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", action="append", default=[],
                    help="fraza; mozna podac wiele razy")
    ap.add_argument("--city", help="miasto (z --query)")
    ap.add_argument("--radius", type=int, default=30, help="promien w km wokol --city")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--details", action="store_true", help="dociagaj pelne opisy (wolne)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="dla zgodnosci z daily.sh")
    args = ap.parse_args()

    queries = ([(q, args.city, args.radius) for q in args.query]
               if args.query else profile_queries())
    if not queries:
        print("[pracuj] brak fraz: podaj --query albo uzupelnij 'Stanowiska' w profilu",
              file=sys.stderr)
        return 1
    s = requests.Session()
    s.headers["User-Agent"] = UA

    seen, ok, empty = set(), 0, 0
    for qi, (q, city, radius) in enumerate(queries):
        print(f"[progress] {qi}/{len(queries)}", file=sys.stderr, flush=True)
        label = f"{q} @ {city} +{radius}km" if city else q
        for page in range(1, (1 if args.debug else args.pages) + 1):
            url = search_url(q, page, city, radius)
            try:
                nd = next_data(get(s, url).text)
            except Exception as e:
                print(f"[pracuj] blad {url}: {e}", file=sys.stderr)
                break
            if not nd:
                print(f"[pracuj] brak __NEXT_DATA__ na {url}", file=sys.stderr)
                break

            items = grouped_offers(nd)
            if not items:
                empty += 1
                print(f"[pracuj] '{label}' str.{page}: 0 ofert", file=sys.stderr)
                break
            print(f"[pracuj] '{label}' str.{page}: {len(items)} ofert", file=sys.stderr)

            fresh = 0
            for it in items:
                rec = parse_item(it)
                if not rec or rec["external_id"] in seen:
                    continue
                seen.add(rec["external_id"])
                fresh += 1
                if args.details:
                    rec = fetch_details(s, rec, args.delay)
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
            if not fresh or len(items) < 40:      # ostatnia strona wynikow
                break
        if args.limit and ok >= args.limit:
            break

    print(f"[pracuj] zapisane={ok} unikalnych={len(seen)}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
