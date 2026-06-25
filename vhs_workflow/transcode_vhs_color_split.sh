#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$script_dir/.." && pwd)
data_root=${VHS_DATA_ROOT:-/Users/btu/scratch/Videos}
master_root=${VHS_MASTER_ROOT:-/Volumes/TU/tu.brian.2026.05.09/data/masters/tape}
default_task_file="$data_root/transcode_vhs_color_split.tasks"

usage() {
  cat <<'EOF'
Usage:
  transcode_vhs_color_split.sh
      Run transcodes sequentially.

  transcode_vhs_color_split.sh --emit-tasks [TASK_FILE]
      Write one transcode command per source file.

  transcode_vhs_color_split.sh --parallel [TASK_FILE] [--run-dir RUN_DIR]
      Write tasks, then run them through transcode_parallel.sh.

  transcode_vhs_color_split.sh --parallel [TASK_FILE] --resume RUN_DIR
      Write tasks from the current split, then resume an existing parallel run.

Set VHS_DATA_ROOT to override the default media/output root:
  VHS_DATA_ROOT=/path/to/Videos vhs_workflow/transcode_vhs_color_split.sh

Set VHS_MASTER_ROOT to override the default master media root:
  VHS_MASTER_ROOT=/path/to/masters/tape vhs_workflow/transcode_vhs_color_split.sh

Adjust live parallelism while --parallel is running:
  echo 1 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
  echo 3 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
  echo 0 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
EOF
}

quote_command() {
  local arg
  printf '%q' "$1"
  shift
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
}

transcode_cmd=()

build_transcode_command() {
  local input_file=$1
  shift
  transcode_cmd=(
    uv --project "$repo_dir" run "$repo_dir/scripts/transcode_access.py"
    --format vhs \
    --encoder libx265 \
    --source-root "$master_root" \
    --output-dir "$data_root/Access_crf22" \
    --log-dir "$data_root/Logs_crf22" \
    --crf 22 \
    --yes \
    "$@" \
    "$input_file"
  )
}

transcode_one() {
  build_transcode_command "$@"
  "${transcode_cmd[@]}"
}

emit_transcode_one() {
  build_transcode_command "$@"
  quote_command "${transcode_cmd[@]}"
}

transcode_group() {
  local color_correct=$1
  shift
  local input_file
  local extra_args=()

  if [[ "$color_correct" == "yes" ]]; then
    extra_args+=(--vhs-color-correct)
  fi

  for input_file in "$@"; do
    if [[ "$(basename "$input_file")" == "08.mkv" ]]; then
      transcode_one "$input_file" ${extra_args[@]+"${extra_args[@]}"} --audio-channel right
    else
      transcode_one "$input_file" ${extra_args[@]+"${extra_args[@]}"}
    fi
  done
}

emit_transcode_group() {
  local color_correct=$1
  shift
  local input_file
  local extra_args=()

  if [[ "$color_correct" == "yes" ]]; then
    extra_args+=(--vhs-color-correct)
  fi

  for input_file in "$@"; do
    if [[ "$(basename "$input_file")" == "08.mkv" ]]; then
      emit_transcode_one "$input_file" ${extra_args[@]+"${extra_args[@]}"} --audio-channel right
    else
      emit_transcode_one "$input_file" ${extra_args[@]+"${extra_args[@]}"}
    fi
  done
}

color_correct_input_files=(
    #"$master_root/04_1st_Birthday_Christmas_1993.mkv"
    "$master_root/05_Brian_Tu.mkv"
    "$master_root/06_Brian_15MO-1M.mkv"
    "$master_root/07_Brian_19mo_-_24_Month.mkv"
    "$master_root/10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991.mkv"
    "$master_root/11.mkv"
    "$master_root/12_Master_Defense.mkv"
    "$master_root/13_Brian_24_Month_-_36_Mo.mkv"
    "$master_root/14.mkv"
    "$master_root/15.mkv"
    "$master_root/20_2.24.1998-10.5.1998_From_China_til_6.28.1998.mkv"
    "$master_root/21_10.5.1998-11.5.1999.mkv"
    "$master_root/22_11.5.1999-12.23.2000.mkv"
  )

no_color_correct_input_files=(
    "$master_root/08.mkv"
    "$master_root/16_6.17.1996-9.16.1996_From_China.mkv"
    "$master_root/17_9.16.1996-1.12.1997_From_China.mkv"
    "$master_root/18_2.8.1997-7.14.1997_From_China.mkv"
    "$master_root/19_7.14.1997-2.24.1998_From_China.mkv"
    "$master_root/23_12.23.2000.mkv"
    "$master_root/24_Butterfield_Gallerie_of_Dance_5-6_Year_Olds_2000-2001.mkv"
    "$master_root/25_Swim_Trial.mkv"
    "$master_root/26_Zoe_Play_Narrator.mkv"
    "$master_root/01_Y2K_1.mkv"
    "$master_root/02_Y2K_2.mkv"
    "$master_root/03_Y2K_3.mkv"
  )

emit_all_tasks() {
  emit_transcode_group yes "${color_correct_input_files[@]}"
  emit_transcode_group no "${no_color_correct_input_files[@]}"
}

run_all_transcodes() {
  transcode_group yes "${color_correct_input_files[@]}"
  transcode_group no "${no_color_correct_input_files[@]}"
}

run_parallel() {
  local task_file="$default_task_file"
  local task_file_set=0
  local runner_args=()

  while (($# > 0)); do
    case "$1" in
      --resume|--run-dir)
        if [[ -z "${2:-}" ]]; then
          echo "$1 requires a directory path" >&2
          exit 2
        fi
        runner_args+=("$1" "$2")
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      -*)
        echo "Unknown --parallel option: $1" >&2
        usage >&2
        exit 2
        ;;
      *)
        if (( task_file_set == 1 )); then
          echo "Only one TASK_FILE may be provided" >&2
          usage >&2
          exit 2
        fi
        task_file=$1
        task_file_set=1
        shift
        ;;
    esac
  done

  emit_all_tasks > "$task_file"
  echo "Wrote tasks to $task_file"
  exec "$repo_dir/scripts/transcode_parallel.sh" "${runner_args[@]}" "$task_file"
}

mode=${1:-run}
case "$mode" in
  run|--run)
    run_all_transcodes
    ;;
  --emit-tasks)
    task_file=${2:-"$default_task_file"}
    emit_all_tasks > "$task_file"
    echo "Wrote tasks to $task_file"
    echo "Run with: $repo_dir/scripts/transcode_parallel.sh '$task_file'"
    ;;
  --parallel)
    shift
    run_parallel "$@"
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
