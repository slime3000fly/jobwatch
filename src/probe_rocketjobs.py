"""Zwiad RocketJobs: gdzie naprawde jest sitemapa i jak wygladaja URL-e ofert.

    python3 probe_rocketjobs.py

Sprawdza robots.txt (tam serwisy deklaruja sitemapy), potem kilka typowych
sciezek, potem schodzi do podsitemap i pokazuje przykladowe adresy.
Wklej output, a dopasuje scraper do tego, co faktycznie jest.
"""
import re
import sys
from collections import Counter
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

BASE = "https://rocketjobs.pl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

CANDIDATES = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemaps.xml", "/sitemap/sitemap.xml", "/server-sitemap.xml",
    "/sitemap-0.xml", "/pl/sitemap.xml",
]


def get(s, url):
    try:
        r = s.get(url, timeout=25)
        return r
    except Exception as e:
        print(f"    blad: {e}")
        return None


def locs(xml):
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def main():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    found = []

    print("[1] robots.txt")
    r = get(s, urljoin(BASE, "/robots.txt"))
    if r is not None and r.status_code == 200:
        declared = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
        print(f"    status {r.status_code}, zadeklarowane sitemapy: {declared or 'brak'}")
        found += declared
    else:
        print(f"    status {getattr(r, 'status_code', '?')}")

    print("\n[2] typowe sciezki")
    for path in CANDIDATES:
        url = urljoin(BASE, path)
        r = get(s, url)
        if r is None:
            continue
        ct = r.headers.get("content-type", "")[:40]
        n = len(locs(r.text)) if r.status_code == 200 and "xml" in ct else 0
        mark = " <--" if n else ""
        print(f"    {r.status_code}  {path:28s} {len(r.content):>8d} B  {ct:22s} loc={n}{mark}")
        if n:
            found.append(url)

    found = list(dict.fromkeys(found))
    if not found:
        print("\nNic nie znalazlem. Wejdz recznie na rocketjobs.pl, otworz dowolna "
              "oferte i wklej mi jej adres - zrobie scraper bez sitemapy, po stronach "
              "wynikow wyszukiwania.")
        return 1

    print(f"\n[3] zawartosc ({len(found)} sitemap)")
    offer_urls = []
    for url in found[:6]:
        r = get(s, url)
        if r is None or r.status_code != 200:
            continue
        ls = locs(r.text)
        subs = [u for u in ls if u.endswith(".xml")]
        pages = [u for u in ls if not u.endswith(".xml")]
        print(f"\n  {url}")
        print(f"    podsitemapy: {len(subs)}, adresy: {len(pages)}")
        for sub in subs[:12]:
            print(f"      - {sub}")
        for pg in pages[:5]:
            print(f"      . {pg}")
        offer_urls += pages
        # zejdz jeden poziom nizej
        for sub in subs[:4]:
            rs = get(s, sub)
            if rs is None or rs.status_code != 200:
                continue
            sl = locs(rs.text)
            print(f"      -> {sub.rsplit('/', 1)[-1]}: {len(sl)} adresow")
            for u in sl[:3]:
                print(f"           {u}")
            offer_urls += [u for u in sl if not u.endswith(".xml")]

    if offer_urls:
        print(f"\n[4] ksztalt adresow ({len(offer_urls)} zebranych)")
        segs = Counter()
        for u in offer_urls:
            parts = [p for p in u.replace(BASE, "").split("/") if p]
            if parts:
                segs[parts[0]] += 1
        for seg, n in segs.most_common(10):
            print(f"    /{seg}/...  {n}")
        print("\n  Przykladowe pelne adresy:")
        for u in offer_urls[:5]:
            print(f"    {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
