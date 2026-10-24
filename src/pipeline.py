"""Caly dzienny przebieg: scrapery -> raw/ -> ingest -> geo -> score -> match.

    python3 pipeline.py                  # wszystkie scrapery
    python3 pipeline.py --only pracuj    # tylko jeden
    python3 pipeline.py --limit 100

Scraper moze pasc - reszta i tak sie wykona. Stan ostatniego przebiegu laduje
w pipeline_status.json, z ktorego GUI pokazuje "ostatnio pobrano".
Blokada (plik .lock) pilnuje, zeby nie odpalic dwoch przebiegow naraz.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from common import DATA_DIR, RAW_DIR, STATUS_PATH

HERE = Path(__file__).resolve().parent
SCRAPERS = ["pracuj", "rocketjobs", "olx"]
POST_STEPS = ("ingest", "geo", "score", "match")
LOCK = DATA_DIR / "pipeline.lock"
LOCK_STALE = 3 * 3600      # sekund; po tym czasie uznajemy blokade za martwa


def now():
    return datetime.now().isoformat(timespec="seconds")


def write_status(st):
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def read_status():
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_running():
    try:
        return time.time() - LOCK.stat().st_mtime < LOCK_STALE
    except OSError:
        return False


PROGRESS_RE = re.compile(r"^\[progress\] (\d+)/(\d+)")


def run_step(cmd, out=None, st=None, name=None):
    """Zwraca (kod wyjscia, ostatnie linie stderr).

    stderr czytamy na biezaco: linie "[progress] i/n" ida do pipeline_status.json
    (pasek postepu w GUI), reszta na nasz stderr.
    """
    if out:      # scraper: stdout (JSONL) do pliku, czytamy stderr
        p = subprocess.Popen([sys.executable, *cmd], cwd=HERE, stdout=out,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        stream = p.stderr
    else:        # reszta: stdout i stderr w jednym strumieniu
        p = subprocess.Popen([sys.executable, *cmd], cwd=HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        stream = p.stdout
    err, last_write = [], 0.0
    for ln in stream:
        ln = ln.rstrip()
        m = PROGRESS_RE.match(ln)
        if m:
            if st is not None and time.time() - last_write > 1:
                st["current"] = {"step": name, "done": int(m[1]), "total": int(m[2])}
                write_status(st)
                last_write = time.time()
            continue
        if ln.strip():
            err.append(ln)
            print(ln, file=sys.stderr)
    return p.wait(), err[-3:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=SCRAPERS)
    ap.add_argument("--limit", type=int, default=int(os.environ.get("JOBWATCH_LIMIT", 300)))
    args = ap.parse_args()

    if is_running():
        print("[pipeline] juz trwa inny przebieg", file=sys.stderr)
        return 2
    LOCK.write_text(str(os.getpid()))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    day = date.today().isoformat()
    scrapers = args.only or SCRAPERS
    st = {"started": now(), "finished": None, "running": True, "steps": {},
          "order": scrapers + list(POST_STEPS), "current": None}
    write_status(st)
    try:
        for name in scrapers:
            st["current"] = {"step": name, "done": 0, "total": 0}
            write_status(st)
            final = RAW_DIR / f"{day}_{name}.jsonl"
            tmp = final.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                # pracuj i olx ograniczaja sie liczba stron na fraze; limit uciąłby dalsze frazy
                extra = [] if name in ("pracuj", "olx") else ["--limit", str(args.limit)]
                rc, tail = run_step([f"{name}.py", *extra], out=fh, st=st, name=name)
            n = sum(1 for _ in open(tmp, encoding="utf-8"))
            if rc == 0 and n:
                tmp.replace(final)
            else:
                tmp.unlink(missing_ok=True)
            st["steps"][name] = {"ok": rc == 0 and n > 0, "n": n, "log": tail}
            write_status(st)

        for step in POST_STEPS:
            st["current"] = {"step": step, "done": 0, "total": 0}
            write_status(st)
            cmd = [f"{step}.py"] + (["--date", day] if step == "ingest" else [])
            rc, tail = run_step(cmd, st=st, name=step)
            st["steps"][step] = {"ok": rc == 0, "log": tail}
            write_status(st)
    finally:
        st["running"] = False
        st["current"] = None
        st["finished"] = now()
        write_status(st)
        LOCK.unlink(missing_ok=True)
    print("[pipeline] koniec", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
