#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TRANSCODE_LIST_SCRIPT="$SCRIPT_DIR/transcode_vhs_color_split.sh"
PYTHON_SCRIPT="$REPO_ROOT/scripts/extract_master_audio.py"
UV_PROJECT_DIR="$REPO_ROOT"

usage() {
  cat <<'EOF'
Usage:
  transcode_vhs_color_split_extract_audio.sh --output-dir AUDIO_DIR [--force] [--repo-root PATH] [--transcode-list PATH]

Required:
  --output-dir PATH      Directory to write extracted FLAC files.

Optional:
  --force                Overwrite existing FLAC files.
  --repo-root PATH       Repository root for uv Python scripts (default: parent of this script directory)
  --transcode-list PATH  transcode_vhs_color_split.sh path (default: sibling in vhs_workflow)
EOF
}

output_dir=""
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      output_dir=$2
      shift 2
      ;;
    --force)
      force=1
      shift
      ;;
    --repo-root|--project-root)
      REPO_ROOT=$2
      TRANSCODE_LIST_SCRIPT="$REPO_ROOT/vhs_workflow/transcode_vhs_color_split.sh"
      PYTHON_SCRIPT="$REPO_ROOT/scripts/extract_master_audio.py"
      UV_PROJECT_DIR="$REPO_ROOT"
      shift 2
      ;;
    --transcode-list)
      TRANSCODE_LIST_SCRIPT=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$output_dir" ]]; then
  echo "--output-dir is required" >&2
  usage >&2
  exit 2
fi

if [[ ! -x "$TRANSCODE_LIST_SCRIPT" ]]; then
  echo "transcode list script not executable: $TRANSCODE_LIST_SCRIPT" >&2
  exit 1
fi

if [[ ! -f "$PYTHON_SCRIPT" ]]; then
  echo "extract_master_audio.py not found: $PYTHON_SCRIPT" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required" >&2
  exit 1
fi

emit_tasks() {
  bash -c '
    set -euo pipefail
    transcode_list_script=$1
    eval "$(awk '\''/^mode=/{exit} {print}'\'' "$transcode_list_script")"
    emit_all_tasks
  ' bash "$TRANSCODE_LIST_SCRIPT"
}

keep_files=()
left_files=()
right_files=()
ordered_files=()
ordered_channels=()
keep_count=0
left_count=0
right_count=0
ordered_count=0

while IFS= read -r task; do
  [[ -z "$task" ]] && continue
  # shellcheck disable=SC2206
  parts=($task)
  input_file=${parts[-1]}
  audio_channel="keep"

  for ((i = 0; i < ${#parts[@]}; i += 1)); do
    if [[ "${parts[$i]}" == "--audio-channel" ]]; then
      audio_channel=${parts[$((i + 1))]}
      break
    fi
  done

  case "$audio_channel" in
    left)
      left_files+=("$input_file")
      ordered_files+=("$input_file")
      ordered_channels+=("left")
      ((left_count += 1))
      ((ordered_count += 1))
      ;;
    right)
      right_files+=("$input_file")
      ordered_files+=("$input_file")
      ordered_channels+=("right")
      ((right_count += 1))
      ((ordered_count += 1))
      ;;
    keep|*)
      keep_files+=("$input_file")
      ordered_files+=("$input_file")
      ordered_channels+=("keep")
      ((keep_count += 1))
      ((ordered_count += 1))
      ;;
  esac
done < <(emit_tasks)

