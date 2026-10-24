"""Scraper RocketJobs.pl — sitemapa zadeklarowana w robots.txt.

    python3 scrapers/rocketjobs.py --debug            # jedna oferta, podglad
    python3 scrapers/rocketjobs.py --limit 400        # normalny przebieg

W sitemapie jest ~16 tys. ofert. Pobieranie kazdej codziennie to kilka godzin
i kilkanascie tysiecy requestow — bez sensu i nieuprzejme wobec serwisu.
Dlatego domyslnie:

  * NOWE adresy (nieznane bazie) -> pobieramy strone i parsujemy w calosci,
  * ZNANE adresy                 -> emitujemy lekki rekord {"seen_only": true},
                                    ktory tylko odswieza historie.

Dzieki temu pierwszy przebieg jest duzy, a kazdy kolejny kosztuje tyle, ile
pojawilo sie nowych ofert. Historia wiszenia ogloszenia (kluczowa dla
wykrywania widm) i tak jest kompletna, bo sitemapa mowi, co nadal zyje.
"""
import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from common import connect, normalize_text  # noqa: E402

BASE = "https://rocketjobs.pl"
ROBOTS = f"{BASE}/robots.txt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
SOURCE = "rocketjobs"
OFFER_RE = re.compile(r"/oferta-pracy/")


def get(s, url, **kw):
    """GET z ponowieniem po 429 - przy kilku watkach serwis czasem przyhamowuje."""
    for attempt in range(4):
        r = s.get(url, timeout=30, **kw)
        if r.status_code != 429 or attempt == 3:
            break
        try:
            wait = float(r.headers.get("Retry-After") or 0)
        except ValueError:
            wait = 0
        time.sleep(max(wait, 5 * (attempt + 1)))
    r.raise_for_status()
    return r


def locs(xml):
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def offer_urls(s):
    """robots.txt -> sitemapa ofert -> podsitemapy -> adresy ogloszen.

    Nie zgadujemy sciezek: /sitemap.xml zwraca HTML z kodem 200 (miekkie 404),
    wiec jedyne pewne zrodlo to deklaracja w robots.txt.
    """
    declared = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", get(s, ROBOTS).text)
    jobs = [u for u in declared if "active-jobs" in u] or declared
    urls = []
    for sm in jobs:
        xml = get(s, sm).text
        for loc in locs(xml):
            if loc.endswith(".xml"):
                time.sleep(0.3)
                urls += [u for u in locs(get(s, loc).text) if OFFER_RE.search(u)]
            elif OFFER_RE.search(loc):
                urls.append(loc)
    return list(dict.fromkeys(urls))


def known_ids():
    """Co juz mamy w bazie — zeby nie pobierac tego drugi raz."""
    try:
        conn = connect()
        return {r["external_id"] for r in conn.execute(
            "SELECT external_id FROM offers WHERE source=?", (SOURCE,))}
    except Exception as e:
        print(f"[rocketjobs] baza niedostepna ({e}), pobieram wszystko", file=sys.stderr)
        return set()


def profile_slugs():
    """Stanowiska z aktywnych profili w postaci jak w adresie: 'glowna-ksiegowa'."""
    try:
        rows = connect().execute("SELECT config FROM profiles WHERE active=1").fetchall()
    except Exception:
        return []
    out = set()
    for r in rows:
        for t in json.loads(r["config"]).get("must_titles") or []:
            t = normalize_text(t).replace(" ", "-")
            if len(t) >= 3:
                out.add(t)
    return sorted(out)


def slug_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def deep_find(obj, keys, _depth=0):
    if _depth > 12:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v not in (None, "", [], {}):
                return v
        for v in obj.values():
            r = deep_find(v, keys, _depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, keys, _depth + 1)
            if r is not None:
                return r
    return None


def clean(x):
    if isinstance(x, (dict, list)):
        x = json.dumps(x, ensure_ascii=False)
    x = re.sub(r"<[^>]+>", " ", str(x))
    return re.sub(r"\s+", " ", x).strip()


def job_posting(html):
    """schema.org JobPosting z JSON-LD - stabilniejszy niz wewnetrzne dane Next.js."""
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return None


def parse_ldjson(url, jp):
    title = jp.get("title")
    if not title:
        return None
    org = jp.get("hiringOrganization") or {}
    locs = jp.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    cities = []
    for loc in locs:
        c = ((loc or {}).get("address") or {}).get("addressLocality")
        if c and c not in cities:
            cities.append(c)
    if not cities and jp.get("jobLocationType") == "TELECOMMUTE":
        cities = ["Zdalnie"]
    sal = jp.get("baseSalary") or {}
    val = sal.get("value") or {}
    lo, hi = val.get("minValue"), val.get("maxValue")
    if val.get("unitText") == "HOUR":           # stawka godzinowa -> miesiecznie
        lo = lo * 168 if isinstance(lo, (int, float)) else lo
        hi = hi * 168 if isinstance(hi, (int, float)) else hi
    emp = jp.get("employmentType")
    return {
        "schema_version": 1,
        "source": SOURCE,
        "external_id": slug_of(url),
        "url": url,
        "title": clean(title),
        "company": clean(org.get("name")) if org.get("name") else None,
        "city": "; ".join(cities) or None,
        "salary_min": float(lo) if isinstance(lo, (int, float)) else None,
        "salary_max": float(hi) if isinstance(hi, (int, float)) else None,
        "currency": sal.get("currency") or ("PLN" if lo else None),
        "contract": clean(emp) if emp else None,
        "description": clean(jp.get("description") or "")[:8000] or None,
        # brak jawnego pola o hybrydzie - common.work_mode zgadnie z opisu,
        # a TELECOMMUTE i tak daje city="Zdalnie"
        "published_at": jp.get("datePosted"),
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "raw": jp,
    }


