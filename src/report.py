"""Raport dzienny: HTML + opcjonalny mail.

python3 report.py              -> zapisuje report.html
python3 report.py --send       -> wysyla mailem wg config.json
python3 report.py --all        -> nie filtruje juz wyslanych
"""
import argparse
import json
import smtplib
import sys
from datetime import date, datetime
from email.message import EmailMessage
from html import escape

from common import ROOT, connect, load_config

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:0 auto;padding:16px;color:#1a1a1a}
h2{font-size:17px;margin:24px 0 8px;padding-bottom:6px;border-bottom:2px solid #eee}
.o{padding:12px;margin:8px 0;border:1px solid #e3e3e3;border-radius:8px}
.o a{font-size:16px;font-weight:600;color:#0b5cd5;text-decoration:none}
.meta{font-size:13px;color:#666;margin-top:4px}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:10px;margin-right:5px}
.new{background:#d8f5dd;color:#0a6b24}
.g-hi{background:#ffd9d9;color:#8b0000}
.g-mid{background:#fff0cc;color:#8a5b00}
.why{font-size:12px;color:#8a5b00;margin-top:5px}
.empty{color:#888;font-style:italic}
"""


def fetch(conn, pid, show_delivered):
    q = """
    SELECT o.*, COALESCE(s.ghost_score,0) AS ghost_score, s.ghost_reasons,
           m.score AS match_score,
           (SELECT COUNT(*) FROM sightings g
             WHERE g.source=o.source AND g.external_id=o.external_id) AS days_seen,
           (d.external_id IS NOT NULL) AS was_sent,
           COALESCE(fb.status,'nowa') AS status
    FROM offers o
    JOIN match_results m ON m.source=o.source AND m.external_id=o.external_id
                         AND m.profile_id=?
    LEFT JOIN scores s ON s.source=o.source AND s.external_id=o.external_id
    LEFT JOIN delivered d ON d.source=o.source AND d.external_id=o.external_id
    LEFT JOIN feedback fb ON fb.source=o.source AND fb.external_id=o.external_id
                          AND fb.profile_id=?
    WHERE m.score >= 0 AND COALESCE(fb.status,'nowa') NOT IN ('nieciekawa','odrzucona')
    ORDER BY days_seen ASC, s.ghost_score ASC, m.score DESC
    """
    rows = conn.execute(q, (pid, pid)).fetchall()
    if not show_delivered:
        rows = [r for r in rows if not r["was_sent"]]
    return rows


def render_offer(r):
    badges = ""
    if r["days_seen"] <= 1:
        badges += '<span class="badge new">NOWA</span>'
    g = r["ghost_score"] or 0
    if g >= 50:
        badges += f'<span class="badge g-hi">WIDMO {g}%</span>'
    elif g >= 25:
        badges += f'<span class="badge g-mid">ryzyko {g}%</span>'

    sal = ""
    if r["salary_min"]:
        sal = f" · {r['salary_min']:.0f}"
        if r["salary_max"]:
            sal += f"–{r['salary_max']:.0f}"
        sal += " zl"

    why = ""
    if g >= 25 and r["ghost_reasons"]:
        reasons = [x["why"] for x in json.loads(r["ghost_reasons"])]
        why = f'<div class="why">⚠ {escape(", ".join(reasons))}</div>'

    return f"""<div class="o">{badges}
<a href="{escape(r['url'])}">{escape(r['title'])}</a>
<div class="meta">{escape(r['company'] or '—')} · {escape(r['city'] or '—')}{sal}
 · {escape(r['source'])} · widoczne {r['days_seen']} dni</div>{why}</div>"""


def build_html(rows, profile_name=""):
    fresh = [r for r in rows if r["days_seen"] <= 1]
    solid = [r for r in rows if r["days_seen"] > 1 and (r["ghost_score"] or 0) < 25]
    risky = [r for r in rows if r["days_seen"] > 1 and (r["ghost_score"] or 0) >= 25]

    def section(title, items, limit=None):
        if not items:
            return f"<h2>{title}</h2><p class='empty'>brak</p>"
        shown = items[:limit] if limit else items
        body = "".join(render_offer(r) for r in shown)
        more = f"<p class='empty'>…i {len(items)-len(shown)} wiecej</p>" if limit and len(items) > limit else ""
        return f"<h2>{title} ({len(items)})</h2>{body}{more}"

    return f"""<!doctype html><html lang="pl"><meta charset="utf-8">
<style>{CSS}</style><body>
<h1 style="font-size:20px">{escape(profile_name)} — {date.today().isoformat()}</h1>
{section("🟢 Nowe dzisiaj", fresh)}
{section("Pozostale pasujace", solid, limit=25)}
{section("🔴 Podejrzane o widmo", risky, limit=10)}
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--all", action="store_true", help="nie pomijaj juz wyslanych")
    ap.add_argument("--profile", type=int)
    args = ap.parse_args()

    cfg = load_config()
    conn = connect()
    profs = conn.execute("SELECT id, name FROM profiles WHERE active=1").fetchall()
    if args.profile:
        profs = [p for p in profs if p["id"] == args.profile]
    if not profs:
        print("[report] brak profili", file=sys.stderr)
        return 1
    prof = profs[0]
    rows = fetch(conn, prof["id"], show_delivered=args.all)
    html = build_html(rows, prof["name"])

    out = ROOT / f"report_{prof['id']}.html"
    out.write_text(html, encoding="utf-8")
    print(f"[report] {len(rows)} ofert -> {out}")

    if args.send:
        m = cfg["mail"]
        msg = EmailMessage()
        msg["Subject"] = f"Oferty pracy — {date.today().isoformat()} ({len(rows)})"
        msg["From"], msg["To"] = m["from"], m["to"]
        msg.set_content("Wersja HTML wymagana.")
        msg.add_alternative(html, subtype="html")
        with smtplib.SMTP_SSL(m["smtp_host"], m.get("smtp_port", 465)) as s:
            s.login(m["user"], m["password"])
            s.send_message(msg)
        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            "INSERT OR IGNORE INTO delivered (source, external_id, sent_at) VALUES (?,?,?)",
            [(r["source"], r["external_id"], now) for r in rows],
        )
        conn.commit()
        print(f"[report] wyslano do {m['to']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
