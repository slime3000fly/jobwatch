"""GUI. Jeden plik, zero frontendowych zaleznosci.

    pip install flask
    python3 app.py                       # tylko dla ciebie: localhost:5000
    python3 app.py --host 0.0.0.0        # dostepne w sieci lokalnej / przez Tailscale

Kazdy profil ma wlasny widok tych samych ofert. Oznaczenia sa per profil,
wiec siostra i kolega nie wchodza sobie w droge.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

from urllib.parse import quote

from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import cv as cvmod
import pipeline
from common import DATA_DIR, ROOT, connect, dump_config, profile_config

app = Flask(__name__)
# Za dokladnie jednym nginx-em: remote_addr = ostatni wpis X-Forwarded-For,
# czyli ten dopisany przez nginx. Wczesniejsze wpisy podaje klient - nie ufamy im.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Sekret sesji trwaly miedzy restartami - inaczej wszyscy sie wylogowuja.
# O_EXCL: przy pierwszym starcie kilka workerow gunicorna moze tu wejsc naraz -
# klucz zapisuje tylko pierwszy, reszta czyta jego.
_sk = DATA_DIR / ".secret_key"
try:
    _fd = os.open(_sk, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(_fd, os.urandom(32))
    os.close(_fd)
except FileExistsError:
    pass
for _ in range(20):          # inny worker moze byc wlasnie w trakcie zapisu
    app.secret_key = _sk.read_bytes()
    if len(app.secret_key) >= 32:
        break
    time.sleep(0.05)
# SECURE wymaga HTTPS. Na publicznym adresie ZAWSZE 1, inaczej cookie leci
# otwartym tekstem. Wylaczyc tylko do testow na localhoscie.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("JOBWATCH_HTTPS", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# Kod zaproszenia. Pusty = rejestracja otwarta dla kazdego z internetu.
# Domyslnie WLACZONY - patrz README, sekcja "Skala".
INVITE_CODE = os.environ.get("JOBWATCH_INVITE", "zmien-mnie")

PUBLIC = {"login", "register", "static"}

# Porownanie hasla takze dla nieistniejacego konta - inaczej czas odpowiedzi
# zdradza, ktore e-maile sa zarejestrowane.
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


def safe_next(target, default="/"):
    """Tylko sciezki w obrebie serwisu. '//evil.com' i '/\\evil.com' to juz inna domena."""
    if (not target or not target.startswith("/")
            or target[1:2] in ("/", "\\") or any(c in target for c in "\r\n")):
        return default
    return target


def safe_url(u):
    """Linki do ofert pochodza ze scrapowanych stron - tylko http(s), zadnego javascript:."""
    u = (u or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else "#"


app.jinja_env.filters["safe_url"] = safe_url


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    # Skrypty i style sa inline, stad 'unsafe-inline'; reszta zablokowana.
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                 "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                 "object-src 'none'; base-uri 'none'; form-action 'self'; "
                 "frame-ancestors 'none'")
    if app.config["SESSION_COOKIE_SECURE"]:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp

# --- CSRF: token w sesji, sprawdzany przy kazdym POST ---


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method != "POST":
        return None
    sent = request.form.get("_csrf", "")
    good = session.get("csrf", "")
    if not good or not secrets.compare_digest(sent, good):
        return ("Sesja wygasla albo formularz zostal wyslany z innej strony. "
                "<a href='/'>Odswiez i sprobuj jeszcze raz</a>.", 400)
    return None


# --- limit prob logowania: na e-mail i na IP, w pamieci procesu ---
ATTEMPTS: dict[str, list[float]] = defaultdict(list)
MAX_TRIES = 8
WINDOW = 15 * 60       # sekund
LOCKOUT_MSG = ("Za dużo prób logowania. Odczekaj kwadrans i spróbuj ponownie.")


MAX_KEYS = 20_000      # zeby zalew losowymi e-mailami nie zjadl pamieci


def too_many(*keys) -> bool:
    now = time.time()
    if len(ATTEMPTS) > MAX_KEYS:
        for k in [k for k, ts in ATTEMPTS.items() if not ts or now - ts[-1] >= WINDOW]:
            del ATTEMPTS[k]
    for k in keys:
        ATTEMPTS[k] = [t for t in ATTEMPTS[k] if now - t < WINDOW]
        if len(ATTEMPTS[k]) >= MAX_TRIES:
            return True
    return False


def note_attempt(*keys):
    now = time.time()
    for k in keys:
        ATTEMPTS[k].append(now)


def client_ip() -> str:
    """Prawdziwe IP ustawia ProxyFix z naglowka dopisanego przez nginx."""
    return request.remote_addr or "?"


def uid():
    return session.get("uid")


@app.before_request
def require_login():
    if request.endpoint in PUBLIC or uid():
        return None
    return redirect(url_for("login", next=request.path))


def own_profile(conn, pid):
    """Profil musi nalezec do zalogowanego konta - inaczej None."""
    return conn.execute("SELECT * FROM profiles WHERE id=? AND user_id=?",
                        (pid, uid())).fetchone()

STATUSES = ["nowa", "zapisana", "wyslana", "rozmowa", "odrzucona", "cisza", "nieciekawa"]
HIDDEN = {"nieciekawa", "odrzucona"}

STATUS_LABELS = {"nowa": "Nowa", "zapisana": "Zapisana", "wyslana": "Wysłana",
                 "rozmowa": "Rozmowa", "odrzucona": "Odrzucona", "cisza": "Cisza",
                 "nieciekawa": "Nieciekawa"}
SOURCE_LABELS = {"pracuj": "pracuj.pl", "rocketjobs": "RocketJobs", "olx": "OLX"}
STEP_LABELS = {**SOURCE_LABELS, "ingest": "zapis do bazy", "geo": "lokalizacje",
               "score": "ocena widm", "match": "dopasowanie"}

STYLE = """<style>
:root{
  --bg:#f5f4f0;--surface:#fff;--surface-2:#faf9f6;--line:#e7e4dc;--line-2:#d8d4c9;
  --text:#1d1c19;--muted:#6e6a60;--faint:#9a958a;
  --accent:#2f5d50;--accent-ink:#fff;--accent-soft:#e3eee9;
  --good:#1f7a4d;--good-soft:#e1f3e8;--warn:#9a6200;--warn-soft:#fdf1d8;
  --bad:#a8322a;--bad-soft:#fbe4e1;--new:#2c5fd6;--new-soft:#e5ecfd;
  --hyb:#7a3fb0;--hyb-soft:#f1e7fb;
  --shadow:0 1px 2px rgba(30,25,15,.05),0 2px 8px rgba(30,25,15,.04);
  --r:12px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#151513;--surface:#1e1e1b;--surface-2:#242420;--line:#2f2e2a;--line-2:#3d3c36;
  --text:#ecebe6;--muted:#a4a095;--faint:#77736a;
  --accent:#7fc4ad;--accent-ink:#10201b;--accent-soft:#1f3530;
  --good:#6fd39e;--good-soft:#1b3326;--warn:#f0b54e;--warn-soft:#3a2d14;
  --bad:#f08a80;--bad-soft:#3d1f1c;--new:#8fb0ff;--new-soft:#1e2a47;
  --hyb:#cfa6f5;--hyb-soft:#2f2140;
  --shadow:none;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--text);background:var(--bg)}
