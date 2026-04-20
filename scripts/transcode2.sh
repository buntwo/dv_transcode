#!/usr/bin/env bash
# Transcode dv file to HEVC (H.265).
# Optimized for Video8, with bottom crop and pad to remove tape head noise/garbage.
# Coded by ChatGPT
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  transcode_dv_vt.sh [options] INPUT ACCESS_ROOT LOG_ROOT

Examples:
  transcode_dv_vt.sh "/Volumes/Tapes/Originals/Family/Tape 01.dv" "/Volumes/Tapes/Access"
  transcode_dv_vt.sh --mode preview --start 00:10:00 --end 00:11:00 \
    --crop-bottom 8 --denoise light \
    "/Volumes/Tapes/Originals/Family/Tape 01.dv" "/Volumes/Tapes/Access" Logs

Modes:
  --mode transcode   Write access copy to file (default)
  --mode preview     Run the exact same pipeline, but pipe the encoded result to ffplay

Options:
  --start HH:MM:SS
  --end HH:MM:SS
  --crop-bottom N        Rows to crop from bottom before padding back (default: 7)
  --pad-bottom N         Rows to pad back at bottom (default: same as crop-bottom)
  --denoise PRESET       off | verylight | light | medium | strong (default: light)
  --q N                  VideoToolbox quality value (default: 70)
  --codec CODEC          h264 | hevc (default: hevc)
  --deint-mode MODE      send_frame | send_field (default: send_field)
  --map-both-audio       Include both audio tracks if present
  --log-level LEVEL      quiet | error | warning | info (default: warning)
  --yes                  Skip the Enter-to-start confirmation in transcode mode
  --output-suffix        Suffix to put in auto generated output filename
  -h, --help
EOF
}

# Defaults
MODE="transcode"
START=""
END=""
BOTTOM_CROP_ROWS=7
BOTTOM_PAD_ROWS=""
DENOISE_PRESET="light"
VIDEO_Q=70
AUDIO_BITRATE="192k"
VIDEO_CODEC="hevc"
DEINT_MODE="send_field"
MAP_BOTH_AUDIO=false
LOG_LEVEL="warning"
ASSUME_YES=false
OUTPUT_SUFFIX=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"; shift 2 ;;
    --start)
      START="$2"; shift 2 ;;
    --end)
      END="$2"; shift 2 ;;
    --crop-bottom)
      BOTTOM_CROP_ROWS="$2"; shift 2 ;;
    --pad-bottom)
      BOTTOM_PAD_ROWS="$2"; shift 2 ;;
    --denoise)
      DENOISE_PRESET="$2"; shift 2 ;;
    --q)
      VIDEO_Q="$2"; shift 2 ;;
    --codec)
      VIDEO_CODEC="$2"; shift 2 ;;
    --deint-mode)
      DEINT_MODE="$2"; shift 2 ;;
    --map-both-audio)
      MAP_BOTH_AUDIO=true; shift ;;
    --log-level)
      LOG_LEVEL="$2"; shift 2 ;;
    --yes)
      ASSUME_YES=true; shift ;;
    --output-suffix)
      OUTPUT_SUFFIX="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; break ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1 ;;
    *)
      break ;;
  esac
done

if [[ $# -ne 3 ]]; then
  usage >&2
  exit 1
fi

IN="$1"
if [[ ! -f "$IN" ]]; then
  echo "Input is not a regular file: $IN" >&2
  exit 1
fi
OUTDIR_PARENT="${2%/}"
LOGDIR_PARENT="${3%/}"

if [[ -z "$BOTTOM_PAD_ROWS" ]]; then
  BOTTOM_PAD_ROWS="$BOTTOM_CROP_ROWS"
fi

case "$MODE" in
  transcode|preview) ;;
  *) echo "Unknown --mode: $MODE" >&2; exit 1 ;;
esac

case "$VIDEO_CODEC" in
  h264|hevc) ;;
  *) echo "Unknown --codec: $VIDEO_CODEC" >&2; exit 1 ;;
esac

case "$DEINT_MODE" in
  send_frame|send_field) ;;
  *) echo "Unknown --deint-mode: $DEINT_MODE" >&2; exit 1 ;;
