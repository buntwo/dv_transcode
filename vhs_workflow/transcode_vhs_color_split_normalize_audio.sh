#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TRANSCODE_LIST_SCRIPT="$SCRIPT_DIR/transcode_vhs_color_split.sh"
NORMALIZE_SCRIPT="$REPO_ROOT/scripts/normalize_access_audio.py"
UV_PROJECT_DIR="$REPO_ROOT"

usage() {
  cat <<'EOF'
Usage:
  transcode_vhs_color_split_normalize_audio.sh \
    --access-copy-dir ACCESS_DIR \
    --audio-dir AUDIO_DIR \
    --output-dir OUTPUT_DIR \
    [--force] \
    [--repo-root PATH] \
    [--transcode-list PATH]

Options:
  --access-copy-dir PATH   Directory with Access MP4 files to process.
  --audio-dir PATH         Directory containing matching FLAC files.
  --output-dir PATH        Directory for normalized MP4 output.
  --force                  Overwrite existing outputs.
  --repo-root PATH         Repository root for uv Python scripts (default: parent of this script directory)
  --transcode-list PATH    transcode_vhs_color_split.sh path (default: sibling in vhs_workflow)
EOF
}

access_dir=""
audio_dir=""
output_dir=""
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --access-copy-dir)
      access_dir=$2
      shift 2
      ;;
    --audio-dir)
      audio_dir=$2
      shift 2
      ;;
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
      NORMALIZE_SCRIPT="$REPO_ROOT/scripts/normalize_access_audio.py"
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

if [[ -z "$access_dir" || -z "$audio_dir" || -z "$output_dir" ]]; then
  echo "--access-copy-dir, --audio-dir, and --output-dir are required." >&2
  usage >&2
  exit 2
fi

if [[ ! -x "$TRANSCODE_LIST_SCRIPT" ]]; then
  echo "transcode list script not executable: $TRANSCODE_LIST_SCRIPT" >&2
  exit 1
fi

if [[ ! -f "$NORMALIZE_SCRIPT" ]]; then
  echo "normalize_access_audio.py not found: $NORMALIZE_SCRIPT" >&2
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

planned_stems=()
planned_access_files=()
planned_audio_files=()
planned_output_files=()
planned_count=0
missing_audio_count=0
existing_output_count=0

while IFS= read -r task; do
  [[ -z "$task" ]] && continue
  parts=($task)
  input_file=${parts[-1]}
  stem=$(basename "$input_file" .mkv)
  access_file="${access_dir%/}/$stem.mp4"
  audio_file="${audio_dir%/}/$stem.flac"
  output_file="${output_dir%/}/$stem.mp4"
  if [[ ! -f "$access_file" ]]; then
    echo "Expected Access file not found: $access_file" >&2
    exit 1
  fi

  planned_stems+=("$stem")
  planned_access_files+=("$access_file")
  planned_audio_files+=("$audio_file")
  planned_output_files+=("$output_file")
  ((planned_count += 1))

  if [[ ! -f "$audio_file" ]]; then
    ((missing_audio_count += 1))
  fi
  if [[ -e "$output_file" ]]; then
    ((existing_output_count += 1))
  fi
done < <(emit_tasks)

if (( planned_count == 0 )); then
  echo "No transcode source files found." >&2
  exit 1
fi

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
  local total_files=$planned_count
  local index=0
  local access_parent=""
  local num_width=${#total_files}
  local file_width=4
  local access_width=6
  local audio_width=5
  local output_width=6
  local audio_status_width=5
  local output_status_width=6

  echo "Found $total_files source file(s) in transcode list: $TRANSCODE_LIST_SCRIPT"
  echo "Using access directory: $access_dir"
  echo "Using audio directory: $audio_dir"
  echo "Output directory: $output_dir"
  echo "Metadata CSV: ${output_dir%/}/metadata.csv"
  echo "Matching FLAC files missing: $missing_audio_count"
  echo "Planned outputs already present: $existing_output_count"

  if (( total_files == 0 )); then
    echo "No source files found."
    return 0
  fi

  access_parent="$(common_parent "${planned_access_files[@]}")"
  echo "Access parent: $access_parent"
  echo "Planned normalization list:"

  while (( index < total_files )); do
    local stem="${planned_stems[$index]}"
    local file_name="$stem.mp4"
    local access_file="${planned_access_files[$index]}"
    local display_access
    local audio_file="${planned_audio_files[$index]}"
    local output_file="${planned_output_files[$index]}"
    local audio_status="missing"
    local output_status="will write"

    display_access="$(relative_to_parent "$access_parent" "$access_file")"
    if [[ -f "$audio_file" ]]; then
      audio_status="found"
    fi
    if [[ -e "$output_file" ]]; then
      if (( force )); then
        output_status="exists, overwrite"
      else
        output_status="exists, needs --force"
      fi
    fi

    (( ${#file_name} > file_width )) && file_width=${#file_name}
    (( ${#display_access} > access_width )) && access_width=${#display_access}
    (( ${#audio_file} > audio_width )) && audio_width=${#audio_file}
    (( ${#output_file} > output_width )) && output_width=${#output_file}
    (( ${#audio_status} > audio_status_width )) && audio_status_width=${#audio_status}
    (( ${#output_status} > output_status_width )) && output_status_width=${#output_status}
    ((index += 1))
  done

  printf '  %*s  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "#" "$file_width" "file" "$access_width" "access" "$audio_width" "audio" "$output_width" "output" "$audio_status_width" "audio" "$output_status_width" "status"
  printf '  %*s  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "---" "$file_width" "----" "$access_width" "------" "$audio_width" "-----" "$output_width" "------" "$audio_status_width" "-----" "$output_status_width" "------"
  index=0
  while (( index < total_files )); do
    local stem="${planned_stems[$index]}"
    local file_name="$stem.mp4"
    local access_file="${planned_access_files[$index]}"
    local display_access
    local audio_file="${planned_audio_files[$index]}"
    local output_file="${planned_output_files[$index]}"
    local audio_status="missing"
    display_access="$(relative_to_parent "$access_parent" "$access_file")"
    if [[ -f "$audio_file" ]]; then
      audio_status="found"
    fi
    local output_status="will write"
    if [[ -e "$output_file" ]]; then
      if (( force )); then
        output_status="exists, overwrite"
      else
        output_status="exists, needs --force"
      fi
    fi
    printf '  %*d  %-*s  %-*s  %-*s  %-*s  %-*s  %-*s\n' "$num_width" "$((index + 1))" "$file_width" "$file_name" "$access_width" "$display_access" "$audio_width" "$audio_file" "$output_width" "$output_file" "$audio_status_width" "$audio_status" "$output_status_width" "$output_status"
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
    read -r -p "Proceed with these normalization jobs? [y/N] " answer
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

tmp_access="$(mktemp -d)"
trap 'rm -rf "$tmp_access"' EXIT

prepare_access_workdir() {
  local index=0
  while (( index < planned_count )); do
    ln -sf "${planned_access_files[$index]}" "$tmp_access/${planned_stems[$index]}.mp4"
    ((index += 1))
  done
}

run_normalize() {
  local -a cmd=(uv --project "$UV_PROJECT_DIR" run "$NORMALIZE_SCRIPT" \
    --access-copy-dir "$tmp_access" \
    --audio-dir "$audio_dir" \
    --output-dir "$output_dir")
  if (( force )); then
    cmd+=(--force)
  fi
  "${cmd[@]}"
}

prepare_access_workdir
run_normalize
