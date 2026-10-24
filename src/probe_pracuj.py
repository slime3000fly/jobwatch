"""ZWIAD. Odpal to PIERWSZE, w sobote rano, zanim napiszesz cokolwiek innego.

Sprawdza trzy podejscia do pracuj.pl od najtanszego do najdrozszego
i mowi, ktore przechodzi. Od wyniku zalezy plan calego weekendu.

    pip install requests cloudscraper
    python3 probe_pracuj.py

Jesli przejdzie __NEXT_DATA__ - masz gotowy JSON i scraper to godzina roboty.
Jesli tylko Playwright - doloz sobie pol dnia.
Jesli nic - kup gotowy actor na Apify i zrzuc jego output do raw/. Pipeline
nie zauwazy roznicy, o to wlasnie chodzilo w projekcie opartym o JSON.
"""
import json
import re
import sys

URL = "https://www.pracuj.pl/praca/ksiegowa;kw"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def find_next_data(html: str):
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# Pola, ktore wystepuja w ogloszeniu, a nie w drzewie kategorii.
JOBBY = {"title", "jobtitle", "offertitle", "positiontitle", "name",
         "employer", "employername", "companyname", "company",
         "salary", "salarydisplaytext", "wage", "offerurl", "joburl",
         "offerid", "groupid", "lastpublicated", "expirationdate",
         "workplaces", "workplace", "citiesnames", "typesofcontract",
         "employmenttypenames", "positionlevels", "jobdescription"}

IGNORE_KEYS = {"id", "level", "name", "parentid"}   # drzewo kategorii


def score_list(lst):
    """Jak bardzo ta lista wyglada na liste ofert."""
    if not lst or not all(isinstance(x, dict) for x in lst):
        return 0
    keys = {k.lower() for k in lst[0].keys()}
    if keys == IGNORE_KEYS or keys <= IGNORE_KEYS:
        return 0
    hits = len(keys & JOBBY)
    if hits < 2:
        return 0
    # dluga lista z duza liczba pol = najpewniej oferty
    return hits * 10 + min(len(lst), 50) + min(len(keys), 30)


def find_offer_lists(obj, path="$", out=None):
    """Zwraca kandydatow [(wynik, sciezka, lista)] posortowanych malejaco."""
    if out is None:
        out = []
    if isinstance(obj, list):
        sc = score_list(obj)
        if sc:
            out.append((sc, path, obj))
        for i, x in enumerate(obj[:5]):
            find_offer_lists(x, f"{path}[{i}]", out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            find_offer_lists(v, f"{path}.{k}", out)
    return sorted(out, key=lambda t: -t[0])


def report(name, html):
    if not html:
        print(f"  {name:14s} BRAK ODPOWIEDZI")
        return False
    blocked = any(s in html.lower() for s in
                  ("just a moment", "cf-browser-verification", "attention required"))
    nd = find_next_data(html)
    print(f"  {name:14s} {len(html):>8d} znakow  "
          f"cloudflare={'TAK' if blocked else 'nie'}  "
          f"__NEXT_DATA__={'JEST' if nd else 'brak'}")
    if nd:
        cands = find_offer_lists(nd)
        if not cands:
            print("      -> nie znalazlem listy wygladajacej na oferty")
            with open("probe_nextdata.json", "w", encoding="utf-8") as fh:
                json.dump(nd, fh, ensure_ascii=False, indent=2)
            print("      -> caly __NEXT_DATA__ w probe_nextdata.json")
        else:
            print(f"      -> {len(cands)} kandydatow, od najlepszego:")
            for sc, path, lst in cands[:3]:
                k = sorted(lst[0].keys())
                t = lst[0].get("title") or lst[0].get("jobTitle") or lst[0].get("name")
                print(f"         [{sc:3d}] {path}  ({len(lst)} szt.)")
                print(f"               klucze: {k[:14]}")
                print(f"               pierwszy tytul: {str(t)[:60]}")
            with open("probe_sample.json", "w", encoding="utf-8") as fh:
                json.dump({"sciezka": cands[0][1], "przyklady": cands[0][2][:2]},
                          fh, ensure_ascii=False, indent=2)
            print("      -> najlepszy kandydat (2 rekordy) w probe_sample.json")
    return bool(nd) and not blocked


def main():
    print(f"Cel: {URL}\n")

    print("[1] zwykly requests")
    try:
        import requests
        r = requests.get(URL, headers={"User-Agent": UA}, timeout=20)
        if report("requests", r.text):
            print("\nWYNIK: dziala najprostsza droga. Scraper to godzina.")
            return 0
    except Exception as e:
        print(f"  requests       blad: {e}")

    print("\n[2] cloudscraper")
    try:
        import cloudscraper
        s = cloudscraper.create_scraper()
        r = s.get(URL, timeout=30)
        if report("cloudscraper", r.text):
            print("\nWYNIK: cloudscraper przechodzi. Uzyj go w scrapers/pracuj.py.")
            return 0
    except ImportError:
        print("  cloudscraper   brak, zainstaluj: pip install cloudscraper")
    except Exception as e:
        print(f"  cloudscraper   blad: {e}")

    print("\n[3] Playwright")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(user_agent=UA)
            pg.goto(URL, wait_until="networkidle", timeout=45000)
            html = pg.content()
            b.close()
        if report("playwright", html):
            print("\nWYNIK: tylko Playwright. Wolniej, ale dziala. Doloz pol dnia.")
            return 0
    except ImportError:
        print("  playwright     brak: pip install playwright && playwright install chromium")
    except Exception as e:
        print(f"  playwright     blad: {e}")

    print("\nWYNIK: nic nie przeszlo. Plan B - gotowy scraper z Apify")
    print("do raw/YYYY-MM-DD_pracuj.jsonl. Reszta pipeline'u bez zmian.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
