#!/usr/bin/env bash
set -euo pipefail

workflow_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$workflow_root/.." && pwd)

batch_name="Unnumbered"
batch_dir="Unnumbered"

split_job_dirs=(
  "Unnumbered/A Brian"
  "Unnumbered/E Piano Party ?year"
  "Unnumbered/F Zoe"
  "Unnumbered/G Zoe Swim"
  "Unnumbered/I 灵灵Zoe Recital"
  "Unnumbered/J TCCDC 2009.11.19"
)
split_job_inputs=("out.dv" "out.dv" "out.dv" "out1.dv" "out.dv" "out.dv")
split_job_specs=(
  "1,2-7,8,9"
  "1-5,6"
  "1-4,5-6"
  "1-6,7-12,13-17,18-25,26-30,31-36,37-39,40,41-47,48-51,52-53"
  "1-4,5-11,12,13-14"
  "1-10,11-12,13-37"
)
split_job_flags=(
  "-s -d -t"
  "-s -d -t"
  "-s -d -t"
  "-s -d -t"
  "-3 -s -d -t"
  "-3 -s -d -t"
)

whole_inputs=(
  "Unnumbered/B MJH Honor Speech_Bye Bye Birdy 1/out1.dv"
  "Unnumbered/B MJH Honor Speech_Bye Bye Birdy 1/out2.dv"
  "Unnumbered/C Bye Bye Birdy 2/out.dv"
  "Unnumbered/D Bye Bye Birdy 3/out.dv"
  "Unnumbered/G Zoe Swim/out2.dv"
  'Unnumbered/H 灵灵 3rd Grade "ShoeBeDo" Show/out.dv'
)

# These captures are present in the source archive but are intentionally not
# part of this batch. J is split exclusively from out.dv.
ignored_inputs=(
  "Unnumbered/J TCCDC 2009.11.19/out1.dv"
  "Unnumbered/J TCCDC 2009.11.19/out2.dv"
  "Unnumbered/J TCCDC 2009.11.19/out3.dv"
  "Unnumbered/J TCCDC 2009.11.19/out4.dv"
  "Unnumbered/J TCCDC 2009.11.19/out5.dv"
)

# shellcheck source=_digital8_split_transcode_common.sh
# shellcheck source-path=SCRIPTDIR
source "$workflow_root/_digital8_split_transcode_common.sh"
run_digital8_split_transcode_batch "$@"
