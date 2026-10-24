#!/bin/sh
# Jeden dzienny przebieg (Docker). Cala logika siedzi w src/pipeline.py.
set -u
cd "$(dirname "$0")/src"
echo "[daily] === $(date +%F) ==="
python3 pipeline.py --limit "${JOBWATCH_LIMIT:-300}"
echo "[daily] koniec"
