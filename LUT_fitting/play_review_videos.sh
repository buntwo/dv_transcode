#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lut_root="$script_dir"
expt9d_transformed="$lut_root/generated_video_pairs/evaluations/expt9D_builtin_filters/transformed_videos"
player="$lut_root/../scripts/multi_player.py"

cd "$lut_root" || exit 1

play_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/expt9F_yuv_only_search/transformed_videos"

  if [[ ! -d "$master_root" ]]; then
    echo "Missing master transformed video directory: $master_root" >&2
    exit 1
  fi

  find "$master_root" -mindepth 2 -maxdepth 2 -type f \( -name 'clip_*_control.mkv' ! -path '*Y2K*' \) -print | sort | while IFS= read -r control; do
    local optimized="${control%_control.mkv}_optimized.mkv"
    if [[ ! -f "$optimized" ]]; then
      echo "Missing expected optimized clip: $optimized" >&2
      exit 1
    fi

    local master_name
    master_name="$(basename "$(dirname "$control")")"
    echo
    echo "Playing master $master_name $(basename "$control" _control.mkv)"
    echo "  1 control:   $control"
    echo "  2 optimized: $optimized"
    echo

    uv run python "$player" "$control" "$optimized" < /dev/tty
    status=$?
    if [[ $status -ne 0 ]]; then
      if [[ $status -ge 128 ]]; then
        echo "multi_player was interrupted; stopping playback loop." >&2
        exit "$status"
      fi
      echo "multi_player exited with status $status; continuing to next clip." >&2
    fi
  done
}

play_expt11_access_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/expt11_gamma_weight_search/transformed_videos/access_master_clips"

  if [[ ! -d "$master_root" ]]; then
    echo "Missing expt11 Access master clip directory: $master_root" >&2
    exit 1
  fi

  find "$master_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' ! -path '*Y2K*' -print | sort | while IFS= read -r ctrl; do
    local stem="${ctrl%_ctrl.mkv}"
    local previous_50pct="${stem}_previous_50pct.mkv"
    local expt11_best="${stem}_expt11_best.mkv"
    local expt11_best_50pct="${stem}_expt11_best_50pct.mkv"

    for path in "$ctrl" "$previous_50pct" "$expt11_best" "$expt11_best_50pct"; do
      if [[ ! -f "$path" ]]; then
        echo "Missing expected video: $path" >&2
        exit 1
      fi
    done

    local access_name
    access_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing expt11 Access master $access_name $(basename "$stem")"
    echo "  1 ctrl:              $ctrl"
    echo "  2 previous_50pct:    $previous_50pct"
    echo "  3 expt11_best:       $expt11_best"
    echo "  4 expt11_best_50pct: $expt11_best_50pct"
    echo

    uv run python "$player" "$ctrl" "$previous_50pct" "$expt11_best" "$expt11_best_50pct" < /dev/tty
    status=$?
    if [[ $status -ne 0 ]]; then
      if [[ $status -ge 128 ]]; then
        echo "multi_player was interrupted; stopping playback loop." >&2
        exit "$status"
      fi
      echo "multi_player exited with status $status; continuing to next clip." >&2
    fi
  done
}

play_final_masters() {
  local master_root="$lut_root/generated_video_pairs/evaluations/final_visual_winner/transformed_videos/masters"

  if [[ ! -d "$master_root" ]]; then
    echo "Missing final winner master clip directory: $master_root" >&2
    exit 1
  fi

  find "$master_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' ! -path '*Y2K*' -print | sort | while IFS= read -r ctrl; do
    local winner="${ctrl%_ctrl.mkv}_winner.mkv"
    if [[ ! -f "$winner" ]]; then
      echo "Missing expected winner clip: $winner" >&2
      exit 1
    fi

    local master_name
    master_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing final winner master $master_name $(basename "$ctrl" _ctrl.mkv)"
    echo "  1 ctrl:   $ctrl"
    echo "  2 winner: $winner"
    echo

    uv run python "$player" "$ctrl" "$winner" < /dev/tty
    status=$?
    if [[ $status -ne 0 ]]; then
      if [[ $status -ge 128 ]]; then
        echo "multi_player was interrupted; stopping playback loop." >&2
        exit "$status"
      fi
      echo "multi_player exited with status $status; continuing to next clip." >&2
    fi
  done
}

play_final_pairs() {
  local pair_root="$lut_root/generated_video_pairs/evaluations/final_visual_winner/transformed_videos/pairs"
  local split

  if [[ ! -d "$pair_root" ]]; then
    echo "Missing final winner pair clip directory: $pair_root" >&2
    exit 1
  fi

  for split in train validation; do
    find "$pair_root/$split" -maxdepth 1 -type f -name 'pair_*_ctrl.mkv' ! -path '*Y2K*' -print | sort | while IFS= read -r ctrl; do
      local stem="${ctrl%_ctrl.mkv}"
      local winner="${stem}_winner.mkv"
      local video8="${stem}_video8.mkv"

      for path in "$ctrl" "$winner" "$video8"; do
        if [[ ! -f "$path" ]]; then
          echo "Missing expected video: $path" >&2
          exit 1
        fi
      done

      echo
      echo "Playing final winner $split $(basename "$stem")"
      echo "  1 ctrl:   $ctrl"
      echo "  2 winner: $winner"
      echo "  3 video8: $video8"
      echo

      uv run python "$player" "$ctrl" "$winner" "$video8" < /dev/tty
      status=$?
      if [[ $status -ne 0 ]]; then
        if [[ $status -ge 128 ]]; then
          echo "multi_player was interrupted; stopping playback loop." >&2
          exit "$status"
        fi
        echo "multi_player exited with status $status; continuing to next pair." >&2
      fi
    done
  done
}