esac

case "$LOG_LEVEL" in
  quiet|error|warning|info) ;;
  *) echo "Unknown --log-level: $LOG_LEVEL" >&2; exit 1 ;;
esac

IN_DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"
STEM="${BASE%.*}"

case "$IN_DIR" in
  *"/Originals"/*)
    REL_DIR="${IN_DIR#*"/Originals"/}"
    ;;
  *)
    echo "Input path must be inside a dir under Originals/: $IN" >&2
    exit 1
    ;;
esac

OUT_DIR="${OUTDIR_PARENT}${REL_DIR:+/$REL_DIR}"
mkdir -p "$OUT_DIR"
LOG_DIR="${LOGDIR_PARENT}${REL_DIR:+/$REL_DIR}"
mkdir -p "$LOG_DIR"

# Build filename prefix from the path under Originals/
# Example:
#   REL_DIR="Set 2/6 破"
#   PATH_PREFIX="Set_2_6_破"
PATH_PREFIX="${REL_DIR#/}"
PATH_PREFIX="${PATH_PREFIX//\//_}"
PATH_PREFIX="${PATH_PREFIX// /_}"

# Collapse repeated underscores just in case
while [[ "$PATH_PREFIX" == *"__"* ]]; do
  PATH_PREFIX="${PATH_PREFIX//__/_}"
done

case "$VIDEO_CODEC" in
  h264) SUFFIX="access_h264" ;;
  hevc) SUFFIX="access_hevc" ;;
  *) echo "Unknown --codec: $VIDEO_CODEC" >&2; exit 1 ;;
esac

if [[ -n "$PATH_PREFIX" ]]; then
  OUT="${OUT_DIR}/${PATH_PREFIX}_${STEM}_${SUFFIX}${OUTPUT_SUFFIX}.mp4"
else
  OUT="${OUT_DIR}/${STEM}_${SUFFIX}${OUTPUT_SUFFIX}.mp4"
fi

CMD_TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
LOG_FN="${LOG_DIR}/${STEM}_access_${CMD_TIMESTAMP}.log"
CMD_LOG_FN="${LOG_DIR}/${STEM}_transcode_cmd_${CMD_TIMESTAMP}.log"

declare -a INPUT_ARGS
declare -a COMMON_ARGS
declare -a OUTPUT_ARGS
declare -a VF_PARTS

add_input_arg() { INPUT_ARGS+=("$@"); }
add_common_arg() { COMMON_ARGS+=("$@"); }
add_output_arg() { OUTPUT_ARGS+=("$@"); }
add_filter() { VF_PARTS+=("$1"); }
wait_for_enter() {
  if [[ "$ASSUME_YES" == true ]]; then
    return 0
  fi
  printf 'Press Enter to start transcode, or Ctrl-C to cancel...'
  IFS= read -r _
}

get_hqdn3d_args() {
  case "$DENOISE_PRESET" in
    off) echo "" ;;
    verylight) echo "1.5:1.125:2.25:1.6875" ;;
    light) echo "2:1.5:3:2" ;;
    medium) echo "3:2.25:4.5:3.375" ;;
    strong) echo "4:3:6:4.5" ;;
    *) echo "Unknown --denoise preset: $DENOISE_PRESET" >&2; exit 1 ;;
  esac
}

HQDN3D_ARGS="$(get_hqdn3d_args)"

# ffmpeg global / input args
add_input_arg -hide_banner
add_input_arg -loglevel "$LOG_LEVEL"

# progress logging
add_input_arg -stats
add_input_arg -stats_period 1

if [[ -n "$START" ]]; then
  add_input_arg -ss "$START"
fi
if [[ -n "$END" ]]; then
  add_input_arg -to "$END"
fi
add_input_arg -i "$IN"

# Filter chain
add_filter "bwdif=mode=${DEINT_MODE}:parity=auto:deint=all"
add_filter "crop=iw:ih-${BOTTOM_CROP_ROWS}:0:0"
add_filter "pad=iw:ih+${BOTTOM_PAD_ROWS}:0:0:black"

if [[ -n "$HQDN3D_ARGS" ]]; then
  add_filter "hqdn3d=${HQDN3D_ARGS}"
fi

add_filter "scale='trunc(ih*dar/2)*2:ih'"
add_filter "setsar=1"

VF="$(IFS=,; echo "${VF_PARTS[*]}")"
add_common_arg -vf "$VF"

# Stream mapping
add_common_arg -map 0:v:0
if [[ "$MAP_BOTH_AUDIO" == true ]]; then
  add_common_arg -map 0:a:0?
  add_common_arg -map 0:a:1?
else
  add_common_arg -map 0:a:0?
fi

# Codec settings: used by BOTH preview and transcode
case "$VIDEO_CODEC" in
  h264)
    add_common_arg -c:v h264_videotoolbox
    add_common_arg -profile:v high
    add_common_arg -coder cabac
    ;;
  hevc)
    add_common_arg -c:v hevc_videotoolbox
    add_common_arg -profile:v main
    add_common_arg -tag:v hvc1
    ;;
esac

add_common_arg -spatial_aq 1
add_common_arg -max_ref_frames 4
add_common_arg -q:v "$VIDEO_Q"

# set color space explicitly
add_filter "setparams=range=limited:color_primaries=smpte170m:color_trc=smpte170m:colorspace=smpte170m"
add_common_arg -color_range tv
add_common_arg -color_primaries smpte170m
add_common_arg -color_trc smpte170m
add_common_arg -colorspace smpte170m

# Audio settings: same pipeline for preview and transcode
add_common_arg -c:a aac
add_common_arg -b:a "$AUDIO_BITRATE"

print_cmd() {
  printf 'ffmpeg'
  for arg in "${INPUT_ARGS[@]}" "${COMMON_ARGS[@]}" "${OUTPUT_ARGS[@]}"; do
    printf ' %q' "$arg"
  done
  printf '\n'
}

printf 'Mode: %s\n' "$MODE"
printf 'Input: %s\n' "$IN"
printf 'Output dir: %s\n' "$OUT_DIR"
if [[ -n "$START" || -n "$END" ]]; then
  printf 'Range: %s -> %s\n' "${START:-beginning}" "${END:-end}"
fi
printf 'Codec: %s\n' "$VIDEO_CODEC"
printf 'Denoise preset: %s\n' "$DENOISE_PRESET"
printf 'Bottom crop rows: %s\n' "$BOTTOM_CROP_ROWS"
printf 'Bottom pad rows: %s\n' "$BOTTOM_PAD_ROWS"
printf 'Deinterlace mode: %s\n' "$DEINT_MODE"

if [[ "$MODE" == "preview" ]]; then
  printf '\nPreview pipeline:\n\n'
  print_cmd
  printf '\n'

  exec ffmpeg \
    "${INPUT_ARGS[@]}" \
    "${COMMON_ARGS[@]}" \
    -movflags +faststart \
    -f matroska - 2> "$LOG_FN" | \
  ffplay -hide_banner -window_title "${STEM} preview" -
fi

add_output_arg -movflags +faststart
add_output_arg "$OUT"

printf 'Output: %s\n' "$OUT"
printf 'Log: %s\n\n' "$LOG_FN"
printf 'Running command:\n\n'
print_cmd
printf '\n'

wait_for_enter

{
  printf 'Timestamp: %s\n' "$(date +"%Y-%m-%d %H:%M:%S")"
  printf 'Input: %s\n' "$IN"
  printf 'Output: %s\n' "$OUT"
  printf 'Mode: %s\n' "$MODE"
  printf 'Codec: %s\n' "$VIDEO_CODEC"
  printf 'Denoise: %s\n' "$DENOISE_PRESET"
  printf 'Crop bottom: %s\n' "$BOTTOM_CROP_ROWS"
  printf 'Pad bottom: %s\n' "$BOTTOM_PAD_ROWS"
  printf '\nCommand:\n'
  print_cmd
} > "$CMD_LOG_FN"

time ffmpeg "${INPUT_ARGS[@]}" "${COMMON_ARGS[@]}" "${OUTPUT_ARGS[@]}" \
  2> >(tee "$LOG_FN" >&2)

printf 'Done: %s\n' "$OUT"
