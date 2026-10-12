#!/usr/bin/env bash

# Takes in a path to a directory of .dcm files (or defaults to .), 
# uses dcmdump to get patient name and ID for each,
# and outputs as csv.

dir="$1"
if [[ ! -d "$dir" ]] ; then
    dir="."
fi

echo "patient,id"
for f in ${dir}/* ; do
    if [[ "${f##*.}" == dcm ]] ; then
        grep -e "(0010,0010)" -e "(0010,0020)" <<< "$(dcmdump "$f")" |
            awk '{print $3}' |
            paste -sd ',' |
            sed 's/[][]//g'
    fi
done