play_denoise_review() {
  local review_root="$lut_root/generated_video_pairs/evaluations/expt12_denoise_workflow_review/transformed_videos/access_master_clips"

  if [[ ! -d "$review_root" ]]; then
    echo "Missing denoise review directory: $review_root" >&2
    exit 1
  fi

  find "$review_root" -mindepth 2 -maxdepth 2 -type f -name 'clip_*_ctrl.mkv' -print | sort | while IFS= read -r ctrl; do
    local stem="${ctrl%_ctrl.mkv}"
    local with_denoise="${stem}_with_denoise.mkv"
    local no_denoise="${stem}_no_denoise.mkv"
    local lanczos="${stem}_with_denoise_lanczos.mkv"

    for path in "$ctrl" "$with_denoise" "$no_denoise" "$lanczos"; do
      if [[ ! -f "$path" ]]; then
        echo "Missing expected video: $path" >&2
        exit 1
      fi
    done

    local access_name
    access_name="$(basename "$(dirname "$ctrl")")"
    echo
    echo "Playing denoise review $access_name $(basename "$stem")"
    echo "  1 ctrl workflow:       $ctrl"
    echo "  2 with denoise:        $with_denoise"
    echo "  3 without denoise:     $no_denoise"
    echo "  4 denoise + lanczos:   $lanczos"
    echo

    uv run python "$player" "$ctrl" "$with_denoise" "$no_denoise" "$lanczos" < /dev/tty
    status=$?
    if [[ $status -ne 0 ]]; then
      if [[ $status -ge 128 ]]; then
        echo "multi_player was interrupted; stopping playback loop." >&2
        exit "$status"
      fi
      echo "multi_player exited with status $status; continuing to next clip." >&2
    fi
  done
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
    local greyedge="$expt9d_transformed/g_opt_greyedge/$split/${pair}_B.mkv"
    local g_opt="$expt9d_transformed/g_opt/$split/${pair}_B.mkv"
    local cc_opt="$expt9d_transformed/g_opt_cc_opt/$split/${pair}_B.mkv"
    local prev_best="$expt9d_transformed/previous_best_lut/$split/${pair}_B.mkv"
    local pure="$expt9d_transformed/pure_filtergraph_cc_opt/$split/${pair}_B.mkv"
    local pure_nosat="$expt9d_transformed/pure_filtergraph_cc_nosat/$split/${pair}_B.mkv"

    for path in "$video_a" "$video_b" "$greyedge" "$cc_opt"; do
      if [[ ! -f "$path" ]]; then
        echo "Missing expected video: $path" >&2
        exit 1
      fi
    done

    echo
    echo "Playing $split $pair"
    #echo "  1 A:                $video_a"
    ##echo "  2 B:                $video_b"
    #echo "  3 B_g_opt_greyedge: $greyedge"
    #echo "  4 B_g_opt_cc_opt:   $cc_opt"
    #echo

    v1="$video_a"
    #v2="$video_b"
    v2="$cc_opt"
    v3="$pure"
    #v4="$greyedge"
    v4="$pure_nosat"

    uv run python "$player" "$v1" "$v2" "$v3" "$v4" < /dev/tty
    status=$?
    if [[ $status -ne 0 ]]; then
      if [[ $status -ge 128 ]]; then
        echo "multi_player was interrupted; stopping playback loop." >&2
        exit "$status"
      fi
      echo "multi_player exited with status $status; continuing to next pair." >&2
    fi
  done 3< "$manifest"
}

if [[ "${1:-}" == "masters" || "${1:-}" == "--masters" ]]; then
  play_masters
  exit 0
fi

if [[ "${1:-}" == "expt11-access" || "${1:-}" == "--expt11-access" || "${1:-}" == "access-masters" ]]; then
  play_expt11_access_masters
  exit 0
fi

if [[ "${1:-}" == "final-masters" || "${1:-}" == "--final-masters" || "${1:-}" == "winner-masters" ]]; then
  play_final_masters
  exit 0
fi

if [[ "${1:-}" == "final-pairs" || "${1:-}" == "--final-pairs" || "${1:-}" == "winner-pairs" ]]; then
  play_final_pairs
  exit 0
fi

if [[ "${1:-}" == "denoise-review" || "${1:-}" == "--denoise-review" || "${1:-}" == "expt12-denoise" ]]; then
  play_denoise_review
  exit 0
fi

play_split "train" "generated_video_pairs/train_geometry_normalized_pairs.txt"
play_split "validation" "generated_video_pairs/validation_geometry_normalized_pairs.txt"