def parse_offer(url, html):
    jp = job_posting(html)
    if jp:
        rec = parse_ldjson(url, jp)
        if rec:
            return rec
    nd = next_data(html)
    if not nd:
        return None
    props = nd.get("props", {}).get("pageProps", nd)

    title = deep_find(props, ["title", "jobTitle", "name"])
    if not title:
        return None

    company = deep_find(props, ["companyName", "company", "employer", "brandName"])
    if isinstance(company, dict):
        company = company.get("name")
    city = deep_find(props, ["city", "cityName", "location", "workplace"])
    if isinstance(city, dict):
        city = city.get("name") or city.get("city")

    emp = deep_find(props, ["employmentTypes", "employment_types", "salary"]) or []
    lo = hi = cur = None
    if isinstance(emp, list) and emp and isinstance(emp[0], dict):
        e0 = emp[0]
        lo = e0.get("fromPln") or e0.get("from") or e0.get("salaryFrom")
        hi = e0.get("toPln") or e0.get("to") or e0.get("salaryTo")
        cur = e0.get("currency") or "PLN"

    body = clean(deep_find(props, ["body", "description", "requirements"]) or "")

    return {
        "schema_version": 1,
        "source": SOURCE,
        "external_id": slug_of(url),
        "url": url,
        "title": clean(title),
        "company": clean(company) if company else None,
        "city": clean(city) if city else None,
        "salary_min": float(lo) if isinstance(lo, (int, float)) else None,
        "salary_max": float(hi) if isinstance(hi, (int, float)) else None,
        "currency": cur,
        "contract": clean(deep_find(props, ["employmentType", "contractType"]) or "") or None,
        "description": body[:8000] or None,
        "published_at": deep_find(props, ["publishedAt", "published_at", "createdAt"]),
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "raw": props if isinstance(props, dict) else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400,
                    help="ile NOWYCH ofert pobrac w tym przebiegu")
    ap.add_argument("--delay", type=float, default=0.3, help="pauza na watek")
    ap.add_argument("--workers", type=int, default=3, help="rownolegle pobrania")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="pobieraj tez znane oferty (wolne, zwykle niepotrzebne)")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA

    urls = offer_urls(s)
    print(f"[rocketjobs] sitemapa: {len(urls)} ofert", file=sys.stderr)
    if not urls:
        print("[rocketjobs] ZERO adresow — odpal probe_rocketjobs.py", file=sys.stderr)
        return 1

    known = set() if args.all else known_ids()
    new = [u for u in urls if slug_of(u) not in known]
    old = [u for u in urls if slug_of(u) in known]
    print(f"[rocketjobs] nowych: {len(new)}, znanych: {len(old)}", file=sys.stderr)

    # Adres zawiera tytul ("...-glowna-ksiegowa-rybnik-finanse..."), wiec oferty
    # pasujace do profili ida na poczatek kolejki. Reszta tylko jesli starczy limitu.
    wanted = profile_slugs()
    if wanted:
        hit = [u for u in new if any(w in slug_of(u) for w in wanted)]
        rest = set(new) - set(hit)
        new = hit + [u for u in new if u in rest]
        print(f"[rocketjobs] pasujacych do profili: {len(hit)}", file=sys.stderr)

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # Znane: jedna linijka na oferte, zero requestow.
    if not args.debug:
        for u in old:
            print(json.dumps({"schema_version": 1, "source": SOURCE,
                              "external_id": slug_of(u), "url": u,
                              "seen_only": True, "fetched_at": now},
                             ensure_ascii=False))

    todo = new[:1] if args.debug else new[:args.limit]
    local = threading.local()

    def fetch(u):
        if not hasattr(local, "s"):              # Session nie jest bezpieczna miedzy watkami
            local.s = requests.Session()
            local.s.headers["User-Agent"] = UA
        try:
            return u, parse_offer(u, get(local.s, u).text), None
        except Exception as e:
            return u, None, e
        finally:
            time.sleep(args.delay)

    ok = fail = 0
    print(f"[progress] 0/{len(todo)}", file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(fetch, u) for u in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            u, rec, err = fut.result()
            if err or not rec:
                print(f"[rocketjobs] {'blad' if err else 'nie umiem sparsowac'} {u}"
                      f"{': ' + str(err) if err else ''}", file=sys.stderr)
                fail += 1
            elif args.debug:
                print(json.dumps(rec, ensure_ascii=False, indent=2)[:4000])
                ok += 1
            else:
                print(json.dumps(rec, ensure_ascii=False))
                ok += 1
            print(f"[progress] {i}/{len(todo)}", file=sys.stderr, flush=True)

    left = max(0, len(new) - len(todo))
    print(f"[rocketjobs] pobrane={ok} bledy={fail} zostalo_nowych={left}", file=sys.stderr)
    if left:
        print(f"[rocketjobs] reszta dojdzie w kolejnych przebiegach "
              f"(albo podnies --limit)", file=sys.stderr)
    return 0 if (ok or old) else 1


if __name__ == "__main__":
    sys.exit(main())
