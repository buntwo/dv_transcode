#!/usr/bin/env bash
set -euo pipefail

workflow_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$workflow_root/.." && pwd)

batch_name="Set 3"
batch_dir="Set 3"

split_job_dirs=(
  "Set 3/1 东东 -> 浦东机场，去苏州"
  "Set 3/2 苏州"
  "Set 3/3 北京"
  "Set 3/6"
  "Set 3/9"
  "Set 3/10"
  "Set 3/11"
  "Set 3/13"
  "Set 3/17 Brians 10th Birthday Party at MG Community Center"
  "Set 3/18 Brians 4th grade musical America The Beautiful (3.13.03)"
  "Set 3/19"
  "Set 3/20 Wisconsin Dells Kalahari (1)"
  "Set 3/22"
  "Set 3/23"
  "Set 3/24"
  "Set 3/25"
  "Set 3/26"
  "Set 3/27"
  "Set 3/28 Piano Competitions_Zoe - B+B (2)"
  "Set 3/30 Zoe - Beauty + Beast (1)"
)
split_job_inputs=(
  "out.dv" "out.dv" "out.dv" "out.dv" "out.dv"
  "out.dv" "out.dv" "out.dv" "out.dv" "out.dv"
  "out.dv" "out.dv" "out.dv" "out.dv" "out.dv"
  "out.dv" "out.dv" "out.dv" "out.dv" "out.dv"
)
split_job_specs=(
  "1-8,9-10,11-32"
  "1-11,12-32"
  "1-14,15-28"
  "1-3,4-10"
  "1-4,5-29"
  "1-10,11-27"
  "1-13,14-15,16-17"
  "1-4,5,6-7"
  "1,2-3,4"
  "1-3,4-6,7-13"
  "1-10,11-21,22,23,24,25-27"
  "1-4,5-6,7-8"
  "1-3,4"
  "1-2,3,4,5-7"
  "1-10,11-13,14-24"
  "1-8,9-18,19-22,23-24"
  "1-25,26-37,38-39"
  "1-13,14-16,17-19"
  "1-3,4-8,9,10-14"
  "1-5,6-7,8-10,11-12,13,14-15"
)
split_job_flags=(
  "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t"
  "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t"
  "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t"
  "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t"
  "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t" "-3 -s -d -t"
)

whole_inputs=(
  "Set 3/4 长城/out.dv"
  "Set 3/5/out.dv"
  "Set 3/7/out.dv"
  "Set 3/8/out.dv"
  "Set 3/12/out.dv"
  "Set 3/14/out.dv"
  "Set 3/15/out.dv"
  "Set 3/16/out.dv"
  "Set 3/21 Wisconsin Dells Kalahari (2)/out.dv"
  "Set 3/29 Paul Wirth Piano Lesson Zoe Brian/out.dv"
)

# shellcheck source=_digital8_split_transcode_common.sh
# shellcheck source-path=SCRIPTDIR
source "$workflow_root/_digital8_split_transcode_common.sh"
run_digital8_split_transcode_batch "$@"
