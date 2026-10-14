#!/usr/bin/env bash
set -uo pipefail

ROOT="${DROPBOX_ROOT:-/data}"
INBOX="$ROOT/inbox"; WORK="$ROOT/processing"
DONE="$ROOT/done";   FAILED="$ROOT/failed"

: "${PEER:?set PEER}"
: "${PORT:?set PORT}"
CALLED_AE="${CALLED_AE:-CTP}"
CALLING_AE="${CALLING_AE:-DROPBOX}"
POLL="${POLL_INTERVAL:-30}"

mkdir -p "$INBOX" "$WORK" "$DONE" "$FAILED"
log(){ printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

stop=0
trap 'log "shutdown requested"; stop=1' TERM INT

declare -A SEEN

log "watching $INBOX -> $PEER:$PORT (poll ${POLL}s, stable-across-2-polls)"

while :; do
  date -u +%s > /tmp/heartbeat

  while IFS= read -r study; do
    [ -z "$study" ] && continue
    name="$(basename "$study")"

    sig=$(find "$study" -type f -exec stat -c '%n %s' {} + 2>/dev/null | sort | md5sum | cut -d' ' -f1)

    prev="${SEEN[$name]:-}"
    SEEN["$name"]="$sig"

    if [ "$prev" != "$sig" ]; then
      log "$name still receiving"
      continue
    fi
    
    if [ -z "$(find "$study" -type f ! -name '.*' -print -quit)" ]; then
      continue
    fi

    mv "$study" "$WORK/$name" 2>/dev/null || { log "claim failed: $name"; continue; }
    unset 'SEEN[$name]'

    find "$WORK/$name" -name '.*' -type f -delete 2>/dev/null

    n=$(find "$WORK/$name" -type f | wc -l)
    log "sending $name ($n files)"

    if dcmsend "$PEER" "$PORT" \
         -aet "$CALLING_AE" -aec "$CALLED_AE" \
         --scan-directories --recurse "$WORK/$name"; then
      mv "$WORK/$name" "$DONE/$name"; log "sent $name ($n files)"
    else
      mv "$WORK/$name" "$FAILED/$name"; log "FAILED $name (kept in failed/)"
    fi
  done < <(find "$INBOX" -mindepth 1 -maxdepth 1 -type d ! -name '*.part' ! -name '.*' | sort)

  [ "$stop" -eq 1 ] && { log "exiting"; exit 0; }
  sleep "$POLL"
done
