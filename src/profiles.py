"""Zarzadzanie profilami z linii polecen (GUI robi to samo klikaniem).

    python3 profiles.py --seed          # dwa przykladowe profile
    python3 profiles.py --list
"""
import argparse
import sys
from datetime import datetime

from common import connect, dump_config, profile_config

SEED = [
    ("Siostra - ksiegowosc", {
        "must_titles": ["ksiegow", "accountant", "ksiegowosc", "rozliczen", "finansow"],
        "levels": ["samodzielna", "samodzielny", "glowna", "specjalista"],
        "nice_to_have": ["symfonia", "optima", "sap", "pelna ksiegowosc",
                         "bilans", "angielski", "biuro rachunkowe"],
        "blacklist": ["praca zmianowa", "delegacje", "wlasna dzialalnosc",
                      "staz bezplatny", "prowizyjny"],
        "cities": ["Krakow", "Kraków"],
        "min_salary": 6000,
    }),
    ("Kolega - magazyn", {
        "must_titles": ["magazynier", "magazyn", "operator wozka",
                        "pracownik magazynu", "logistyk"],
        "levels": [],
        "nice_to_have": ["uprawnienia udt", "wozek widlowy", "wms",
                         "jedna zmiana", "bez zmianowosci"],
        "blacklist": ["agencja pracy tymczasowej", "umowa zlecenie"],
        "cities": ["Krakow", "Kraków", "Wieliczka", "Skawina"],
        "min_salary": 5000,
    }),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    conn = connect()

    if args.seed:
        now = datetime.now().isoformat(timespec="seconds")
        for name, cfg in SEED:
            conn.execute(
                "INSERT OR IGNORE INTO profiles (name, config, active, created_at) VALUES (?,?,1,?)",
                (name, dump_config(cfg), now))
        conn.commit()
        print(f"[profiles] dodano {len(SEED)} profili")

    for p in conn.execute("SELECT id, name, active FROM profiles ORDER BY id"):
        cfg = profile_config(conn.execute(
            "SELECT config FROM profiles WHERE id=?", (p["id"],)).fetchone()["config"])
        print(f"  [{p['id']}] {p['name']:24s} aktywny={bool(p['active'])} "
              f"miasta={cfg.get('cities')} min={cfg.get('min_salary')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
