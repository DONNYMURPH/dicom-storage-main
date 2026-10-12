#!/usr/bin/env bash

# Watches a directory (recursively) for incoming dicoms, and sends them to an SCP.
# Run ./store_dcm <output_dir> <port> on the receiver."

WATCHDIR="." # Path to local directory to watch for updates
PEER=""      # Hostname of receiver
PORT=""      # Receiever port

USAGE="Usage: $(basename "${BASH_SOURCE[0]}") -d <dir to watch> -s <receiver hostname> -p <receiver port>"

NUM_ARGS=6
if [[ $# -lt NUM_ARGS ]] ; then
    echo "$USAGE" >&2
    exit 1
fi

while [[ $# -gt 0 ]] ; do
    case "$1" in
        "-h")
            echo "$USAGE" >&2
            exit
            ;;
        "-d")
            WATCHDIR="$2"
            shift 2
            ;;
        "-p")
            PORT="$2"
            shift 2
            ;;
        "-s")
            PEER="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# In loop, wait for .dcms in DIR and send them to receiver
# -r: recursively checks dirs
# -c: Outputted as csv (for splitting)
# -e: Listens only for files created in, moved to the dir
inotifywait -m -r -c -e create -e moved_to "$WATCHDIR" | while read -r line; do
    event=$(awk -F',' '{print $2}' <<< "$line")
    file=$(awk -F',' '{print $3}' <<< "$line")
    # if .dcm, dcmsend it and delete original file
    if [[ $(awk -F'.' '{print $NF}' <<< "$file") == "dcm" ]] ; then
        filepath="${WATCHDIR}/${file}"
        echo "${event} on ${file}, sending to ${PEER} on port ${PORT}"
        dcmsend "$PEER" "$PORT" "$filepath"
        if [[ $# -eq 0 ]] ; then
            rm -f "$filepath"
        else 
            echo "Error sending file ${file}" >&2
        fi
    fi
done