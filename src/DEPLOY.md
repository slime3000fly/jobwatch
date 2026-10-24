# Wdrozenie na publiczny adres

Dotyczy sytuacji, gdy uzytkownicy sa nietechniczni i lacza sie przez zwykly
adres w przegladarce. `python3 app.py` to serwer deweloperski Flaska —
**nie wystawiaj go na internet**. Ponizej minimalny, ale kompletny zestaw.

## 1. Serwer aplikacji

    pip install gunicorn
    gunicorn -w 2 -b 127.0.0.1:8000 app:app

Dwa procesy wystarcza na kilkanascie osob. `127.0.0.1` jest celowo — na
zewnatrz wystawia dopiero nginx.

## 2. systemd

`/etc/systemd/system/jobwatch.service`:

    [Unit]
    Description=JobWatch
    After=network.target

    [Service]
    User=jobwatch
    WorkingDirectory=/opt/jobwatch
    Environment="JOBWATCH_INVITE=twoj-kod-zaproszenia"
    Environment="JOBWATCH_HTTPS=1"
    ExecStart=/opt/jobwatch/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 app:app
    Restart=always
    RestartSec=5
    NoNewPrivileges=true
    PrivateTmp=true
    ProtectSystem=strict
    ReadWritePaths=/opt/jobwatch

    [Install]
    WantedBy=multi-user.target

    systemctl enable --now jobwatch

Osobny uzytkownik `jobwatch`, nie root. Baza i `.secret_key` musza byc dla
niego zapisywalne, dla reszty swiata nie: `chmod 600 .secret_key`.

## 3. nginx + HTTPS

`/etc/nginx/sites-available/jobwatch`:

    server {
        server_name praca.twojadomena.pl;

        # Na poziomie server - dziedzicza je WSZYSTKIE location ponizej.
        # Wewnatrz `location /` bloki /login i /register by ich nie dostaly.
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        location / {
            proxy_pass http://127.0.0.1:8000;
        }

        # drugi limit, niezalezny od tego w aplikacji
        location /login  { limit_req zone=auth burst=5 nodelay;
                           proxy_pass http://127.0.0.1:8000; }
        location /register { limit_req zone=auth burst=3 nodelay;
                           proxy_pass http://127.0.0.1:8000; }

        client_max_body_size 2m;
        # Naglowki bezpieczenstwa (CSP, HSTS, X-Frame-Options...) ustawia aplikacja.
    }

W `/etc/nginx/nginx.conf`, w bloku `http`:

    limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/m;

Certyfikat:

    certbot --nginx -d praca.twojadomena.pl

Aplikacja ufa tylko OSTATNIEMU wpisowi `X-Forwarded-For` (ProxyFix, x_for=1),
czyli temu dopisanemu przez nginx - klient nie podrobi sobie IP. Zakladamy
dokladnie jeden proxy przed aplikacja; port 8000 nie moze byc wystawiony
na zewnatrz (w docker-compose jest `127.0.0.1:8000:8000` - nie zmieniaj tego,
Docker omija ufw). `X-Forwarded-For` jest wazny — bez niego limiter w aplikacji widzi wszystkich
jako jedno IP (adres nginx-a) i zablokuje wszystkich naraz.

## 4. Backup

Baza zawiera cudze CV. Jej utrata to nie tylko twoj problem.

    0 3 * * * sqlite3 /opt/jobwatch/jobwatch.db ".backup /var/backups/jw-$(date +\%F).db"
    0 4 * * * find /var/backups -name 'jw-*.db' -mtime +30 -delete

Backupy trzymaj zaszyfrowane, nie na tym samym dysku.

## 5. Czego aplikacja nadal NIE ma

Uczciwa lista, zebys wiedzial co dokladasz swiadomie:

- **resetu hasla** — zapomniane haslo oznacza recznie w bazie,
- **potwierdzania e-maila** — ktos moze zarejestrowac cudzy adres,
- **usuwania konta z poziomu GUI** — a RODO daje do tego prawo,
- **polityki prywatnosci** — przy cudzych CV jest obowiazkowa,
- **logow bezpieczenstwa** — poza logiem nginx-a.

Pierwsze trzy to po pol dnia kazde. Czwarte to nie kod, tylko dokument.

## 6. RODO w skrocie

Hostujac CV nieznajomych jestes administratorem ich danych. Minimum:

- podstawa prawna (zgoda przy rejestracji) i informacja, po co zbierasz dane,
- mozliwosc usuniecia konta i danych na zadanie,
- 72 godziny na zgloszenie naruszenia do UODO,
- nie przechowuj CV dluzej, niz potrzeba.

Dopoki kod zaproszenia jest wlaczony, a krag to rodzina i znajomi, ryzyko jest
niewielkie. Otwarta rejestracja to inna kategoria — wtedy warto skonsultowac
to z kims, kto zna temat zawodowo.
