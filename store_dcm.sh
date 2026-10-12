#!/usr/bin/env bash

# Wrapper for storescp.
# adds -sp flag (output into directory for patient),
# sets AET to this hostname.
# Takes in as positional arguments the output directory, other options, and the listen port.

if [[ $# -lt 2 ]] ; then
    echo "Usage: $(basename "${BASH_SOURCE[0]}") <output-directory> <args> <port>" >&2
    exit 1
fi
if [[ ! -d "$1" ]] ; then
    if ! mkdir "$1" ; then
        echo "Error creating output directory" >&2
        exit 1
    fi
fi
echo "Starting SCP on port ${!#}"
storescp -sp -aet "$(hostname)" -od "$@"