"""Dopasowanie liczone osobno dla kazdego aktywnego profilu.

    python3 match.py              # wszystkie profile
    python3 match.py --profile 2  # jeden

Scraping i scoring widma sa wspolne - oferta to oferta. Dopiero tutaj
kazda osoba dostaje swoj wlasny widok tych samych danych.
"""
import argparse
import json
import sys
from datetime import datetime

import geo
from common import connect, normalize_text

DEFAULT_RADIUS = 30
# Tylko jednoznaczna praca zdalna. "Hybrydowa" albo "mozliwosc pracy zdalnej
# 2 dni w tygodniu" oznacza dojazdy, wiec nie zwalnia z filtra miast.
REMOTE_PHRASES = ("praca zdalna", "full remote", "fully remote", "w pelni zdaln",
                  "calkowicie zdaln", "tylko zdaln", "remote only")


def is_remote(row, blob):
    city = normalize_text(row["city"])
    if any(w in city for w in ("zdalnie", "zdalna", "remote", "cala polska")):
        return True
    # pracuj.pl wypisuje tryby pracy obok siebie: "Praca stacjonarna, Praca zdalna"
    # to zwykle "zalezy od wariantu", wiec liczymy tylko czysta zdalna
    if "praca stacjonarna" in blob or "praca hybrydowa" in blob:
        return False
    return any(p in blob for p in REMOTE_PHRASES)


class Locator:
    """Sprawdza, czy oferta lezy w promieniu od ktoregos z miast profilu."""

    def __init__(self, conn, prof):
        self.conn = conn
        self.cities = [c for c in prof.get("cities") or [] if c.strip()]
        self.norm = {normalize_text(c) for c in self.cities}
        self.radius = prof.get("radius_km", DEFAULT_RADIUS) or 0
        # miasta profilu jest kilka - te mozna geokodowac na zywo
        self.centers = ([p for p in (geo.lookup(conn, c) for c in self.cities) if p]
                        if conn is not None and self.radius else [])

    def check(self, row):
        """None = pasuje (z powodem w self.why), str = powod odrzucenia."""
        self.why = None
        best = None
        for place in geo.places(row["city"]):
            name = place.split(",")[0]
            if normalize_text(name) in self.norm:
                self.why = f"miasto: {name}"
                return None
            pt = geo.cached(self.conn, place) if self.centers else None
            if pt:
                d = min(geo.km(pt, c) for c in self.centers)
                if d <= self.radius and (best is None or d < best[0]):
                    best = (d, name)
        if best:
            self.why = f"{best[1]}: {best[0]:.0f} km"
            return None
        return f"odrzucone: lokalizacja {row['city'] or 'nieznana'}"


def match_row(row, prof: dict, loc: Locator | None = None):
    """Zwraca (score, reasons). score < 0 = odrzucone."""
    blob = normalize_text(f"{row['title']} {row['description']} {row['company']}")
    title = normalize_text(row["title"])
    reasons = []

    for kw in prof.get("blacklist", []):
        if normalize_text(kw) in blob:
            return -1, [f"odrzucone: {kw}"]

    if prof.get("cities"):
        loc = loc or Locator(None, prof)
        if is_remote(row, blob) and prof.get("remote_ok", True):
            reasons.append("zdalnie")
        else:
            why = loc.check(row)
            if why:
                return -1, [why]
            reasons.append(loc.why)

    hit = False
    for kw in prof.get("must_titles", []):
        if normalize_text(kw) in title:
            reasons.append(f"stanowisko: {kw}")
            hit = True
            break
    if prof.get("must_titles") and not hit:
        return -1, ["odrzucone: inne stanowisko"]
    score = 40 if hit else 20

    for l in prof.get("levels", []):
        if normalize_text(l) in title or normalize_text(l) in blob:
            score += 15
            reasons.append(f"poziom: {l}")
            break

    for kw in prof.get("nice_to_have", []):
        if normalize_text(kw) in blob:
            score += 8
            reasons.append(f"+ {kw}")

    lo = row["salary_min"]
    minimum = prof.get("min_salary") or 0
    if lo and minimum:
        if lo >= minimum:
            score += 15
            reasons.append(f"widelki od {lo:.0f}")
        elif lo < minimum * 0.8:
            score -= 20
            reasons.append(f"stawka ponizej oczekiwan ({lo:.0f})")

    return max(0, min(score, 100)), reasons


def run_profile(conn, pid, name, prof):
    now = datetime.now().isoformat(timespec="seconds")
    kept = dropped = 0
    loc = Locator(conn, prof)
    for row in conn.execute("SELECT * FROM offers").fetchall():
        s, reasons = match_row(row, prof, loc)
        conn.execute(
            """INSERT INTO match_results (profile_id, source, external_id, score, reasons, scored_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(profile_id, source, external_id) DO UPDATE SET
                 score=excluded.score, reasons=excluded.reasons, scored_at=excluded.scored_at""",
            (pid, row["source"], row["external_id"], s,
             json.dumps(reasons, ensure_ascii=False), now),
        )
        dropped += s < 0
        kept += s >= 0
    print(f"[match] {name:18s} pasuje={kept:5d} odrzucone={dropped:5d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", type=int)
    args = ap.parse_args()

    conn = connect()
    q = "SELECT id, name, config FROM profiles WHERE active=1"
    params = ()
    if args.profile:
        q += " AND id=?"
        params = (args.profile,)

    profs = conn.execute(q, params).fetchall()
    if not profs:
        print("[match] brak profili. Odpal: python3 profiles.py --seed", file=sys.stderr)
        return 1
    for p in profs:
        run_profile(conn, p["id"], p["name"], json.loads(p["config"]))
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
