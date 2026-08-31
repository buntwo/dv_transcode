#!/usr/bin/env bash

# Shared implementation for the Digital8 batch entrypoints in this directory.
# shellcheck disable=SC2154  # Manifest variables are defined by the sourcing entrypoint.
# The entrypoint must define:
#   batch_name, batch_dir
#   split_job_dirs, split_job_inputs, split_job_specs, split_job_flags
#   whole_inputs (paths relative to the Originals root)
# The entrypoint may define:
#   ignored_inputs (source paths that must exist but must not be transcoded)

run_digital8_split_transcode_batch() {
  local plan_only=false
  local keep_parts=false
  local overwrite_access=false
  local data_root
  data_root=$(cd "$repo_root/.." && pwd)

  if ! declare -p ignored_inputs >/dev/null 2>&1; then
    ignored_inputs=()
  fi

  while (($#)); do
    case "$1" in
      --plan-only)
        plan_only=true
        ;;
      --keep-parts)
        keep_parts=true
        ;;
      --overwrite-access)
        overwrite_access=true
        ;;
      --data-root)
        if (($# < 2)); then
          echo "--data-root requires a path" >&2
          return 2
        fi
        data_root=$2
        shift
        ;;
      -h|--help)
        cat <<EOF
Usage: $0 [--plan-only] [options]

Split/unsplit the historically selected ${batch_name} captures and transcode
the resulting logical parts plus every capture that was intentionally left whole.

Options:
  --plan-only          Print the complete plan without reading or writing media
  --keep-parts         Keep generated lettered DV files after successful transcode
  --overwrite-access   Permit replacement of matching existing access MP4s
  --data-root PATH     Media root (default: parent of this repository)
  -h, --help           Show this help

Default media directories under PATH:
  8mm Originals, 8mm Access, 8mm Logs
EOF
        return 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        echo "Run $0 --help for usage." >&2
        return 2
        ;;
    esac
    shift
  done

  local originals_dirname="8mm Originals"
  local access_dirname="8mm Access"
  local logs_dirname="8mm Logs"
  local originals_root="$data_root/$originals_dirname"
  local access_root="$data_root/$access_dirname"
  local unpackager="$repo_root/scripts/dv_unpackager.py"
  local transcoder="$repo_root/scripts/transcode3.py"
  local access_dir_path
  local -a existing_access_dirs=()

  if [[ -d "$access_root/$batch_dir" ]]; then
    while IFS= read -r -d '' access_dir_path; do
      existing_access_dirs+=("${access_dir_path#"$access_root/"}")
    done < <(find "$access_root/$batch_dir" -mindepth 1 -maxdepth 1 -type d -print0)
  fi

  local split_job_count=${#split_job_dirs[@]}
  if [[ $split_job_count -eq ${#split_job_inputs[@]} &&
        $split_job_count -eq ${#split_job_specs[@]} &&
        $split_job_count -eq ${#split_job_flags[@]} ]]; then
    :
  else
    echo "Internal error: split-job manifest columns have different lengths." >&2
    return 1
  fi

  local letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
  local -a expected_parts=()

  access_directory_exists() {
    local relative_dir=$1
    local existing_dir

    if ((${#existing_access_dirs[@]} == 0)); then
      return 1
    fi
    for existing_dir in "${existing_access_dirs[@]}"; do
      if [[ "$existing_dir" == "$relative_dir" ]]; then
        return 0
      fi
    done
    return 1
  }

  build_expected_parts() {
    local relative_dir=$1
    local input_name=$2
    local spec=$3
    local stem=${input_name%.dv}
    local -a groups=()
    local i

    IFS=',' read -r -a groups <<< "$spec"
    if ((${#groups[@]} > ${#letters})); then
      echo "Too many output groups in spec: $spec" >&2
      return 1
    fi

    expected_parts=()
    for ((i = 0; i < ${#groups[@]}; i++)); do
      expected_parts+=("$originals_root/$relative_dir/${stem}_part${letters:i:1}.dv")
    done
  }

  access_output_matches() {
    local input_file=$1
    local relative=${input_file#"$originals_root/"}
    local relative_dir=${relative%/*}
    local stem=${input_file##*/}
    stem=${stem%.dv}
    local set_name=${relative_dir%%/*}
    local child_and_rest=${relative_dir#*/}
    local child=${child_and_rest%%/*}
    local child_prefix=${child%% *}
    local prefix=${set_name// /_}_$child_prefix
    local output_dir="$access_root/$relative_dir"
    local base_name="${prefix}_${stem}.mp4"
    local -a matches=()
    local -a dated_matches=()

    if [[ -f "$output_dir/$base_name" ]]; then
      matches+=("$output_dir/$base_name")
    fi
    shopt -s nullglob
    dated_matches=("$output_dir/"[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_"$base_name")
    shopt -u nullglob
    if ((${#dated_matches[@]} > 0)); then
      matches+=("${dated_matches[@]}")
    fi
    if ((${#matches[@]} > 0)); then
      printf '%s\n' "${matches[@]}"
    fi
  }

  guard_access_output() {
    local input_file=$1
    local -a matches=()
    while IFS= read -r match; do
      [[ -n "$match" ]] && matches+=("$match")
    done < <(access_output_matches "$input_file")

    if ((${#matches[@]} > 1)); then
      echo "Refusing ambiguous access outputs for: $input_file" >&2
      printf '  %s\n' "${matches[@]}" >&2
      return 1
    fi
    if ((${#matches[@]} == 1)) && [[ "$overwrite_access" != true ]]; then
      echo "Refusing to overwrite existing access output: ${matches[0]}" >&2
      echo "Use --overwrite-access only after confirming it should be replaced." >&2
      return 1
    fi
  }

  validate_expected_parts() {
    local relative_dir=$1
    local input_name=$2
    local spec=$3
    local stem=${input_name%.dv}
    local tape_dir="$originals_root/$relative_dir"
    local -a found=()
    local part

    build_expected_parts "$relative_dir" "$input_name" "$spec"
    shopt -s nullglob
    found=("$tape_dir/${stem}_part"*.dv)
    shopt -u nullglob

    if ((${#found[@]} != ${#expected_parts[@]})); then
      return 1
    fi
    for part in "${expected_parts[@]}"; do
      [[ -f "$part" ]] || return 1
    done
  }

  print_plan() {
    local i input_file relative_dir
    local -a flags=()

    echo "$batch_name Digital8 workflow"
    echo "  Data root:  $data_root"
    echo "  Originals:  $originals_root"
    echo "  Access:     $access_root"
    echo "  Logs:       $data_root/$logs_dirname"
    echo "  Transcode:  libx265, preset slow, CRF 20, denoise verylight"
    if [[ "$keep_parts" == true ]]; then
      echo "  DV parts:   keep"
    else
      echo "  DV parts:   remove after each successful validated transcode"
    fi
    echo
    echo "Split/unsplit jobs (${#split_job_dirs[@]}):"
    for ((i = 0; i < ${#split_job_dirs[@]}; i++)); do
      IFS=' ' read -r -a flags <<< "${split_job_flags[i]}"
      build_expected_parts "${split_job_dirs[i]}" "${split_job_inputs[i]}" "${split_job_specs[i]}"
      if access_directory_exists "${split_job_dirs[i]}"; then
        printf '  %s/%s [skip: Access directory exists]\n' "${split_job_dirs[i]}" "${split_job_inputs[i]}"
      else
        printf '  %s/%s\n' "${split_job_dirs[i]}" "${split_job_inputs[i]}"
      fi
      printf '    flags: %s\n' "${flags[*]}"
      printf '    groups: %s\n' "${split_job_specs[i]}"
      printf '    outputs: %s\n' "${expected_parts[*]##*/}"
    done
    echo
    echo "Captures left whole (${#whole_inputs[@]}):"
    for input_file in "${whole_inputs[@]}"; do
      relative_dir=${input_file%/*}
      if access_directory_exists "$relative_dir"; then
        printf '  %s [skip: Access directory exists]\n' "$input_file"
      else
        printf '  %s\n' "$input_file"
      fi
    done
    if ((${#ignored_inputs[@]} > 0)); then
      echo
      echo "Captures intentionally ignored (${#ignored_inputs[@]}):"
      for input_file in "${ignored_inputs[@]}"; do
        printf '  %s\n' "$input_file"
      done
    fi
  }

  print_plan
  if [[ "$plan_only" == true ]]; then
    return 0
  fi

  local command_name
  for command_name in uv dvpackager dvrescue ffmpeg ffprobe; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      echo "Required command not found: $command_name" >&2
      return 1
    fi
  done
  if [[ ! -f "$unpackager" || ! -f "$transcoder" ]]; then
    echo "Repository tools not found under: $repo_root/scripts" >&2
    return 1
  fi
  if [[ ! -d "$originals_root/$batch_dir" ]]; then
    echo "Batch source directory not found: $originals_root/$batch_dir" >&2
    return 1
  fi

  local i relative_dir input_name spec flags_text input_file split_dir part
  local -a flags=()
  local -a expected_sources=()
  local -a actual_sources=()

  # Check every source before beginning hours of media work.
  for ((i = 0; i < ${#split_job_dirs[@]}; i++)); do
    input_file="$originals_root/${split_job_dirs[i]}/${split_job_inputs[i]}"
    expected_sources+=("$input_file")
    if [[ ! -f "$input_file" ]]; then
      echo "Missing split source: $input_file" >&2
      return 1
    fi
  done
  for input_file in "${whole_inputs[@]}"; do
    expected_sources+=("$originals_root/$input_file")
    if [[ ! -f "$originals_root/$input_file" ]]; then
      echo "Missing whole source: $originals_root/$input_file" >&2
      return 1
    fi
  done
  for input_file in "${ignored_inputs[@]}"; do
    expected_sources+=("$originals_root/$input_file")
    if [[ ! -f "$originals_root/$input_file" ]]; then
      echo "Missing intentionally ignored source: $originals_root/$input_file" >&2
      return 1
    fi
  done

  while IFS= read -r -d '' input_file; do
    actual_sources+=("$input_file")
  done < <(
    find "$originals_root/$batch_dir" \
      \( -type d \( -name retake -o -name retakes -o -name split \) -prune \) -o \
      -type f -name '*.dv' ! -name '*_part[A-Z]*.dv' -print0
  )
  if ((${#actual_sources[@]} != ${#expected_sources[@]})); then
    echo "Source inventory differs from the verified master manifest." >&2
    echo "Expected ${#expected_sources[@]} source DV files; found ${#actual_sources[@]}." >&2
    return 1
  fi
  for input_file in "${actual_sources[@]}"; do
    local source_is_expected=false
    for part in "${expected_sources[@]}"; do
      if [[ "$input_file" == "$part" ]]; then
        source_is_expected=true
        break
      fi
    done
    if [[ "$source_is_expected" != true ]]; then
      echo "Unexpected source DV outside the verified manifest: $input_file" >&2
      return 1
    fi
  done

  # Refuse all output collisions before generating any intermediates.
  for ((i = 0; i < ${#split_job_dirs[@]}; i++)); do
    if access_directory_exists "${split_job_dirs[i]}"; then
      continue
    fi
    build_expected_parts "${split_job_dirs[i]}" "${split_job_inputs[i]}" "${split_job_specs[i]}"
    for part in "${expected_parts[@]}"; do
      guard_access_output "$part"
    done
  done
  for input_file in "${whole_inputs[@]}"; do
    if access_directory_exists "${input_file%/*}"; then
      continue
    fi
    guard_access_output "$originals_root/$input_file"
  done

  for ((i = 0; i < ${#split_job_dirs[@]}; i++)); do
    relative_dir=${split_job_dirs[i]}
    input_name=${split_job_inputs[i]}
    spec=${split_job_specs[i]}
    flags_text=${split_job_flags[i]}
    input_file="$originals_root/$relative_dir/$input_name"
    split_dir="$originals_root/$relative_dir/split"
    IFS=' ' read -r -a flags <<< "$flags_text"

    if access_directory_exists "$relative_dir"; then
      echo
      echo "Skipping completed Access directory: $access_root/$relative_dir"
      continue
    fi

    if ! validate_expected_parts "$relative_dir" "$input_name" "$spec"; then
      build_expected_parts "$relative_dir" "$input_name" "$spec"
      local -a found_parts=()
      shopt -s nullglob
      found_parts=("$originals_root/$relative_dir/${input_name%.dv}_part"*.dv)
      shopt -u nullglob

      if ((${#found_parts[@]} > 0)); then
        echo "Partial or unexpected logical parts exist for: $input_file" >&2
        printf '  %s\n' "${found_parts[@]}" >&2
        return 1
      fi
      if [[ -d "$split_dir" ]] && [[ -n "$(find "$split_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        local split_metadata="$split_dir/$input_name.dvrescue.xml"
        local split_entry_count
        split_entry_count=$(find "$split_dir" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d ' ')
        if ((split_entry_count == 1)) && [[ -f "$split_metadata" ]]; then
          echo "Removing metadata-only failed split directory: $split_dir"
          rm -f -- "$split_metadata"
          rmdir -- "$split_dir"
        else
          echo "Refusing non-empty split directory: $split_dir" >&2
          return 1
        fi
      fi

      echo
      echo "Splitting and regrouping: $input_file"
      uv --project "$repo_root" run "$unpackager" \
        --originals-dirname "$originals_dirname" \
        --logs-dirname "$logs_dirname" \
        split-unsplit "$input_file" "$spec" "${flags[@]}"

      if ! validate_expected_parts "$relative_dir" "$input_name" "$spec"; then
        echo "Split/unsplit did not create the exact expected outputs for: $input_file" >&2
        return 1
      fi

      # The numbered split files are generated intermediates. The archived master
      # also omits them, and removing them keeps peak scratch usage to one tape.
      if [[ -d "$split_dir" && "$split_dir" == "$originals_root/$relative_dir/split" ]]; then
        rm -rf -- "$split_dir"
      fi
    else
      echo
      echo "Reusing complete logical parts: $input_file"
    fi

    build_expected_parts "$relative_dir" "$input_name" "$spec"
    echo "Transcoding ${#expected_parts[@]} logical part(s): $relative_dir"
    if ! uv --project "$repo_root" run "$transcoder" \
      --mode transcode \
      --format digital8 \
      --encoder libx265 \
      --preset slow \
      --crf 20 \
      --denoise verylight \
      --originals-dirname "$originals_dirname" \
      --access-dirname "$access_dirname" \
      --logs-dirname "$logs_dirname" \
      --yes \
      "${expected_parts[@]}"; then
      echo "Transcode failed; retaining logical DV parts for recovery: $relative_dir" >&2
      return 1
    fi

    if [[ "$keep_parts" != true ]]; then
      for part in "${expected_parts[@]}"; do
        if [[ "$part" != "$originals_root/$relative_dir/${input_name%.dv}_part"*.dv ]]; then
          echo "Refusing unsafe generated-part cleanup path: $part" >&2
          return 1
        fi
      done
      rm -f -- "${expected_parts[@]}"
      echo "Removed successfully transcoded logical DV intermediates: $relative_dir"
    fi
  done

  for input_file in "${whole_inputs[@]}"; do
    if access_directory_exists "${input_file%/*}"; then
      echo
      echo "Skipping completed Access directory: $access_root/${input_file%/*}"
      continue
    fi
    input_file="$originals_root/$input_file"
    echo
    echo "Transcoding whole capture: $input_file"
    uv --project "$repo_root" run "$transcoder" \
      --mode transcode \
      --format digital8 \
      --encoder libx265 \
      --preset slow \
      --crf 20 \
      --denoise verylight \
      --originals-dirname "$originals_dirname" \
      --access-dirname "$access_dirname" \
      --logs-dirname "$logs_dirname" \
      --yes \
      "$input_file"
  done

  echo
  echo "$batch_name complete."
}
