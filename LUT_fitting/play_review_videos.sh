#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lut_root="$script_dir"
expt9d_transformed="$lut_root/generated_video_pairs/evaluations/expt9D_builtin_filters/transformed_videos"
player="$lut_root/../scripts/multi_player.py"

cd "$lut_root" || exit 1

require_dir() {
  local path="$1"
  local label="$2"

  if [[ ! -d "$path" ]]; then
    echo "Missing ${label}directory: $path" >&2
    exit 1
  fi
}

require_files() {
  local path
  for path in "$@"; do
    if [[ ! -f "$path" ]]; then
      echo "Missing expected video: $path" >&2
      exit 1
    fi
  done
}

run_player() {
  local status

  uv run python "$player" "$@" < /dev/tty
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "multi_player exited with status $status; exiting." >&2
    exit "$status"
  fi
}

play_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/expt9F_yuv_only_search/transformed_videos"

  require_dir "$master_root" "master transformed video "

  while IFS= read -r control; do
    local optimized="${control%_control.mkv}_optimized.mkv"
    require_files "$control" "$optimized"

    local master_name
    master_name="$(basename "$(dirname "$control")")"
    echo
    echo "Playing master $master_name $(basename "$control" _control.mkv)"
    echo "  1 control:   $control"
    echo "  2 optimized: $optimized"
    echo

    run_player "$control" "$optimized"
  done < <(find "$master_root" -mindepth 2 -maxdepth 2 -type f \( -name 'clip_*_control.mkv' ! -path '*Y2K*' \) -print | sort)
}

play_expt11_access_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/expt11_gamma_weight_search/transformed_videos/access_master_clips"

  require_dir "$master_root" "expt11 Access master clip "

  while IFS= read -r ctrl; do
    local stem="${ctrl%_ctrl.mkv}"
    local previous_50pct="${stem}_previous_50pct.mkv"
    local expt11_best="${stem}_expt11_best.mkv"
    local expt11_best_50pct="${stem}_expt11_best_50pct.mkv"

    require_files "$ctrl" "$previous_50pct" "$expt11_best" "$expt11_best_50pct"

    local access_name
    access_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing expt11 Access master $access_name $(basename "$stem")"
    echo "  1 ctrl:              $ctrl"
    echo "  2 previous_50pct:    $previous_50pct"
    echo "  3 expt11_best:       $expt11_best"
    echo "  4 expt11_best_50pct: $expt11_best_50pct"
    echo

    run_player "$ctrl" "$previous_50pct" "$expt11_best" "$expt11_best_50pct"
  done < <(find "$master_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' ! -path '*Y2K*' -print | sort)
}

play_final_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/final_visual_winner/transformed_videos/masters"

  require_dir "$master_root" "final winner master clip "

  while IFS= read -r ctrl; do
    local winner="${ctrl%_ctrl.mkv}_winner.mkv"
    require_files "$ctrl" "$winner"

    local master_name
    master_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing final winner master $master_name $(basename "$ctrl" _ctrl.mkv)"
    echo "  1 ctrl:   $ctrl"
    echo "  2 winner: $winner"
    echo

    run_player "$ctrl" "$winner"
  done < <(find "$master_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' ! -path '*Y2K*' -print | sort)
}

play_final_pairs() {
  local pair_root="$lut_root/generated_video_pairs/evaluations/final_visual_winner/transformed_videos/pairs"
  local split

  require_dir "$pair_root" "final winner pair clip "

  for split in train validation; do
    while IFS= read -r ctrl; do
      local stem="${ctrl%_ctrl.mkv}"
      local winner="${stem}_winner.mkv"
      local video8="${stem}_video8.mkv"

      require_files "$ctrl" "$winner" "$video8"

      echo
      echo "Playing final winner $split $(basename "$stem")"
      echo "  1 ctrl:   $ctrl"
      echo "  2 winner: $winner"
      echo "  3 video8: $video8"
      echo

      run_player "$ctrl" "$winner" "$video8"
    done < <(find "$pair_root/$split" -maxdepth 1 -type f -name 'pair_*_ctrl.mkv' ! -path '*Y2K*' -print | sort)
  done
}

play_denoise_review() {
  local review_root="$lut_root/generated_video_pairs/evaluations/expt12_denoise_workflow_review/transformed_videos/access_master_clips"

  require_dir "$review_root" "denoise review "

  while IFS= read -r ctrl; do
    local stem="${ctrl%_ctrl.mkv}"
    local with_denoise="${stem}_with_denoise.mkv"
    local no_denoise="${stem}_no_denoise.mkv"
    local lanczos="${stem}_with_denoise_lanczos.mkv"

    require_files "$ctrl" "$with_denoise" "$no_denoise" "$lanczos"

    local access_name
    access_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing denoise review $access_name $(basename "$stem")"
    echo "  1 ctrl workflow:       $ctrl"
    echo "  2 with denoise:        $with_denoise"
    echo "  3 without denoise:     $no_denoise"
    echo "  4 denoise + lanczos:   $lanczos"
    echo

    run_player "$ctrl" "$with_denoise" "$no_denoise" "$lanczos"
  done < <(find "$review_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' -print | sort)
}

play_split() {
  local split="$1"
  local manifest="$2"
  local index=0

  while IFS='|' read -r video_a video_b <&3; do
    if [[ -z "${video_a:-}" || -z "${video_b:-}" ]]; then
      continue
    fi

    index=$((index + 1))
    local pair
    pair="$(printf 'pair_%03d' "$index")"
    local cc_opt="$expt9d_transformed/g_opt_cc_opt/$split/${pair}_B.mkv"
    local pure="$expt9d_transformed/pure_filtergraph_cc_opt/$split/${pair}_B.mkv"
    local pure_nosat="$expt9d_transformed/pure_filtergraph_cc_nosat/$split/${pair}_B.mkv"

    require_files "$video_a" "$cc_opt" "$pure" "$pure_nosat"

    echo
    echo "Playing $split $pair"

    run_player "$video_a" "$cc_opt" "$pure" "$pure_nosat"
  done 3< "$manifest"
}

case "${1:-}" in
  masters|--masters)
    play_masters
    ;;
  expt11-access|--expt11-access|access-masters)
    play_expt11_access_masters
    ;;
  final-masters|--final-masters|winner-masters)
    play_final_masters
    ;;
  final-pairs|--final-pairs|winner-pairs)
    play_final_pairs
    ;;
  denoise-review|--denoise-review|expt12-denoise)
    play_denoise_review
    ;;
  *)
    play_split "train" "generated_video_pairs/train_geometry_normalized_pairs.txt"
    play_split "validation" "generated_video_pairs/validation_geometry_normalized_pairs.txt"
    ;;
esac
