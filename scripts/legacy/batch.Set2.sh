#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRANSCODE2="$SCRIPT_DIR/transcode2.sh"

for f in 'Originals/Set 2/'*; do
    echo "$f"
    subpath="${f#*/}"
    if [ -e "Access/$subpath" ]; then
        echo "$f already transcribed, skipping..."
        continue
    fi
    "$TRANSCODE2" --mode transcode --crop-bottom 8 --denoise light --yes "$f/out.dv" Access "Logs"
done