a{color:var(--accent)}
button,input,select,textarea{font:inherit;color:inherit}
.wrap{max-width:1320px;margin:0 auto;padding:0 24px}
.narrow{max-width:920px}

/* naglowek */
header{background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.bar{display:flex;align-items:center;gap:14px;height:56px}
.brand{font-weight:700;font-size:17px;letter-spacing:-.01em;text-decoration:none;color:var(--text);
  display:flex;align-items:center;gap:8px}
.brand i{width:26px;height:26px;border-radius:8px;background:var(--accent);display:grid;
  place-items:center;color:var(--accent-ink);font-style:normal;font-size:14px}
.tabs{display:flex;gap:2px;overflow-x:auto;flex:1;scrollbar-width:none}
.tabs a{padding:7px 12px;border-radius:8px;text-decoration:none;color:var(--muted);
  white-space:nowrap;font-size:14px;font-weight:500}
.tabs a:hover{background:var(--surface-2);color:var(--text)}
.tabs a.on{background:var(--accent-soft);color:var(--accent)}
.tabs a.add{color:var(--faint)}
.menu{display:flex;gap:4px;align-items:center}
.icon-btn{border:0;background:none;padding:7px 10px;border-radius:8px;color:var(--muted);
  text-decoration:none;font-size:14px;cursor:pointer}
.icon-btn:hover{background:var(--surface-2);color:var(--text)}

/* pasek pobierania */
.sync{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:18px 0 6px;
  font-size:13px;color:var(--muted)}
.sync .dot{width:8px;height:8px;border-radius:50%;background:var(--good);display:inline-block}
.sync .dot.run{background:var(--warn);animation:pulse 1s infinite alternate}
.sync .dot.err{background:var(--bad)}
@keyframes pulse{to{opacity:.3}}
.prog{flex-basis:100%;display:flex;align-items:center;gap:10px}
.prog-bar{flex:1;max-width:420px;height:6px;border-radius:6px;background:var(--line);overflow:hidden}
.prog-bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s}
.sync form{margin-left:auto}
.sync-detail{font-size:12px;color:var(--faint);margin-bottom:6px}

/* przyciski */
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line-2);
  background:var(--surface);padding:7px 13px;border-radius:9px;font-size:13.5px;font-weight:500;
  cursor:pointer;text-decoration:none;color:var(--text);line-height:1.2}
.btn:hover{border-color:var(--faint)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.ghost{border-color:transparent;background:none;color:var(--muted)}
.btn.ghost:hover{background:var(--surface-2);color:var(--text)}
.btn.on{background:var(--accent-soft);border-color:transparent;color:var(--accent)}
.btn:disabled{opacity:.5;cursor:default}
.btn.lg{padding:11px 20px;font-size:15px}

/* filtry */
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}
.seg{display:flex;gap:2px;background:var(--surface);border:1px solid var(--line);
  border-radius:10px;padding:3px;overflow-x:auto;max-width:100%}
.seg a{padding:6px 11px;border-radius:7px;text-decoration:none;color:var(--muted);
  font-size:13.5px;white-space:nowrap;font-weight:500}
.seg a:hover{color:var(--text)}
.seg a.on{background:var(--text);color:var(--surface)}
.seg a b{font-weight:500;opacity:.6;margin-left:3px}
.search{flex:1;min-width:180px;padding:8px 12px;border:1px solid var(--line);border-radius:10px;
  background:var(--surface)}
.search:focus,.field input:focus,.field textarea:focus{outline:2px solid var(--accent-soft);
  border-color:var(--accent)}

/* karta oferty */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:12px;
  margin:10px 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  box-shadow:var(--shadow);padding:16px 18px;display:flex;gap:16px;min-width:0}
.card.dim{opacity:.62}
.score{flex:none;width:52px;height:52px;border-radius:50%;display:grid;place-items:center;
  font-weight:700;font-size:16px;background:conic-gradient(var(--c) calc(var(--p)*1%),var(--line) 0)}
.score span{width:42px;height:42px;border-radius:50%;background:var(--surface);display:grid;
  place-items:center;color:var(--c)}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px}
.tag{font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;
  padding:2px 8px;border-radius:20px}
.tag.new{background:var(--new-soft);color:var(--new)}
.tag.bad{background:var(--bad-soft);color:var(--bad)}
.tag.warn{background:var(--warn-soft);color:var(--warn)}
.tag.st{background:var(--accent-soft);color:var(--accent)}
.tag.remote{background:var(--good-soft);color:var(--good)}
.tag.hybrid{background:var(--hyb-soft);color:var(--hyb)}
.tag.src{text-transform:none;letter-spacing:0;border:1px solid var(--line-2);color:var(--muted);
  background:var(--surface)}
