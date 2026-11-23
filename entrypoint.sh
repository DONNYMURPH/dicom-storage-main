#!/usr/bin/env bash
# One image, two roles (both run in the same pod, sharing the data volume):
#   samba   - SMB file server with one share per project (brain, prostate, ...)
#   watcher - watches every share's inbox/ and sends finished studies to CTP
set -euo pipefail

role="${1:-watcher}"
shift || true

case "$role" in
  samba)   exec /app/samba_server.py "$@" ;;
  watcher) exec /app/watcher.py "$@" ;;
  *)       exec "$role" "$@" ;;   # e.g. "bash" for debugging
esac
