"""raw/*.jsonl -> SQLite. Waliduje, liczy hash, prowadzi historie.

Idempotentny: mozesz puscic dwa razy na tych samych plikach.
Uruchomienie:  python3 ingest.py [--date YYYY-MM-DD]
"""
import argparse
import json
import re
import sys
from datetime import date, datetime

from common import (QUARANTINE_DIR, RAW_DIR, connect, content_hash,
                    iter_jsonl, work_mode)

REQUIRED = ["schema_version", "source", "external_id", "url", "title", "fetched_at"]
REQUIRED_SEEN = ["schema_version", "source", "external_id", "fetched_at"]


def validate(rec: dict) -> str | None:
    """Zwraca powod odrzucenia albo None. Celowo recznie, bez jsonschema."""
    if rec.get("seen_only"):
        for f in REQUIRED_SEEN:
            if not rec.get(f):
                return f"brak pola: {f}"
        return None
    for f in REQUIRED:
        if not rec.get(f):
            return f"brak pola: {f}"
    if rec["schema_version"] != 1:
        return f"nieznana schema_version: {rec['schema_version']}"
    if rec.get("work_modes") is not None and not isinstance(rec["work_modes"], list):
        return "work_modes nie jest lista"
    if not re.match(r"^https?://", str(rec["url"])):
        return "url nie jest http(s)"
    for f in ("salary_min", "salary_max"):
        if rec.get(f) is not None and not isinstance(rec[f], (int, float)):
            return f"{f} nie jest liczba"
    return None


def ingest_file(conn, path, seen_date):
    ok = bad = new = 0
    rejects = []
    for lineno, line in iter_jsonl(path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            rejects.append({"line": lineno, "why": f"zly JSON: {e}", "raw": line[:500]})
            bad += 1
            continue

        why = validate(rec)
        if why:
            rejects.append({"line": lineno, "why": why, "raw": line[:500]})
            bad += 1
            continue

        key = (rec["source"], str(rec["external_id"]))
        now = rec["fetched_at"]

        existing = conn.execute(
            "SELECT first_seen FROM offers WHERE source=? AND external_id=?", key
        ).fetchone()

        # Rekord "widzialem, nadal wisi" - z sitemapy, bez pobierania szczegolow.
        # Aktualizuje tylko historie, nie nadpisuje pol gorszymi danymi.
        if rec.get("seen_only"):
            if existing:
                conn.execute(
                    "UPDATE offers SET last_seen=?, times_seen=times_seen+1 "
                    "WHERE source=? AND external_id=?", (now, *key))
                conn.execute(
                    "INSERT OR IGNORE INTO sightings (source, external_id, seen_date) "
                    "VALUES (?,?,?)", (*key, seen_date))
                ok += 1
            else:
                bad += 1
                rejects.append({"line": lineno, "why": "seen_only dla nieznanej oferty",
                                "raw": line[:200]})
            continue

        h = content_hash(rec)
        mode = work_mode(rec)

        if existing:
            conn.execute(
                """UPDATE offers SET content_hash=?, url=?, title=?, company=?, city=?,
                   salary_min=?, salary_max=?, currency=?, contract=?, description=?,
                   published_at=?, work_mode=?, last_seen=?, times_seen=times_seen+1
                   WHERE source=? AND external_id=?""",
                (h, rec["url"], rec["title"], rec.get("company"), rec.get("city"),
                 rec.get("salary_min"), rec.get("salary_max"), rec.get("currency"),
                 rec.get("contract"), rec.get("description"), rec.get("published_at"),
                 mode, now, *key),
            )
        else:
            conn.execute(
                """INSERT INTO offers (source, external_id, content_hash, url, title,
                   company, city, salary_min, salary_max, currency, contract,
                   description, published_at, work_mode, first_seen, last_seen, times_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (*key, h, rec["url"], rec["title"], rec.get("company"), rec.get("city"),
                 rec.get("salary_min"), rec.get("salary_max"), rec.get("currency"),
                 rec.get("contract"), rec.get("description"), rec.get("published_at"),
                 mode, now, now),
            )
            new += 1

        conn.execute(
            "INSERT OR IGNORE INTO sightings (source, external_id, seen_date) VALUES (?,?,?)",
            (*key, seen_date),
        )
        ok += 1

    if rejects:
        QUARANTINE_DIR.mkdir(exist_ok=True)
        out = QUARANTINE_DIR / (path.stem + ".rejects.jsonl")
        with open(out, "w", encoding="utf-8") as fh:
            for r in rejects:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    return ok, bad, new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    files = sorted(RAW_DIR.glob(f"{args.date}_*.jsonl"))
    if not files:
        print(f"[ingest] brak plikow dla {args.date} w {RAW_DIR}", file=sys.stderr)
        return 1

    conn = connect()
    total_new = 0
    for path in files:
        ok, bad, new = ingest_file(conn, path, args.date)
        source = path.stem.split("_", 1)[1]
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_at, source, n_records) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), source, ok),
        )
        total_new += new
        flag = "  <-- ZERO, sprawdz scraper" if ok == 0 else ""
        print(f"[ingest] {source:14s} ok={ok:5d} odrzucone={bad:3d} nowe={new:4d}{flag}")

    conn.commit()
    print(f"[ingest] razem nowych ofert: {total_new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
