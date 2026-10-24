# Wszystko dziala z katalogu src/. Python z venv-a, jesli jest.
PY  := $(shell [ -x env/bin/python ] && echo $(CURDIR)/env/bin/python || echo python3)
RUN := cd src &&

daily:   ; $(RUN) $(PY) pipeline.py           # scrapery + ingest + score + match
scrape-pracuj: ; $(RUN) $(PY) pipeline.py --only pracuj
scrape-olx: ; $(RUN) $(PY) pipeline.py --only olx
ingest:  ; $(RUN) $(PY) ingest.py
score:   ; $(RUN) $(PY) score.py
match:   ; $(RUN) $(PY) match.py
report:  ; $(RUN) $(PY) report.py

rescore: score match           # przelicz historie bez scrapowania
gui:     ; $(RUN) JOBWATCH_HTTPS=0 $(PY) app.py   # lokalnie: http://127.0.0.1:5000
serve:   ; $(RUN) gunicorn -w 2 -b 127.0.0.1:8000 app:app    # produkcja, patrz src/DEPLOY.md

setup:
	$(PY) -m pip install -r requirements.txt
	$(RUN) $(PY) profiles.py --seed

clean-db: ; rm -f src/jobwatch.db src/jobwatch.db-wal src/jobwatch.db-shm

.PHONY: daily scrape-pracuj scrape-olx ingest score match report rescore gui serve setup clean-db
