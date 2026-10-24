#!/bin/sh
# Scheduler bez crona: liczy ile sekund do najblizszej godziny startu, spi, dziala.
# Prostsze w debugowaniu niz cron w kontenerze - wszystko leci na stdout.
set -eu

HOUR="${JOBWATCH_RUN_HOUR:-7}"
cd "$(dirname "$0")"

echo "[scheduler] start, przebieg codziennie o ${HOUR}:00 (${TZ:-UTC})"

# Pierwszy przebieg od razu, zeby nie czekac doby na pierwsze dane.
if [ "${JOBWATCH_RUN_NOW:-1}" = "1" ]; then
    echo "[scheduler] przebieg startowy"
    sh ./daily.sh || echo "[scheduler] przebieg startowy nieudany, lece dalej"
fi

while true; do
    now=$(date +%s)
    next=$(date -d "today ${HOUR}:00" +%s 2>/dev/null || date -v${HOUR}H -v0M -v0S +%s)
    [ "$next" -le "$now" ] && next=$((next + 86400))
    sleep_for=$((next - now))
    echo "[scheduler] nastepny przebieg za ${sleep_for}s"
    sleep "$sleep_for"
    sh ./daily.sh || echo "[scheduler] przebieg nieudany, probuje jutro"
done
