# JobWatch

Agregator ofert z polskich portali z trzema wskaznikami: flaga "pierwszy raz",
scoring ogloszenia-widma, dopasowanie do profilu. Wiele profili — kazda osoba
ma swoj widok tych samych danych.

## Zasada

Scraper to dowolny proces wypluwajacy **JSON Lines na stdout**. Pipeline nie wie,
kto go wyprodukowal. Jesli portal zablokuje twojego scrapera, podmieniasz go na
gotowy (np. Apify), zrzucasz output do `raw/` i reszta dziala bez zmian.

    scrapers/*.py -> raw/*.jsonl -> ingest -> score -> match -> app/report
                                     wspolne  wspolne  per profil

Kazdy etap jest idempotentny. Zmieniles regule albo profil? `make rescore`
przelicza cala historie bez scrapowania czegokolwiek.

## Konta

Rejestracja przez przegladarke, haslo hashowane scryptem (werkzeug), sesja w
cookie HttpOnly + SameSite + Secure, odnawiana przy kazdym logowaniu. Wylogowanie
to POST z tokenem CSRF. Pierwsze zalozone konto dostaje `is_admin=1`.
Kazde konto widzi wylacznie swoje profile, oznaczenia i CV — proba wejscia na
cudzy profil konczy sie przekierowaniem, nie podgladem.

**Kod zaproszenia jest wlaczony domyslnie:**

    JOBWATCH_INVITE="twoj-kod" python3 app.py --host 0.0.0.0

Pusty `JOBWATCH_INVITE=""` otwiera rejestracje dla kazdego. Zanim to zrobisz,
przeczytaj sekcje "Skala" nizej.

## Skala — przeczytaj przed otwarciem rejestracji

Dopoki to narzedzie dla rodziny i znajomych (kod zaproszenia), sytuacja jest
prosta. Otwarta rejestracja zmienia dwie rzeczy naraz:

- **Regulaminy portali.** Scraping na wlasny uzytek to jedno; publiczny serwis
  zbudowany na ich danych to juz realne ryzyko, nie teoria.
- **RODO.** Stajesz sie administratorem cudzych danych osobowych, w tym CV,
  ktore potrafi zawierac dane wrazliwe. To oznacza podstawe prawna, polityke
  prywatnosci, obsluge zadan usuniecia i zgloszenia naruszen.

Po stronie technicznej aplikacja ma juz: hashowanie hasel (scrypt), ochrone
CSRF na kazdym POST, limit prob logowania i rejestracji (8 na 15 min, per e-mail
i per IP), cookie HttpOnly/SameSite/Secure, izolacje danych miedzy kontami,
limit rozmiaru requestu, **szyfrowanie profili i CV w bazie** oraz reszte z
sekcji "Bezpieczenstwo" nizej.

**Nadal nie ma** resetu hasla, potwierdzania e-maila, usuwania konta z GUI ani
polityki prywatnosci. Pelna lista i gotowa konfiguracja nginx/systemd/certbot
jest w `DEPLOY.md` — przeczytaj przed wystawieniem na publiczny adres.

## Bezpieczenstwo

Co aplikacja robi sama:

- **Brute force** — limit prob logowania per e-mail i per IP, drugi limit w
  nginx (`limit_req`, patrz `DEPLOY.md`). Kod zaproszenia chroniony tym samym
  limitem i porownywany w stalym czasie. Czas odpowiedzi nie zdradza, czy
  konto o danym e-mailu istnieje.
- **Prawdziwe IP klienta** — aplikacja ufa tylko ostatniemu wpisowi
  `X-Forwarded-For` (ten dopisany przez nginx). Podrobiony naglowek nie obchodzi
  limitu. Zalozenie: dokladnie jeden nginx przed aplikacja.
- **Przekierowania** — `next=` / `back=` przyjmuja tylko sciezki w obrebie
  serwisu (`//evil.com` odpada), linki do ofert tylko `http(s)://`.
- **Naglowki** — CSP, HSTS, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.
- **SQL injection / XSS** — wszystkie zapytania parametryzowane, szablony
  escapowane automatycznie.
- **Kontener** — read-only, bez roota, `no-new-privileges`, limit pamieci.

Czego pilnujesz ty:

- **Port 8000 tylko na localhoscie.** W `docker-compose.yml` jest
  `127.0.0.1:8000:8000` — nie zmieniaj na `8000:8000`. Docker omija ufw, wiec
  aplikacja wisialaby w internecie bez HTTPS i bez limitow nginx-a.
- **Mocny kod zaproszenia**: `openssl rand -hex 16`.
- **Sekrety nigdy w repo** — `.env`, `.secret_key`, `.db_key`, `jobwatch.db`,
  `raw/` i `backups/` sa w `.gitignore`. Nie dodawaj ich z `-f`.

### Szyfrowanie CV i profili

Kolumna `profiles.config` — tekst CV, kompetencje z CV, stanowiska, miasta,
minimalna stawka, blacklista — jest w bazie zaszyfrowana (Fernet, AES +
HMAC). Wyciekly plik `jobwatch.db` albo backup bez klucza jest bezuzyteczny.
Wlasciciel widzi swoje CV normalnie — aplikacja odszyfrowuje je w locie.

Klucz pochodzi z `JOBWATCH_DB_KEY`. Wygeneruj go **raz**:

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

i wpisz do `.env` na serwerze. `docker compose` bez niego nie wystartuje.
Lokalnie (bez zmiennej) aplikacja sama tworzy `src/.db_key`.

- **Zgubiony klucz = wszystkie CV stracone.** Backup bazy nie pomoze. Trzymaj
  kopie klucza w menedzerze hasel.
- **Nigdy obok backupow bazy** — inaczej szyfrowanie nic nie daje.
- Stara baza z jawnymi profilami szyfruje sie sama przy pierwszym starcie,
  razem z wyczyszczeniem starych kopii z pliku (`VACUUM`).

Zaszyfrowane NIE sa: e-maile (potrzebne do logowania), hasla (te sa
hashowane — jeszcze lepiej), oferty (dane publiczne) i oznaczenia ofert.

Czego szyfrowanie NIE chroni: przejecia dzialajacego serwera. Aplikacja musi
miec klucz, zeby pokazac uzytkownikowi jego CV, wiec atakujacy z dostepem do
procesu tez go ma. Przed tym chroni tylko hardening z listy wyzej. Dysk
serwera i backupy szyfruj dodatkowo (LUKS, `age`/`gpg`).

## Profile i GUI

    make setup          # zaleznosci z requirements.txt + przykladowe profile
    make gui            # http://localhost:5000 -> zaloz konto

Jedno konto moze miec kilka profili (np. dwa zawody naraz).

W GUI: przelaczasz profil na gorze, filtrujesz (nowe / bez widm / zapisane /
wyslane), oznaczasz oferty jednym klikiem, a przez "ustawienia" zmieniasz
miasta, zawody, poziomy stanowisk i blackliste bez dotykania kodu.
Nowy kolega = przycisk "+ nowy".

Zapis profilu od razu przelicza oferty — nie trzeba wracac do terminala.
Formularz ma zwykle pola (zawody, miasta, minimalna stawka, czego NIE chce),
zadnego JSON-a. Podglad JSON jest schowany pod rozwijanka dla ciebie.

## Aplikowanie

Przycisk **Aplikuj** na karcie oferty otwiera widok przygotowania zgloszenia:

1. pokrycie wymagan (zielone / szare kafelki),
2. szkic listu wygenerowany z pokrycia — z miejscami w [nawiasach] do recznego
   uzupelnienia i gotowa klauzula RODO,
3. przycisk, ktory otwiera formularz portalu i od razu oznacza oferte jako wyslana.
   Jesli w ogloszeniu jest adres e-mail, jest tez przycisk `mailto:` z tematem.

Przy ofercie z wysokim ryzykiem widma na gorze pojawia sie ostrzezenie —
zanim ktos zainwestuje czas w list.

**Czego CELOWO nie ma: automatycznej wysylki.** Powody w kolejnosci waznosci:
masowe auto-aplikowanie jest dokladnie tym, co zalewa rekruterow i sprawia,
ze przestaja czytac — czyli dziala przeciwko celowi tego narzedzia; laman
regulaminy duzo wyrazniej niz czytanie ofert, a ban leci na konto osoby
szukajacej pracy; i technicznie to osobny projekt (logowanie, CAPTCHA,
upload, kreatory wieloetapowe, inne na kazdym portalu).

Pole `display_name` w profilu podstawia imie i nazwisko do listu.

## CV i zielone kafelki

Wklej CV w formularzu profilu (pole na dole) albo zaimportuj z pliku:

    pip install pdfminer.six python-docx         # tylko jesli CV to pdf/docx
    python3 cv.py cv.pdf --save "Ania"           # profil wygenerowany z CV
    python3 cv.py cv.pdf --gap 20                # czego brakuje wzgledem ofert

Po zapisaniu CV kazda oferta w GUI dostaje kafelki: **zielone** = kompetencje
z CV, ktore oferta wymaga, **szare przekreslone** = wymagania, ktorych w CV nie
ma. Widac od razu, czy oferta jest w zasiegu i co podkreslic w liscie
motywacyjnym.

Tekst CV zostaje w bazie, zaszyfrowany (patrz "Szyfrowanie CV i profili"),
i nigdzie nie wychodzi. Pliki pdf/docx nie sa nigdzie zapisywane.

Czego CELOWO nie robi: procentowego "dopasowania CV do oferty" z embeddingow.
Przy ofertach nie-IT ("stabilne zatrudnienie, mily zespol") taki wskaznik
jest szumem udajacym precyzje. Blacklisty i minimalnej stawki z CV odgadnac
sie nie da — te dwa pola uzupelnij recznie.

**Gdzie to postawic, zeby siostra miala dostep** — od najprostszej:

- **Nietechniczni uzytkownicy** (zwykly adres w przegladarce): VPS, gunicorn
  za nginx-em z certyfikatem. Krok po kroku w `DEPLOY.md`. `make gui` to serwer
  deweloperski — nigdy na publicznym adresie.
- **Tylko dla siebie**: `make gui` + Tailscale, zero kosztow.
- **Bez GUI**: `python3 report.py --send`, mail codziennie rano.

## Start

    pip install -r requirements.txt
    cp config.example.json config.json     # tylko dane SMTP do maila

**Sobota rano, KROK PIERWSZY:**

    python3 probe_pracuj.py

Od wyniku zalezy plan calego weekendu. Nie pisz nic innego, zanim to nie przejdzie.

    python3 scrapers/rocketjobs.py --debug    # obejrzyj jeden rekord
    make daily && make gui

## Pliki

| plik | rola |
|---|---|
| `schema.json` | kontrakt, od niego zalezy wszystko |
| `common.py` | baza, normalizacja, `content_hash`, szyfrowanie profili |
| `ingest.py` | walidacja, dedup, historia |
| `score.py` | reguly widma — wspolne, zero I/O |
| `profiles.py` | profile z CLI |
| `match.py` | dopasowanie per profil |
| `app.py` | GUI, jeden plik Flask |
| `cv.py` | profil z CV + analiza luk |
| `report.py` | mail |
| `scrapers/` | jedyne pliki, ktore sie psuja |
| `DEPLOY.md` | wdrozenie na publiczny adres |

## Baza

Wszystko w `jobwatch.db` (SQLite): `offers`, `sightings` (historia dzien po dniu),
`scores` (widmo), `profiles` (config zaszyfrowany), `match_results`, `feedback`
(oznaczenia), `delivered`. W Dockerze baza lezy w wolumenie `/data`.
`raw/*.jsonl` NIE kasuj — to zrodlo prawdy, baze odtworzysz z nich od zera.
Ale tez nie commituj — to dane, nie kod.

Obok bazy leza `.secret_key` (podpis sesji, tworzony sam) i — bez
`JOBWATCH_DB_KEY` — `.db_key`. Usuniecie `.secret_key` wylogowuje wszystkich;
usuniecie `.db_key` kasuje dostep do wszystkich profili.

## Czego sie spodziewac

- Flaga NOWA dziala od drugiego uruchomienia, dopasowanie od pierwszego.
- **Scoring widma potrzebuje 2–3 tygodni.** Reposty i dlugosc wiszenia licza sie
  z historii, ktorej na starcie nie ma. Uprzedz o tym siostre.
- Zero ofert ze zrodla, ktore wczoraj dalo 200? To selektor, nie rynek —
  `ingest.py` krzyczy o tym w logu.

## Cron

    0 7 * * 1-5 cd /sciezka/jobwatch && make daily
