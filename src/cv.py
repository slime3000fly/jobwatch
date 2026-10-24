"""Modul CV: wyciaga tekst, proponuje profil, pokazuje luki wzgledem oferty.

    python3 cv.py cv.pdf                  # propozycja profilu
    python3 cv.py cv.pdf --save "Siostra" # zapis do bazy

Czego tu CELOWO NIE MA: procentowego "dopasowania CV do oferty" liczonego
embeddingami. Przy ofertach nie-IT ("stabilne zatrudnienie, mily zespol")
taki wskaznik jest szumem udajacym precyzje. Zamiast tego modul robi dwie
rzeczy, ktore realnie pomagaja: buduje profil z CV i wypisuje konkretne
wymagania oferty nieobecne w CV.

Formaty: .txt zawsze, .docx przez python-docx, .pdf przez pdfminer.six.
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from common import connect, normalize_text

STOP = set("""
a aby albo ale ani az bardzo bez bo by byc byl byla byly bym co czy dla do
gdy gdzie i ich ile im inne jak jako je jego jej jest jestem juz kazdy kiedy
kto ktora ktore ktorego ktory ku lat lub ma majac mam mi mnie moge moj moze
na nad nam nas nasz nie nim niz o od oraz po pod ponad poniewaz praca pracy
przez przy raz sa sie sobie swoje ta tak takze tam te tego tej ten teraz tez
to tu tych tylko tym u w we wiec wszystko z za ze zeby oferujemy poszukujemy
firma osoba stanowisko obowiazki wymagania oferta mile widziane umowa etat
doswiadczenie umiejetnosci wyksztalcenie znajomosc dobra bardzo mozliwosc
rozwoju zespol miejsce czas pelny pelnym wymiarze zatrudnienie stabilne
""".split())

# Frazy warte wylapania w calosci (dwuwyrazowe kompetencje)
PHRASES = [
    "pelna ksiegowosc", "ksiega przychodow", "deklaracje vat", "cit", "pit",
    "bilans", "srodki trwale", "kadry i place", "platnik", "symfonia",
    "optima", "enova", "sap", "comarch", "excel", "rachunkowosc zarzadcza",
    "sprawozdanie finansowe", "jpk", "audyt", "windykacja",
    "wozek widlowy", "uprawnienia udt", "wms", "sap ewm", "skaner",
    "kompletacja", "inwentaryzacja", "suwnica", "prawo jazdy",
    "angielski", "niemiecki", "francuski",
]

TITLE_HINTS = [
    "ksiegow", "glowna ksiegowa", "samodzielna ksiegowa", "accountant",
    "specjalista ds rozliczen", "analityk", "kadrowa", "fakturzystka",
    "magazynier", "operator wozka", "pracownik magazynu", "logistyk",
    "kierowca", "monter", "operator produkcji", "handlowiec", "sprzedawca",
    "asystent", "recepcjonist", "kierownik",
]

LEVEL_HINTS = ["junior", "mlodszy", "samodzielna", "samodzielny", "starszy",
               "glowna", "glowny", "senior", "specjalista", "kierownik", "manager"]


def read_cv(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if suf == ".docx":
        try:
            import docx
        except ImportError:
            sys.exit("pip install python-docx")
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    if suf == ".pdf":
        try:
            from pdfminer.high_level import extract_text
        except ImportError:
            sys.exit("pip install pdfminer.six")
        return extract_text(str(path))
    sys.exit(f"nieobslugiwany format: {suf}")


def terms(text: str) -> set:
    """Zbior znaczacych terminow: frazy + pojedyncze slowa."""
    n = normalize_text(text)
    found = {p for p in PHRASES if p in n}
    words = [w for w in n.split() if len(w) > 3 and w not in STOP]
    found |= {w for w, c in Counter(words).items() if c >= 2}
    return found


def propose_profile(text: str) -> dict:
    n = normalize_text(text)
    t = terms(text)

    titles = [h for h in TITLE_HINTS if h in n]
    levels = [h for h in LEVEL_HINTS if h in n]
    skills = sorted(x for x in t if x in PHRASES)

    # Miasto: szukamy w naglowku CV (pierwsze 400 znakow)
    head = normalize_text(text[:400])
    cities = [c for c in ("warszawa", "krakow", "wroclaw", "poznan", "gdansk",
                          "lodz", "katowice", "szczecin", "lublin", "bydgoszcz")
              if c in head]

    return {
        "cv_terms": sorted(t),
        "must_titles": titles[:6] or ["<uzupelnij: czego szukasz>"],
        "levels": levels[:4],
        "nice_to_have": skills[:12],
        "blacklist": [],
        "cities": [c.capitalize() for c in cities],
        "min_salary": 0,
    }


def gap(cv_terms: set, offer_text: str, limit=8):
    """Czego oferta wymaga, a czego nie ma w CV. To jest wartosc tego modulu."""
    o = terms(offer_text)
    missing = sorted(x for x in (o - cv_terms) if x in PHRASES or len(x) > 5)
    overlap = sorted(cv_terms & o)
    return overlap[:limit], missing[:limit]


def find_email(text: str):
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text or "")
    return m.group(0) if m else None


def draft_letter(prof: dict, offer, have, miss) -> str:
    """Szkic listu. Konkretny tam, gdzie moze byc: wymienia realne pokrycie
    wymagan z ogloszenia. Luki zostawia do recznej decyzji kandydata."""
    who = prof.get("display_name") or "[Twoje imie i nazwisko]"
    title = offer["title"]
    company = offer["company"] or "Panstwa firmie"

    L = ["Dzien dobry,", "",
         f"pisze w odpowiedzi na ogloszenie na stanowisko {title} w {company}."]
    if have:
        L += ["", "Z wymagan wymienionych w ogloszeniu mam doswiadczenie w nastepujacych obszarach:"]
        L += [f"  - {h}" for h in have]
    if miss:
        L += ["", "[DO DECYZJI: ogloszenie wymienia tez " + ", ".join(miss) +
              ". Jesli masz z tym stycznosc, dopisz jedno zdanie. Jesli nie - "
              "usun ten akapit, nie tlumacz sie z brakow.]"]
    L += ["", "[JEDNO ZDANIE: dlaczego akurat ta firma. Zajrzyj na ich strone - tego "
          "nie da sie zautomatyzowac, a rekruter to widzi.]",
          "", "W zalaczniku przesylam CV. Chetnie opowiem wiecej podczas rozmowy.", "",
          "Z powazaniem,", who, "", "---",
          "Wyrazam zgode na przetwarzanie moich danych osobowych przez "
          f"{company} w celu prowadzenia rekrutacji na stanowisko, na ktore aplikuje."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cv")
    ap.add_argument("--save", metavar="NAZWA", help="zapisz jako profil")
    ap.add_argument("--gap", type=int, metavar="N", help="pokaz luki dla N ofert")
    args = ap.parse_args()

    text = read_cv(Path(args.cv))
    if len(text.strip()) < 100:
        sys.exit("Z pliku wyszlo prawie nic. Skan? Sprobuj wyeksportowac CV jako .docx")

    prof = propose_profile(text)
    print(json.dumps(prof, ensure_ascii=False, indent=2))
    print(f"\n[cv] {len(text)} znakow, {len(terms(text))} terminow", file=sys.stderr)
    print("[cv] To PROPOZYCJA. Popraw ja w GUI - zwlaszcza blackliste "
          "i min_salary, ktorych z CV nie da sie odgadnac.", file=sys.stderr)

    conn = connect()
    if args.save:
        conn.execute(
            "INSERT OR REPLACE INTO profiles (name, config, active, created_at) VALUES (?,?,1,?)",
            (args.save, json.dumps(prof, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        print(f"[cv] zapisano profil '{args.save}'", file=sys.stderr)

    if args.gap:
        cvt = terms(text)
        rows = conn.execute(
            "SELECT title, company, description FROM offers LIMIT ?", (args.gap,)).fetchall()
        for r in rows:
            have, miss = gap(cvt, f"{r['title']} {r['description'] or ''}")
            print(f"\n— {r['title']} ({r['company']})")
            print(f"  masz:   {', '.join(have) or '—'}")
            print(f"  brakuje: {', '.join(miss) or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
