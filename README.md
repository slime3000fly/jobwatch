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
cookie HttpOnly + SameSite. Pierwsze zalozone konto dostaje `is_admin=1`.
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
CSRF na kazdym POST, limit prob logowania (8 na 15 min, per e-mail i per IP),
cookie HttpOnly/SameSite/Secure, izolacje danych miedzy kontami i limit
rozmiaru requestu.

**Nadal nie ma** resetu hasla, potwierdzania e-maila, usuwania konta z GUI ani
polityki prywatnosci. Pelna lista i gotowa konfiguracja nginx/systemd/certbot
jest w `DEPLOY.md` — przeczytaj przed wystawieniem na publiczny adres.

## Profile i GUI

    make setup          # flask + requests
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

Tekst CV zostaje w lokalnej bazie, nigdzie nie wychodzi.

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

    pip install requests cloudscraper flask
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
| `common.py` | baza, normalizacja, `content_hash` |
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
`scores` (widmo), `profiles`, `match_results`, `feedback` (oznaczenia), `delivered`.
`raw/*.jsonl` NIE kasuj — to zrodlo prawdy, baze odtworzysz z nich od zera.

## Czego sie spodziewac

- Flaga NOWA dziala od drugiego uruchomienia, dopasowanie od pierwszego.
- **Scoring widma potrzebuje 2–3 tygodni.** Reposty i dlugosc wiszenia licza sie
  z historii, ktorej na starcie nie ma. Uprzedz o tym siostre.
- Zero ofert ze zrodla, ktore wczoraj dalo 200? To selektor, nie rynek —
  `ingest.py` krzyczy o tym w logu.

## Cron

    0 7 * * 1-5 cd /sciezka/jobwatch && make daily
