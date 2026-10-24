"""Scoring widma. Czyste reguly na danych z bazy - zero sieci.

Mozesz to puszczac ile chcesz na calej historii:  python3 score.py
Zmieniles regule? Puszczasz jeszcze raz, nic nie trzeba scrapowac.

UWAGA: przez pierwsze 2-3 tygodnie reguly oparte na historii (reposty,
dlugosc wiszenia) beda zwracac 0. To nie blad, tylko brak danych.
"""
import json
import sys
from datetime import datetime

from common import connect, load_config, normalize_text

# Slowa wskazujace agencje posrednictwa / pracy tymczasowej
AGENCY_HINTS = [
    "agencja", "personnel", "recruitment", "rekrutacj", "hr solutions",
    "work service", "randstad", "adecco", "manpower", "grafton", "hays",
    "leasing pracownic", "praca tymczasowa", "outsourcing", "job impulse",
    "otto", "trenkwalder", "gi group",
]

# Tytuly bez konkretu - typowe dla ogloszen zbierajacych CV
VAGUE_TITLES = [
    "pracownik biurowy", "pracownik produkcji", "pracownik magazynu",
    "specjalista", "konsultant", "doradca", "asystent",
    "ksiegowa", "ksiegowy", "pracownik administracyjny",
]


def rule_reposts(row, ctx):
    """Ta sama tresc pod innym ID = ogloszenie krazy w kolko."""
    n = ctx["hash_counts"].get(row["content_hash"], 1)
    if n >= 4:
        return 30, f"tresc reposowana {n}x pod roznymi ID"
    if n == 3:
        return 20, "tresc reposowana 3x"
    if n == 2:
        return 10, "tresc juz raz reposowana"
    return 0, None


def rule_days_live(row, ctx):
    """Ile dni ogloszenie realnie wisi (liczone z sightings)."""
    d = ctx["days_live"].get((row["source"], row["external_id"]), 1)
    if d > 60:
        return 25, f"wisi {d} dni"
    if d > 30:
        return 15, f"wisi {d} dni"
    return 0, None


def rule_agency(row, ctx):
    c = normalize_text(row["company"])
    if any(h in c for h in AGENCY_HINTS):
        return 15, "publikuje agencja, nie pracodawca"
    return 0, None


def rule_salary_band(row, ctx):
    lo, hi = row["salary_min"], row["salary_max"]
    if lo and hi and lo > 0:
        if hi / lo > 3:
            return 20, f"widelki absurdalnie szerokie ({lo:.0f}-{hi:.0f})"
        if hi / lo > 2:
            return 12, f"widelki bardzo szerokie ({lo:.0f}-{hi:.0f})"
    return 0, None


def rule_no_salary(row, ctx):
    """Slaby sygnal i coraz slabszy - od 2026 brak widelek to raczej
    lamanie przepisow niz dowod widma. Zostawiamy nisko."""
    if not row["salary_min"] and not row["salary_max"]:
        return 5, "brak widelek"
    return 0, None


def rule_vague_title(row, ctx):
    t = normalize_text(row["title"])
    if t in VAGUE_TITLES and len(normalize_text(row["description"])) < 400:
        return 10, "ogolny tytul + krotki opis"
    return 0, None


def rule_company_spam(row, ctx):
    """Firma wrzuca dziesiatki niemal identycznych ogloszen naraz."""
    n = ctx["company_hash_counts"].get((row["company"], row["content_hash"]), 1)
    if n >= 5:
        return 12, f"ta sama tresc w {n} lokalizacjach naraz"
    return 0, None


RULES = [rule_reposts, rule_days_live, rule_agency, rule_salary_band,
         rule_no_salary, rule_vague_title, rule_company_spam]


def build_context(conn):
    hash_counts = {}
    for r in conn.execute(
        "SELECT content_hash, COUNT(DISTINCT external_id) n FROM offers GROUP BY content_hash"
    ):
        hash_counts[r["content_hash"]] = r["n"]

    company_hash_counts = {}
    for r in conn.execute(
        """SELECT company, content_hash, COUNT(DISTINCT external_id) n
           FROM offers WHERE company IS NOT NULL GROUP BY company, content_hash"""
    ):
        company_hash_counts[(r["company"], r["content_hash"])] = r["n"]

    days_live = {}
    for r in conn.execute(
        """SELECT source, external_id, MIN(seen_date) a, MAX(seen_date) b
           FROM sightings GROUP BY source, external_id"""
    ):
        d = (datetime.fromisoformat(r["b"]) - datetime.fromisoformat(r["a"])).days + 1
        days_live[(r["source"], r["external_id"])] = d

    return {"hash_counts": hash_counts, "days_live": days_live,
            "company_hash_counts": company_hash_counts}


def score_row(row, ctx):
    total, reasons = 0, []
    for rule in RULES:
        pts, why = rule(row, ctx)
        if pts:
            total += pts
            reasons.append({"pts": pts, "why": why})
    return min(total, 100), reasons


def main():
    conn = connect()
    ctx = build_context(conn)
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for row in conn.execute("SELECT * FROM offers").fetchall():
        s, reasons = score_row(row, ctx)
        conn.execute(
            """INSERT INTO scores (source, external_id, ghost_score, ghost_reasons, scored_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(source, external_id) DO UPDATE SET
                 ghost_score=excluded.ghost_score,
                 ghost_reasons=excluded.ghost_reasons,
                 scored_at=excluded.scored_at""",
            (row["source"], row["external_id"], s,
             json.dumps(reasons, ensure_ascii=False), now),
        )
        n += 1
    conn.commit()

    buckets = conn.execute(
        """SELECT CASE WHEN ghost_score >= 50 THEN 'wysokie'
                       WHEN ghost_score >= 25 THEN 'srednie'
                       ELSE 'niskie' END b, COUNT(*) n
           FROM scores GROUP BY b"""
    ).fetchall()
    print(f"[score] ocenionych: {n}")
    for r in buckets:
        print(f"[score]   ryzyko {r['b']:8s}: {r['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