.tag.src:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
  margin-right:5px;background:var(--faint);vertical-align:1px}
.src-pracuj:before{background:#0e6fd8!important}
.src-rocketjobs:before{background:#e8473a!important}
.src-olx:before{background:#23e5db!important}
.card h3{margin:0;font-size:16.5px;line-height:1.35;font-weight:600;letter-spacing:-.005em}
.card h3 a{color:var(--text);text-decoration:none}
.card h3 a:hover{color:var(--accent)}
.company{color:var(--muted);font-size:14px;margin-top:1px}
.facts{display:flex;gap:6px 16px;flex-wrap:wrap;margin-top:8px;font-size:13px;color:var(--muted)}
.facts .sal{color:var(--good);font-weight:600}
.why{font-size:12.5px;color:var(--warn);margin-top:8px}
.reasons{font-size:12.5px;color:var(--faint);margin-top:6px}
.chips{margin-top:10px;display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip{font-size:12px;padding:2px 9px;border-radius:20px;white-space:nowrap}
.chip.has{background:var(--good-soft);color:var(--good)}
.chip.miss{background:var(--surface-2);color:var(--faint);border:1px dashed var(--line-2)}
.chips-lbl{font-size:11.5px;color:var(--faint);margin:0 2px 0 4px}
.chips-lbl:first-child{margin-left:0}
.actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:auto;
  padding-top:12px;border-top:1px solid var(--line)}
.main>:nth-last-child(2){margin-bottom:12px}
.actions form{display:contents}
.actions select{margin-left:auto;padding:6px 8px;border:1px solid var(--line);border-radius:8px;
  background:var(--surface);font-size:13px;color:var(--muted)}

.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty h3{color:var(--text);margin:0 0 6px;font-size:17px}
.more{text-align:center;color:var(--faint);font-size:13px;padding:10px}

/* formularze */
h1.page{font-size:22px;letter-spacing:-.015em;margin:26px 0 4px}
.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  box-shadow:var(--shadow);padding:20px;margin:12px 0}
.panel h2{font-size:15px;margin:0 0 12px;display:flex;align-items:baseline;gap:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 18px}
.grid .full{grid-column:1/-1}
.field{display:block;margin-bottom:14px}
.field>span{display:block;font-size:13px;font-weight:600;margin-bottom:5px}
.field small,.hint{display:block;font-weight:400;color:var(--faint);font-size:12px;margin-top:4px}
.panel h2 .hint{display:inline;margin:0}
.field input,.field textarea{width:100%;padding:9px 11px;border:1px solid var(--line-2);
  border-radius:9px;background:var(--surface)}
.field textarea{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
  line-height:1.5;resize:vertical}
.field.check{display:flex;gap:9px;align-items:center;align-self:center}
.field.check input{width:auto;margin:0}
.field.check span{margin:0;font-weight:500}
.field input.bl{border-color:color-mix(in srgb,var(--bad) 35%,var(--line-2))}
.formbar{display:flex;gap:10px;align-items:center;margin:16px 0 30px}
details.raw summary{cursor:pointer;font-size:13px;color:var(--faint)}
details.raw pre{font-size:12px;background:var(--surface-2);padding:12px;border-radius:9px;overflow:auto}
.back{display:inline-block;margin-top:22px;font-size:13.5px;text-decoration:none;color:var(--muted)}
.back:hover{color:var(--text)}
.alert{border-radius:var(--r);padding:14px 16px;margin:12px 0;font-size:14px}
.alert.bad{background:var(--bad-soft);color:var(--bad)}
.alert b{display:block;margin-bottom:2px}
.steps{counter-reset:s}
.steps .panel h2:before{counter-increment:s;content:counter(s);width:22px;height:22px;
  border-radius:50%;background:var(--accent-soft);color:var(--accent);display:inline-grid;
  place-items:center;font-size:12px;flex:none;align-self:center}

/* logowanie */
.auth{min-height:100vh;display:grid;place-items:center;padding:20px}
.auth .panel{width:100%;max-width:380px;padding:28px}
.auth .brand{justify-content:center;margin-bottom:22px;font-size:19px}
.auth h1{font-size:18px;text-align:center;margin:0 0 18px}
.auth .btn{width:100%;justify-content:center}
.err{background:var(--bad-soft);color:var(--bad);padding:10px 12px;border-radius:9px;
  font-size:13.5px;margin-bottom:14px}
.alt{text-align:center;font-size:13.5px;margin-top:16px;color:var(--muted)}

@media (max-width:640px){
  .wrap{padding:0 16px}
  .cards{grid-template-columns:1fr}
  .bar{gap:8px}
  .brand b{display:none}
  .grid{grid-template-columns:1fr}
  .card{padding:14px;gap:12px}
  .score{width:44px;height:44px;font-size:14px}
  .score span{width:36px;height:36px}
  .actions select{margin-left:0}
}
</style>"""

BASE = """<!doctype html><html lang="pl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JobWatch</title>""" + STYLE + """<body>
<header><div class="wrap bar">
  <a class="brand" href="{{ url_for('index') }}"><i>J</i><b>JobWatch</b></a>
  <nav class="tabs">
    {% for p in profiles %}<a href="{{ url_for('view', pid=p['id']) }}"
      class="{{ 'on' if p['id']==pid }}">{{ p['name'] }}</a>{% endfor %}
    <a class="add" href="{{ url_for('new_profile') }}" title="Nowy profil">+ profil</a>
  </nav>
  <div class="menu">
    {% if pid %}<a class="icon-btn" href="{{ url_for('edit', pid=pid) }}"
      title="Ustawienia profilu">Ustawienia</a>{% endif %}
    <form method="post" action="{{ url_for('logout') }}" style="margin:0">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <button class="icon-btn" title="Wyloguj">Wyloguj</button></form>
  </div>
</div></header>
<main class="wrap">
{{ body|safe }}
</main>
</body></html>"""

LIST = """
<div class="sync">
  {% if sync.running %}
    <span class="dot run"></span> Pobieram nowe oferty… lista odświeży się sama po zakończeniu.
  {% elif sync.finished %}
    <span class="dot {{ 'err' if sync.failed }}"></span>
    Ostatnio pobrano {{ sync.finished }}{% if sync.summary %} · {{ sync.summary }}{% endif %}
  {% else %}
    <span class="dot err"></span> Jeszcze nic nie pobrano.
  {% endif %}
  <form method="post" action="{{ url_for('refresh') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="back" value="{{ request.full_path }}">
    <button class="btn {{ '' if sync.finished else 'primary' }}" {{ 'disabled' if sync.running }}>
      ↻ Pobierz oferty</button>
  </form>
</div>
{% if sync.running %}<div class="sync prog" id="prog">
  <div class="prog-bar"><i style="width:{{ sync.progress.pct }}%"></i></div>
  <span class="prog-txt">{{ sync.progress.text }}</span>
</div>
<script>
(function poll(){setTimeout(function(){
  fetch({{ url_for('status')|tojson }},{credentials:'same-origin'}).then(function(r){return r.json()})
  .then(function(s){
    if(!s.running){location.reload();return}
    var p=document.getElementById('prog');
    p.querySelector('i').style.width=s.progress.pct+'%';
    p.querySelector('.prog-txt').textContent=s.progress.text;
    poll();
  }).catch(poll);
},2000)})();
</script>{% endif %}
{% if sync.failed %}<div class="sync-detail">Problem: {{ sync.failed }}</div>{% endif %}

<div class="toolbar">
  <nav class="seg">
    {% for key, label in [('nowe','Nowe dziś'),('wszystkie','Wszystkie'),('pewne','Bez widm'),
                          ('zapisane','Zapisane'),('wyslane','Wysłane')] %}
    <a href="?f={{ key }}{{ '&src=' ~ src if src }}" class="{{ 'on' if f==key }}">{{ label }}<b>{{ counts[key] }}</b></a>
    {% endfor %}
  </nav>
  {% if src_counts|length > 1 %}<nav class="seg">
    <a href="?f={{ f }}" class="{{ 'on' if not src }}">Wszystkie źródła</a>
    {% for k, n in src_counts.items() %}<a href="?f={{ f }}&src={{ k }}"
      class="{{ 'on' if src==k }}">{{ sources.get(k, k) }}<b>{{ n }}</b></a>{% endfor %}
  </nav>{% endif %}
  {% if rows %}<input class="search" type="search" placeholder="Szukaj w wynikach…"
    oninput="filt(this.value)">{% endif %}
</div>

{% if not rows %}
<div class="empty">
  {% if counts.wszystkie == 0 %}
    <h3>Brak ofert dla tego profilu</h3>
    <p>Kliknij „Pobierz oferty” albo poluzuj kryteria w <a href="{{ url_for('edit', pid=pid) }}">ustawieniach</a>.</p>
  {% elif f == 'nowe' %}
    <h3>Dziś nic nowego</h3>
    <p>Zobacz <a href="?f=wszystkie">wszystkie oferty ({{ counts.wszystkie }})</a>.</p>
  {% else %}
    <h3>Pusto</h3><p>Nie ma tu jeszcze żadnych ofert.</p>
  {% endif %}
</div>
{% endif %}

<div class="cards">
{% for r in rows %}
<article class="card {{ 'dim' if r.status in hidden }}" data-q="{{ (r.title ~ ' ' ~ (r.company or '') ~ ' ' ~ (r.city or '') ~ ' ' ~ (r.work_mode or ''))|lower }}">
  <div class="score" style="--p:{{ r.match }};--c:{{ 'var(--good)' if r.match >= 60 else 'var(--warn)' if r.match >= 35 else 'var(--faint)' }}"
       title="Dopasowanie do profilu"><span>{{ r.match }}</span></div>
  <div class="main">
    <div class="tags">
      <span class="tag src src-{{ r.source }}">{{ sources.get(r.source, r.source) }}</span>
      {% if r.days_seen <= 1 %}<span class="tag new">Nowa</span>{% endif %}
      {% if r.work_mode and 'zdalna' in r.work_mode %}<span class="tag remote">Zdalna</span>{% endif %}
      {% if r.work_mode and 'hybrydowa' in r.work_mode %}<span class="tag hybrid">Hybrydowa</span>{% endif %}
      {% if r.ghost >= 50 %}<span class="tag bad">Widmo {{ r.ghost }}%</span>
      {% elif r.ghost >= 25 %}<span class="tag warn">Ryzyko widma {{ r.ghost }}%</span>{% endif %}
      {% if r.status != 'nowa' %}<span class="tag st">{{ labels[r.status] }}</span>{% endif %}
    </div>
    <h3><a href="{{ r.url|safe_url }}" target="_blank" rel="noopener">{{ r.title }}</a></h3>
    <div class="company">{{ r.company or 'Firma nieznana' }}</div>
    <div class="facts">
      {% if r.city %}<span>📍 {{ r.city }}</span>{% endif %}
      {% if r.sal %}<span class="sal">{{ r.sal }}</span>{% endif %}
      {% if r.contract %}<span>{{ r.contract }}</span>{% endif %}
      <span>widoczna {{ r.days_seen }} {{ 'dzień' if r.days_seen == 1 else 'dni' }}</span>
    </div>
    {% if r.ghost_why %}<div class="why">⚠ {{ r.ghost_why }}</div>{% endif %}
    {% if r.reasons %}<div class="reasons">{{ r.reasons }}</div>{% endif %}
    {% if r.has or r.miss %}<div class="chips">
      {% if r.has %}<span class="chips-lbl">Masz:</span>
        {% for c in r.has %}<span class="chip has">{{ c }}</span>{% endfor %}{% endif %}
      {% if r.miss %}<span class="chips-lbl">Brakuje:</span>
        {% for c in r.miss %}<span class="chip miss">{{ c }}</span>{% endfor %}{% endif %}
    </div>{% endif %}
    <div class="actions">
      <a class="btn primary"
        href="{{ url_for('apply_view', pid=pid, source=r.source, eid=r.external_id) }}">Aplikuj</a>
      <form method="post" action="{{ url_for('mark') }}">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <input type="hidden" name="pid" value="{{ pid }}">
        <input type="hidden" name="source" value="{{ r.source }}">
        <input type="hidden" name="external_id" value="{{ r.external_id }}">
        <input type="hidden" name="back" value="{{ request.full_path }}">
        {% if r.status == 'zapisana' %}
          <button class="btn on" name="status" value="nowa">★ Zapisana</button>
        {% else %}
          <button class="btn" name="status" value="zapisana">☆ Zapisz</button>
        {% endif %}
        <button class="btn ghost" name="status" value="nieciekawa">Ukryj</button>
        <select name="status" onchange="this.form.requestSubmit ? this.form.requestSubmit() : this.form.submit()"
                title="Status oferty">
          {% for s in statuses %}<option value="{{ s }}" {{ 'selected' if r.status==s }}>{{ labels[s] }}</option>{% endfor %}
        </select>
      </form>
    </div>
  </div>
</article>
{% endfor %}
</div>
{% if total > rows|length %}<p class="more">Pokazuję {{ rows|length }} z {{ total }} — zawęź filtry, żeby zobaczyć resztę.</p>{% endif %}
<script>
function filt(q){q=q.trim().toLowerCase();
  document.querySelectorAll('.card').forEach(function(c){
    c.style.display=!q||c.dataset.q.indexOf(q)>-1?'':'none'})}
</script>"""

EDIT = """<div class="narrow">
<h1 class="page">{{ title }}</h1>
<p class="sub">Na tej podstawie oferty są filtrowane i oceniane. Po zapisaniu wszystko przelicza się od razu.</p>
<form method="post">
  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
  <section class="panel">
    <h2>Czego szukasz</h2>
    <div class="grid">
      <label class="field full"><span>Nazwa profilu</span>
        <input name="name" value="{{ name }}" placeholder="np. Ania – księgowość" required></label>
      <label class="field full"><span>Stanowiska</span>
        <input name="must_titles" value="{{ f.must_titles }}" placeholder="księgowa, specjalista ds. rozliczeń">
        <small>Po przecinku. Te frazy są też wyszukiwane na pracuj.pl i OLX.</small></label>
      <label class="field"><span>Poziom</span>
        <input name="levels" value="{{ f.levels }}" placeholder="samodzielna, specjalista">
        <small>Opcjonalne, daje dodatkowe punkty.</small></label>
      <label class="field"><span>Miasta</span>
        <input name="cities" value="{{ f.cities }}" placeholder="Sieradz, Łódź">
        <small>Puste = cały kraj.</small></label>
      <label class="field"><span>Promień od miasta</span>
        <input name="radius_km" type="number" min="0" max="300" step="5" value="{{ f.radius_km }}">
        <small>km; 0 = tylko dokładnie te miasta</small></label>
      <label class="field check"><input type="checkbox" name="remote_ok" value="1"
        {{ 'checked' if f.remote_ok }}> <span>Pokazuj też pracę w pełni zdalną z innych miast</span></label>
      <label class="field"><span>Minimalne wynagrodzenie</span>
        <input name="min_salary" type="number" min="0" step="100" value="{{ f.min_salary }}">
        <small>zł brutto / mies., 0 = nie filtruj</small></label>
      <label class="field"><span>Mile widziane</span>
        <input name="nice_to_have" value="{{ f.nice_to_have }}" placeholder="Symfonia, angielski">
        <small>Programy, języki, uprawnienia.</small></label>
      <label class="field full"><span>Czego NIE chcesz</span>
        <input class="bl" name="blacklist" value="{{ f.blacklist }}" placeholder="praca zmianowa, delegacje, umowa zlecenie">
        <small>Twarde odrzucenie — oferta z którąkolwiek z tych fraz znika.</small></label>
    </div>
  </section>
  <section class="panel">
    <h2>CV <span class="hint">{% if n_terms %}rozpoznane kompetencje: {{ n_terms }}{% else %}opcjonalne{% endif %}</span></h2>
    <label class="field" style="margin:0">
      <textarea name="cv_text" rows="9"
        placeholder="Wklej tekst CV. Zostaje w twojej bazie i nigdzie nie jest wysyłany.">{{ f.cv_text }}</textarea>
      <small>Z CV powstają zielone kafelki „Masz / Brakuje” przy ofertach i szkic listu motywacyjnego.</small>
    </label>
  </section>
  <div class="formbar">
    <button class="btn primary lg">Zapisz profil</button>
    {% if pid %}<a class="btn ghost" href="{{ url_for('view', pid=pid) }}">Anuluj</a>{% endif %}
  </div>
</form>
<details class="raw"><summary>Podgląd konfiguracji (JSON)</summary><pre>{{ raw }}</pre></details>
<p style="height:30px"></p></div>"""


AUTH = """<!doctype html><html lang="pl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} · JobWatch</title>
""" + STYLE + """<body><div class="auth"><div class="panel">
<div class="brand"><i>J</i>JobWatch</div>
<h1>{{ title }}</h1>
{% if err %}<div class="err">{{ err }}</div>{% endif %}
<form method="post">
  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
  <label class="field"><span>E-mail</span><input name="email" type="email" required autofocus
    value="{{ email or '' }}"></label>
  <label class="field"><span>Hasło</span><input name="password" type="password" required
    minlength="8" placeholder="min. 8 znaków"></label>
  {% if invite %}<label class="field"><span>Kod zaproszenia</span>
    <input name="invite" required placeholder="od administratora"></label>{% endif %}
  <button class="btn primary lg">{{ title }}</button>
</form>
<div class="alt">{{ alt|safe }}</div>
</div></div></body></html>"""


APPLY = """<div class="narrow">
<a class="back" href="{{ url_for('view', pid=pid) }}">← Wróć do listy</a>
<h1 class="page">{{ o.title }}</h1>
<p class="sub">{{ o.company or 'Firma nieznana' }}{% if o.city %} · {{ o.city }}{% endif %}
  {% if o.work_mode %} · praca {{ o.work_mode }}{% endif %}
  · <a href="{{ o.url|safe_url }}" target="_blank" rel="noopener">zobacz ogłoszenie ↗</a></p>

{% if ghost >= 50 %}
<div class="alert bad"><b>⚠ Wysokie ryzyko ogłoszenia-widma ({{ ghost }}%)</b>
  {{ ghost_why }}. Możesz aplikować, ale nie inwestuj w długi list i nie licz na odpowiedź.</div>
{% endif %}

<div class="steps">
<section class="panel">
  <h2>Pokrycie wymagań</h2>
  {% if has or miss %}<div class="chips" style="margin:0">
    {% if has %}<span class="chips-lbl">Masz:</span>
      {% for c in has %}<span class="chip has">{{ c }}</span>{% endfor %}{% endif %}
    {% if miss %}<span class="chips-lbl">Brakuje:</span>
      {% for c in miss %}<span class="chip miss">{{ c }}</span>{% endfor %}{% endif %}
  </div>{% else %}<p class="hint" style="margin:0">
    Wklej CV w <a href="{{ url_for('edit', pid=pid) }}">ustawieniach profilu</a>, żeby zobaczyć porównanie.</p>{% endif %}
</section>

<section class="panel">
  <h2>Szkic listu <span class="hint">popraw miejsca w [nawiasach]</span></h2>
  <label class="field" style="margin:0"><textarea id="letter" rows="16">{{ letter }}</textarea></label>
  <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn" type="button" onclick="navigator.clipboard.writeText(
      document.getElementById('letter').value); this.textContent='✓ Skopiowano'">Kopiuj list</button>
    {% if email %}<a class="btn" href="mailto:{{ email }}?subject={{ subject }}">Otwórz w mailu ({{ email }})</a>{% endif %}
  </div>
</section>

<section class="panel">
  <h2>Wyślij i oznacz</h2>
  <p class="hint" style="margin:0 0 12px">Ogłoszenie otworzy się w nowej karcie, a oferta zostanie oznaczona jako wysłana.</p>
  <form method="post" action="{{ url_for('mark') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="pid" value="{{ pid }}">
    <input type="hidden" name="source" value="{{ o.source }}">
    <input type="hidden" name="external_id" value="{{ o.external_id }}">
    <input type="hidden" name="status" value="wyslana">
    <input type="hidden" name="back" value="{{ url_for('view', pid=pid) }}">
    <button class="btn primary lg" onclick="window.open({{ o.url|safe_url|tojson|forceescape }},'_blank')">
      Otwórz ogłoszenie i oznacz jako wysłane</button>
  </form>
</section>
</div></div>"""


LIST_FIELDS = ["must_titles", "levels", "cities", "nice_to_have", "blacklist"]


def cfg_to_form(cfg):
    f = {k: ", ".join(cfg.get(k) or []) for k in LIST_FIELDS}
    f["min_salary"] = cfg.get("min_salary") or 0
    f["radius_km"] = cfg.get("radius_km", 30)
    f["remote_ok"] = cfg.get("remote_ok", True)
    f["cv_text"] = cfg.get("cv_text") or ""
    return f


def form_to_cfg(form):
    cfg = {k: [x.strip() for x in form.get(k, "").split(",") if x.strip()]
           for k in LIST_FIELDS}
    for k, default in (("min_salary", 0), ("radius_km", 30)):
        try:
            cfg[k] = max(0, int(form.get(k) or default))
        except ValueError:
            cfg[k] = default
    cfg["remote_ok"] = form.get("remote_ok") == "1"
    cv_text = (form.get("cv_text") or "").strip()
    if cv_text:
        cfg["cv_text"] = cv_text
        cfg["cv_terms"] = sorted(cvmod.terms(cv_text))
    return cfg


def rematch(conn, pid):
    """Przelicza dopasowanie od razu po zapisie - bez wychodzenia do terminala."""
    import match as match_mod
    row = conn.execute("SELECT name, config FROM profiles WHERE id=?", (pid,)).fetchone()
    match_mod.run_profile(conn, pid, row["name"], profile_config(row["config"]))
    conn.commit()


def profiles(conn):
    return conn.execute(
        "SELECT id, name FROM profiles WHERE active=1 AND user_id=? ORDER BY id",
        (uid(),)).fetchall()


def fmt_money(v):
    return f"{v:,.0f}".replace(",", "\u00a0")


def fmt_salary(r):
    if not r["salary_min"]:
        return ""
    cur = {"PLN": "zł", None: "zł"}.get(r["currency"], r["currency"])
    s = fmt_money(r["salary_min"])
    if r["salary_max"] and r["salary_max"] != r["salary_min"]:
        s += "–" + fmt_money(r["salary_max"])
    return f"{s} {cur}"


def fmt_when(iso):
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    today = datetime.now().date()
    if d.date() == today:
        return f"dziś o {d:%H:%M}"
    if d.date() == today - timedelta(days=1):
        return f"wczoraj o {d:%H:%M}"
    return f"{d:%d.%m} o {d:%H:%M}"


def sync_status():
    """Co pokazac w pasku 'ostatnio pobrano' - z pipeline_status.json."""
    st = pipeline.read_status()
    running = pipeline.is_running()
    steps = st.get("steps") or {}
    scraped = {k: v for k, v in steps.items() if k in pipeline.SCRAPERS}
    failed = [pipeline_label(k) for k, v in steps.items() if not v.get("ok")]
    summary = ", ".join(f"{SOURCE_LABELS.get(k, k)}: {v.get('n', 0)}"
                        for k, v in scraped.items())
    return {
        "running": running,
        "progress": progress(st) if running else None,
        "finished": fmt_when(st["finished"]) if st.get("finished") and not running else None,
        "summary": summary,
        "failed": ("nie udało się: " + ", ".join(failed)) if failed and not running else "",
    }


def progress(st):
    """Postep calego przebiegu: ukonczone kroki + ulamek biezacego."""
    order = st.get("order") or []
    cur = st.get("current") or {}
    step = cur.get("step")
    if step not in order:
        return {"pct": 2, "text": "start…"}
    done, total = cur.get("done") or 0, cur.get("total") or 0
    frac = min(done / total, 1) if total else 0
    pct = round(100 * (order.index(step) + frac) / len(order))
    text = f"krok {order.index(step) + 1}/{len(order)}: {STEP_LABELS.get(step, step)}"
    if total:
        text += f" ({done}/{total})"
    return {"pct": max(pct, 2), "text": text}


@app.route("/status")
def status():
    """Stan pobierania dla paska postepu - odpytywane co 2 s z listy ofert."""
    st = sync_status()
    return jsonify(running=st["running"], progress=st["progress"])


def pipeline_label(step):
    return SOURCE_LABELS.get(step, step)


def page(conn, pid, body):
    return render_template_string(BASE, profiles=profiles(conn), pid=pid or None, body=body)


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        ip = client_ip()
        if too_many(f"e:{email}", f"i:{ip}"):
            return render_template_string(
                AUTH, title="Zaloguj się", err=LOCKOUT_MSG, invite=False,
                email=email, alt=""), 429
        conn = connect()
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = check_password_hash(u["pass_hash"] if u else DUMMY_HASH,
                                 request.form["password"])
        if u and ok:
            session.clear()                              # nowa sesja po zalogowaniu
            session.permanent = True
            session["uid"] = u["id"]
            conn.execute("UPDATE users SET last_login=? WHERE id=?",
                         (datetime.now().isoformat(timespec="seconds"), u["id"]))
            conn.commit()
            session["csrf"] = secrets.token_urlsafe(32)   # nowy token po zalogowaniu
            return redirect(safe_next(request.args.get("next"), url_for("index")))
        note_attempt(f"e:{email}", f"i:{ip}")
        err = "Zły e-mail lub hasło."
    return render_template_string(
        AUTH, title="Zaloguj się", err=err, invite=False,
        email=request.form.get("email"),
        alt=f'Nie masz konta? <a href="{url_for("register")}">Załóż konto</a>')


@app.route("/register", methods=["GET", "POST"])
def register():
    err = None
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pwd = request.form["password"]
        ip = client_ip()
        if too_many(f"r:{ip}"):
            return render_template_string(
                AUTH, title="Załóż konto", err=LOCKOUT_MSG,
                invite=bool(INVITE_CODE), email=email, alt=""), 429
        note_attempt(f"r:{ip}")
        if INVITE_CODE and not secrets.compare_digest(
                request.form.get("invite", "").encode(), INVITE_CODE.encode()):
            err = "Zły kod zaproszenia."
        elif len(pwd) < 8:
            err = "Hasło musi mieć co najmniej 8 znaków."
        else:
            conn = connect()
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                err = "Takie konto już istnieje."
            else:
                now = datetime.now().isoformat(timespec="seconds")
                first = not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
                conn.execute(
                    "INSERT INTO users (email, pass_hash, is_admin, created_at) VALUES (?,?,?,?)",
                    (email, generate_password_hash(pwd), 1 if first else 0, now))
                conn.commit()
                session.clear()
                session.permanent = True
                session["uid"] = conn.execute(
                    "SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
                session["csrf"] = secrets.token_urlsafe(32)
                return redirect(url_for("new_profile"))
    return render_template_string(
        AUTH, title="Załóż konto", err=err, invite=bool(INVITE_CODE),
        email=request.form.get("email"),
        alt=f'Masz już konto? <a href="{url_for("login")}">Zaloguj się</a>')


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    conn = connect()
    ps = profiles(conn)
    if not ps:
        return redirect(url_for("new_profile"))
    return redirect(url_for("view", pid=ps[0]["id"]))


@app.route("/p/<int:pid>")
def view(pid):
    f = request.args.get("f", "nowe")
    conn = connect()
    if not own_profile(conn, pid):
        return redirect(url_for("index"))

    q = """SELECT o.*, m.score AS match, m.reasons AS match_why,
                  COALESCE(s.ghost_score,0) AS ghost, s.ghost_reasons,
                  COALESCE(fb.status,'nowa') AS status,
                  (SELECT COUNT(*) FROM sightings g
                    WHERE g.source=o.source AND g.external_id=o.external_id) AS days_seen
           FROM offers o
           JOIN match_results m ON m.source=o.source AND m.external_id=o.external_id
                                AND m.profile_id=?
           LEFT JOIN scores s ON s.source=o.source AND s.external_id=o.external_id
           LEFT JOIN feedback fb ON fb.source=o.source AND fb.external_id=o.external_id
                                AND fb.profile_id=?
           WHERE m.score >= 0
           ORDER BY days_seen ASC, m.score DESC, ghost ASC"""
    all_rows = conn.execute(q, (pid, pid)).fetchall()

    def visible(r):
        return r["status"] not in HIDDEN

    counts = {
        "wszystkie": sum(1 for r in all_rows if visible(r)),
        "nowe": sum(1 for r in all_rows if visible(r) and r["days_seen"] <= 1),
        "pewne": sum(1 for r in all_rows if visible(r) and r["ghost"] < 25),
        "zapisane": sum(1 for r in all_rows if r["status"] == "zapisana"),
        "wyslane": sum(1 for r in all_rows if r["status"] in ("wyslana", "rozmowa")),
    }

    sel = {
        "wszystkie": lambda r: visible(r),
        "nowe": lambda r: visible(r) and r["days_seen"] <= 1,
        "pewne": lambda r: visible(r) and r["ghost"] < 25,
        "zapisane": lambda r: r["status"] == "zapisana",
        "wyslane": lambda r: r["status"] in ("wyslana", "rozmowa"),
    }.get(f, visible)

    pcfg = profile_config(own_profile(conn, pid)["config"])
    cv_terms = set(pcfg.get("cv_terms") or [])

    src = request.args.get("src") or ""
    src_counts = {}
    for r in all_rows:
        if sel(r):
            src_counts[r["source"]] = src_counts.get(r["source"], 0) + 1

    rows = []
    for r in all_rows:
        if not sel(r) or (src and r["source"] != src):
            continue
        has = miss = []
        if cv_terms:
            has, miss = cvmod.gap(cv_terms, f"{r['title']} {r['description'] or ''}", limit=6)
        why = ""
        if r["ghost"] >= 25 and r["ghost_reasons"]:
            why = ", ".join(x["why"] for x in json.loads(r["ghost_reasons"]))
        try:
            reasons = " · ".join(json.loads(r["match_why"] or "[]"))
        except ValueError:
            reasons = ""
        rows.append(dict(r, sal=fmt_salary(r), ghost_why=why, reasons=reasons,
                         has=has, miss=miss))

    body = render_template_string(LIST, rows=rows[:300], total=len(rows), f=f,
                                  counts=counts, pid=pid, statuses=STATUSES,
                                  labels=STATUS_LABELS, sources=SOURCE_LABELS,
                                  hidden=HIDDEN, sync=sync_status(), request=request,
                                  src=src, src_counts=src_counts)
    return page(conn, pid, body)


@app.route("/p/<int:pid>/apply/<source>/<eid>")
def apply_view(pid, source, eid):
    conn = connect()
    if not own_profile(conn, pid):
        return redirect(url_for("index"))
    o = conn.execute("SELECT * FROM offers WHERE source=? AND external_id=?",
                     (source, eid)).fetchone()
    if not o:
        return "Nie ma takiej oferty", 404

    sc = conn.execute(
        "SELECT ghost_score, ghost_reasons FROM scores WHERE source=? AND external_id=?",
        (source, eid)).fetchone()
    ghost = (sc["ghost_score"] if sc else 0) or 0
    ghost_why = (", ".join(x["why"] for x in json.loads(sc["ghost_reasons"]))
                 if sc and sc["ghost_reasons"] else "")

    pcfg = profile_config(own_profile(conn, pid)["config"])
    cv_terms = set(pcfg.get("cv_terms") or [])
    has = miss = []
    if cv_terms:
        has, miss = cvmod.gap(cv_terms, f"{o['title']} {o['description'] or ''}", limit=8)

    body = render_template_string(
        APPLY, o=o, pid=pid, has=has, miss=miss,
        letter=cvmod.draft_letter(pcfg, o, has, miss),
        ghost=ghost, ghost_why=ghost_why,
        email=cvmod.find_email(o["description"]),
        subject=quote(f"Aplikacja - {o['title']}"))
    return page(conn, pid, body)


@app.route("/mark", methods=["POST"])
def mark():
    conn = connect()
    if not own_profile(conn, request.form["pid"]):
        return redirect(url_for("index"))
    if request.form.get("status") not in STATUSES:
        return "Nieznany status", 400
    conn.execute(
        """INSERT INTO feedback (profile_id, source, external_id, status, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(profile_id, source, external_id) DO UPDATE SET
             status=excluded.status, updated_at=excluded.updated_at""",
        (request.form["pid"], request.form["source"], request.form["external_id"],
         request.form["status"], datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return redirect(safe_next(request.form.get("back"),
                              url_for("view", pid=request.form["pid"])))


@app.route("/refresh", methods=["POST"])
def refresh():
    """Odpala caly przebieg w tle; postep widac w pasku nad lista."""
    if not pipeline.is_running():
        subprocess.Popen([sys.executable, str(ROOT / "pipeline.py")], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(0.5)      # zeby plik blokady zdazyl powstac przed przekierowaniem
    return redirect(safe_next(request.form.get("back")))


@app.route("/p/<int:pid>/edit", methods=["GET", "POST"])
def edit(pid):
    conn = connect()
    if not own_profile(conn, pid):
        return redirect(url_for("index"))
    if request.method == "POST":
        cfg = form_to_cfg(request.form)
        conn.execute("UPDATE profiles SET name=?, config=? WHERE id=?",
                     (request.form["name"].strip() or f"Profil {pid}",
                      dump_config(cfg), pid))
        conn.commit()
        rematch(conn, pid)
        return redirect(url_for("view", pid=pid))
    p = own_profile(conn, pid)
    cfg = profile_config(p["config"])
    body = render_template_string(EDIT, title="Ustawienia profilu", name=p["name"], pid=pid,
                                  f=cfg_to_form(cfg), n_terms=len(cfg.get("cv_terms") or []),
                                  raw=json.dumps(cfg, ensure_ascii=False, indent=2))
    return page(conn, pid, body)


@app.route("/profiles/new", methods=["GET", "POST"])
def new_profile():
    conn = connect()
    if request.method == "POST":
        cfg = form_to_cfg(request.form)
        conn.execute(
            "INSERT INTO profiles (user_id, name, config, active, created_at) VALUES (?,?,?,1,?)",
            (uid(), request.form["name"].strip() or "Nowy profil",
             dump_config(cfg),
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        pid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        rematch(conn, pid)
        return redirect(url_for("view", pid=pid))
    blank = {k: [] for k in LIST_FIELDS}
    blank["min_salary"] = 0
    blank["radius_km"] = 30
    blank["remote_ok"] = True
    ps = profiles(conn)
    body = render_template_string(EDIT, title="Nowy profil", name="", pid=None,
                                  f=cfg_to_form(blank), n_terms=0, raw="{}")
    return page(conn, ps[0]["id"] if ps else 0, body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
