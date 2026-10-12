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
QUIET="${QUIET_SECONDS:-120}"

# match the lab's convention: ignore dotfiles and AppleDouble junk
NOJUNK=( ! -name '.*' ! -name '._*' )

mkdir -p "$INBOX" "$WORK" "$DONE" "$FAILED"
log(){ printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

stop=0
trap 'log "shutdown requested"; stop=1' TERM INT

log "watching $INBOX -> $PEER:$PORT (poll ${POLL}s, quiet ${QUIET}s)"

while :; do
  date -u +%s > /tmp/heartbeat

  while IFS= read -r study; do
    [ -z "$study" ] && continue
    name="$(basename "$study")"
    if [ -z "$(find "$study" -type f "${NOJUNK[@]}" -print -quit)" ]; then
      continue
    fi

    if [ -n "$(find "$study" -type f "${NOJUNK[@]}" -newermt "-${QUIET} seconds" -print -quit)" ]; then
      log "$name still receiving"; continue
    fi

    mv "$study" "$WORK/$name" 2>/dev/null || { log "claim failed: $name"; continue; }

    # drop macOS litter so dcmsend never sees it
    find "$WORK/$name" \( -name '.*' -o -name '._*' \) -type f -delete 2>/dev/null

    log "sending $name"
    if dcmsend "$PEER" "$PORT" \
         -aet "$CALLING_AE" -aec "$CALLED_AE" \
         --scan-directories --recurse "$WORK/$name"; then
      mv "$WORK/$name" "$DONE/$name"; log "sent $name"
    else
      mv "$WORK/$name" "$FAILED/$name"; log "FAILED $name (kept in failed/)"
    fi
  done < <(find "$INBOX" -mindepth 1 -maxdepth 1 -type d | sort)

  [ "$stop" -eq 1 ] && { log "exiting"; exit 0; }
  sleep "$POLL"
done