common_parent() {
  local common
  local dir

  if (( $# == 0 )); then
    return 0
  fi

  common="$(dirname "$1")"
  shift
  while (( $# > 0 )); do
    dir="$(dirname "$1")"
    while [[ "$dir" != "$common" && "$dir" != "$common"/* ]]; do
      if [[ "$common" == "/" || "$common" == "." ]]; then
        break
      fi
      common="$(dirname "$common")"
    done
    shift
  done

  printf '%s\n' "$common"
}

relative_to_parent() {
  local parent=$1
  local path=$2

  if [[ -n "$parent" && "$path" == "$parent"/* ]]; then
    printf '%s\n' "${path#"$parent"/}"
  else
    printf '%s\n' "$path"
  fi
}

print_plan() {
  local total_files=$ordered_count
  local index=0
  local input_parent=""
  local num_width=${#total_files}
  local channel_width=7
  local input_width=5
  local output_width=6
  local status_width=6

  echo "Found $total_files source file(s) in transcode list: $TRANSCODE_LIST_SCRIPT"
  echo "Keep channel: $keep_count"
  echo "Left channel: $left_count"
  echo "Right channel: $right_count"
  echo "Planned FLAC outputs in: $output_dir"

  if (( total_files == 0 )); then
    echo "No source files found."
    return 0
  fi

  input_parent="$(common_parent "${ordered_files[@]}")"
  echo "Input parent: $input_parent"
  echo "Planned extraction list:"

  while (( index < total_files )); do
    local input_file="${ordered_files[$index]}"
    local display_input
    local audio_channel="${ordered_channels[$index]}"
    local stem
    local output_file
    local output_status

    display_input="$(relative_to_parent "$input_parent" "$input_file")"
    stem="$(basename "$input_file")"
    stem="${stem%.*}"
    output_file="${output_dir%/}/$stem.flac"
    output_status="will write"
    if [[ -e "$output_file" ]]; then
      if (( force )); then
        output_status="exists, overwrite"
      else
        output_status="exists, needs --force"
      fi
    fi

    (( ${#audio_channel} > channel_width )) && channel_width=${#audio_channel}
    (( ${#display_input} > input_width )) && input_width=${#display_input}
    (( ${#output_file} > output_width )) && output_width=${#output_file}
    (( ${#output_status} > status_width )) && status_width=${#output_status}
    ((index += 1))
  done

  printf '  %*s  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "#" "$channel_width" "channel" "$input_width" "input" "$output_width" "output" "$status_width" "status"
  printf '  %*s  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "---" "$channel_width" "-------" "$input_width" "-----" "$output_width" "------" "$status_width" "------"
  index=0
  while (( index < total_files )); do
    local input_file="${ordered_files[$index]}"
    local display_input
    local audio_channel="${ordered_channels[$index]}"
    local stem
    local output_file
    display_input="$(relative_to_parent "$input_parent" "$input_file")"
    stem="$(basename "$input_file")"
    stem="${stem%.*}"
    output_file="${output_dir%/}/$stem.flac"
    output_status="will write"
    if [[ -e "$output_file" ]]; then
      if (( force )); then
        output_status="exists, overwrite"
      else
        output_status="exists, needs --force"
      fi
    fi
    printf '  %*d  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "$((index + 1))" "$channel_width" "$audio_channel" "$input_width" "$display_input" "$output_width" "$output_file" "$status_width" "$output_status"
    ((index += 1))
  done
}

confirm_run() {
  local answer
  if [[ ! -t 0 ]]; then
    echo "Interactive confirmation required. Re-run from a terminal." >&2
    return 1
  fi
  while true; do
    read -r -p "Proceed with these extraction jobs? [y/N] " answer
    case "$answer" in
      y|Y)
        return 0
        ;;
      n|N|"")
        echo "Aborted."
        return 1
        ;;
      *)
        echo "Please answer y or n."
        ;;
    esac
  done
}

print_plan
if ! confirm_run; then
  exit 0
fi

run_extract() {
  local channel=$1
  shift
  local -a files=("$@")
  if (( ${#files[@]} == 0 )); then
    return 0
  fi

  local -a cmd=(uv --project "$UV_PROJECT_DIR" run "$PYTHON_SCRIPT" --output-dir "$output_dir")
  if (( force )); then
    cmd+=(--force)
  fi
  if [[ "$channel" != "keep" ]]; then
    cmd+=(--audio-channel "$channel")
  fi
  cmd+=("${files[@]}")
  "${cmd[@]}"
}

if (( keep_count > 0 )); then
  run_extract keep "${keep_files[@]}"
fi
if (( left_count > 0 )); then
  run_extract left "${left_files[@]}"
fi
if (( right_count > 0 )); then
  run_extract right "${right_files[@]}"
fi
