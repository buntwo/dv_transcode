#!/bin/bash

for f in 'Originals/Set 2/'*; do
    echo $f
    subpath="${f#*/}"
    if [ -e "Access/$subpath" ]; then
        echo $f already transcribed, skipping...
        continue
    fi
    ./transcode2.sh --mode transcode --crop-bottom 8 --denoise light --yes "$f/out.dv" Access "Logs"
done
