#!/usr/bin/env bash
set -euo pipefail

workflow_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$workflow_root/.." && pwd)
workspace_root=$(cd "$repo_root/.." && pwd)
transcoder="$repo_root/scripts/transcode_access.py"

delivery_20260702="$workspace_root/2.2, 2.4, audio/Archival Master Files [Video]/tu.brian.2026.07.02/data/masters/tape"
delivery_20260731='/Volumes/video masters/Brian Zoe childhood home videos - 8mm/Archival Works Bag 20260731/Tu.Brian.2026.07.31/data/masters/tape'

manifest=(
  "$delivery_20260702/063_12_13_98.mkv|2 12.13.98 -|Set_2_2_out.mp4"
  "$delivery_20260702/064_Brian_Piano_2001.mkv|4 Brian Piano 2001|Set_2_4_out.mp4"
  "$delivery_20260731/001_2001.mkv|1 2001|Set_2_1_out.mp4"
  "$delivery_20260731/002_1999.mkv|3 窗子漏水, 1999|Set_2_3_out.mp4"
  # Redo requested from the transfer company; uncomment this one line when replaced.
  # "$delivery_20260731/003_1999.mkv|5 1999|Set_2_5_out.mp4"
  "$delivery_20260731/004_12_99.mkv|6 破|Set_2_6_out.mp4"
  "$delivery_20260731/005_12_99_7.mkv|7 12.99|Set_2_7_out.mp4"
  "$delivery_20260731/006_1_00.mkv|8 1.00|Set_2_8_out.mp4"
  "$delivery_20260731/007_6_00.mkv|9 Nineth 6.00|Set_2_9_out.mp4"
  "$delivery_20260731/008_10_3_2000.mkv|11 10.3.00|Set_2_11_out.mp4"
)

source_files=()
target_dirs=()
target_names=()
for manifest_row in "${manifest[@]}"; do
  IFS='|' read -r source_file target_dir target_name <<< "$manifest_row"
  source_files+=("$source_file")
  target_dirs+=("$target_dir")
  target_names+=("$target_name")
done

plan_only=false
overwrite_access=false
data_root="$workspace_root"

usage() {
  cat <<EOF
Usage: $0 [--plan-only] [--overwrite-access] [--data-root PATH]

Transcode the enabled professional Set 2 FFV1 masters with the Video8 profile.

Options:
  --plan-only          Print the complete manifest without transcoding
  --overwrite-access   Permit replacement of completed access outputs
  --data-root PATH     Output root (default: directory containing this script)
  -h, --help           Show this help

Outputs are written below PATH/8mm Access/Set 2 and logs below
PATH/8mm Logs/Set 2.
EOF
}

while (($#)); do
  case "$1" in
    --plan-only)
      plan_only=true
      ;;
    --overwrite-access)
      overwrite_access=true
      ;;
    --data-root)
      if (($# < 2)); then
        echo "--data-root requires a path" >&2
        exit 2
      fi
      data_root=$2
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Run $0 --help for usage." >&2
      exit 2
      ;;
  esac
  shift
done

manifest_count=${#source_files[@]}
if ((manifest_count == ${#target_dirs[@]} &&
     manifest_count == ${#target_names[@]})); then
  :
else
  echo "Internal error: Set 2 manifest columns have different lengths." >&2
  exit 1
fi

access_root="$data_root/8mm Access/Set 2"
logs_root="$data_root/8mm Logs/Set 2"

echo "Set 2 professional-master transcode"
echo "  Access:     $access_root"
echo "  Logs:       $logs_root"
echo "  Transcode:  Video8, libx265 slow, CRF 20, denoise verylight"
echo "  Geometry:   crop 6 top / 0 bottom, preserve 4:3, mask bottom 7"
echo
echo "Manifest ($manifest_count masters):"

for ((i = 0; i < manifest_count; i++)); do
  final_output="$access_root/${target_dirs[i]}/${target_names[i]}"
  if [[ -f "$final_output" && "$overwrite_access" != true ]]; then
    status=' [skip: output exists]'
  else
    status=''
  fi
  printf '  %s\n    -> %s%s\n' "${source_files[i]}" "$final_output" "$status"
done

if [[ "$plan_only" == true ]]; then
  exit 0
fi

for command_name in uv ffmpeg ffprobe; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done
if [[ ! -f "$transcoder" ]]; then
  echo "Transcoder not found: $transcoder" >&2
  exit 1
fi

# Check the entire manifest before beginning hours of transcoding.
for source_file in "${source_files[@]}"; do
  if [[ ! -f "$source_file" ]]; then
    echo "Missing professional master: $source_file" >&2
    exit 1
  fi
done

for ((i = 0; i < manifest_count; i++)); do
  source_file=${source_files[i]}
  target_dir="$access_root/${target_dirs[i]}"
  log_dir="$logs_root/${target_dirs[i]}"
  final_output="$target_dir/${target_names[i]}"
  source_basename=${source_file##*/}
  source_stem=${source_basename%.mkv}
  transcoder_output="$target_dir/$source_stem.mp4"

  if [[ -f "$final_output" && "$overwrite_access" != true ]]; then
    echo
    echo "Skipping completed output: $final_output"
    continue
  fi
  if [[ -e "$transcoder_output" && "$transcoder_output" != "$final_output" ]]; then
    echo "Refusing unexpected source-named output: $transcoder_output" >&2
    echo "Move or remove it after checking whether an earlier run completed." >&2
    exit 1
  fi

  echo
  echo "Transcoding $source_basename"
  echo "  Output: $final_output"
  echo "  Logs:   $log_dir"

  uv --project "$repo_root" run "$transcoder" \
    --mode transcode \
    --format video8 \
    --crop-top 6 \
    --output-dir "$target_dir" \
    --log-dir "$log_dir" \
    --yes \
    "$source_file"

  if [[ ! -f "$transcoder_output" ]]; then
    echo "Transcoder completed without expected output: $transcoder_output" >&2
    exit 1
  fi
  mv -f "$transcoder_output" "$final_output"
  echo "Completed: $final_output"
done
