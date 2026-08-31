#!/usr/bin/env bash
set -euo pipefail

workflow_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$workflow_root/.." && pwd)

batch_name="Set 1"
batch_dir="Set 1"

split_job_dirs=(
  "Set 1/2 Disney + Brian birthday + piano"
  "Set 1/3 Kids dance + hotpot"
  "Set 1/4 Christmas"
)
split_job_inputs=("out.dv" "out.dv" "out.dv")
split_job_specs=(
  "1-9,10,11-13,14"
  "1-10,11"
  "1-3,4"
)
split_job_flags=("-s" "-s" "-s")

whole_inputs=(
  "Set 1/1 Disney/out.dv"
)

# shellcheck source=_digital8_split_transcode_common.sh
# shellcheck source-path=SCRIPTDIR
source "$workflow_root/_digital8_split_transcode_common.sh"
run_digital8_split_transcode_batch "$@"
