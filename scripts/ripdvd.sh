#!/usr/bin/env bash
set -uo pipefail

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  ripdvd.sh --list
  ripdvd.sh --device /dev/diskN --name LABEL --out DIR [--retries 3] [--yes] [--no-eject] [--no-direct]

Examples:
  ./ripdvd.sh --list
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips"
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips" --retries 10

Notes:
  Use the whole disk, e.g. /dev/disk4, not /dev/disk4s1.
  The script will create:
    LABEL.iso
    LABEL.map
    LABEL.ddrescue-output.txt
    LABEL.iso.sha256
EOF
}

find_first() {
  local cmd
  for cmd in "$@"; do
    if command -v "$cmd" >/dev/null 2>&1; then
      command -v "$cmd"
      return 0
    fi
  done
  return 1
}

DEVICE=""
LABEL=""
OUT_DIR="$PWD"
RETRIES=3
ASSUME_YES=0
EJECT=1
DIRECT=1
LIST_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      LIST_ONLY=1
      shift
      ;;
    --device)
      [[ $# -ge 2 ]] || die "--device requires a value"
      DEVICE="$2"
      shift 2
      ;;
    --name)
      [[ $# -ge 2 ]] || die "--name requires a value"
      LABEL="$2"
      shift 2
      ;;
    --out)
      [[ $# -ge 2 ]] || die "--out requires a value"
      OUT_DIR="$2"
      shift 2
      ;;
    --retries)
      [[ $# -ge 2 ]] || die "--retries requires a value"
      RETRIES="$2"
      shift 2
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --no-eject)
      EJECT=0
      shift
      ;;
    --no-direct)
      DIRECT=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

command -v diskutil >/dev/null 2>&1 || die "diskutil not found; this script is for macOS"

if (( LIST_ONLY )); then
  diskutil list
  exit 0
fi

DDRESCUE="$(find_first ddrescue gddrescue)" || die "ddrescue not found. Install with: brew install ddrescue"
DDRESCUELOG="$(find_first ddrescuelog gddrescuelog)" || die "ddrescuelog not found. Install with: brew install ddrescue"
command -v shasum >/dev/null 2>&1 || die "shasum not found"

[[ -n "$DEVICE" ]] || die "--device is required"
[[ -n "$LABEL" ]] || die "--name is required"
[[ "$LABEL" != */* ]] || die "--name must not contain /"
[[ "$RETRIES" =~ ^-?[0-9]+$ ]] || die "--retries must be an integer, e.g. 3, 10, or -1"

base="$(basename "$DEVICE")"

case "$base" in
  disk[0-9]*)
    WHOLE="/dev/$base"
    RAW="/dev/r$base"
    ;;
  rdisk[0-9]*)
    RAW="/dev/$base"
    WHOLE="/dev/${base#r}"
    ;;
  *)
    die "Device must look like /dev/diskN or /dev/rdiskN, not a partition like /dev/diskNs1"
    ;;
esac

mkdir -p "$OUT_DIR" || die "Could not create output directory: $OUT_DIR"

ISO="$OUT_DIR/$LABEL.iso"
MAP="$OUT_DIR/$LABEL.map"
RUNLOG="$OUT_DIR/$LABEL.ddrescue-output.txt"
SHA="$OUT_DIR/$LABEL.iso.sha256"

if [[ -e "$ISO" && ! -e "$MAP" ]]; then
  die "ISO exists but mapfile does not: $ISO. Choose a different --name or move the old file."
fi

if [[ ! -e "$ISO" && -e "$MAP" ]]; then
  die "Mapfile exists but ISO does not: $MAP. Choose a different --name or move the old mapfile."
fi

if [[ -e "$ISO" && -e "$MAP" ]]; then
  echo "Existing ISO and mapfile found. This run will resume/fill gaps:"
  echo "  $ISO"
  echo "  $MAP"
fi

echo
echo "=== Source device ==="
diskutil info "$WHOLE" || die "Could not inspect $WHOLE"

echo
echo "=== Planned output ==="
echo "Raw source: $RAW"
echo "ISO:        $ISO"
echo "Mapfile:    $MAP"
echo "Run log:    $RUNLOG"
echo "SHA-256:    $SHA"
echo "Retries:    $RETRIES"

if (( ! ASSUME_YES )); then
  echo
  read -r -p "Type RIP to continue: " confirm
  [[ "$confirm" == "RIP" ]] || die "Aborted"
fi

{
  echo
  echo "=== ripdvd run: $(date) ==="
  echo "WHOLE=$WHOLE"
  echo "RAW=$RAW"
  echo "ISO=$ISO"
  echo "MAP=$MAP"
  echo "RETRIES=$RETRIES"
} >> "$RUNLOG"

run_logged() {
  echo | tee -a "$RUNLOG"
  printf '>>> ' | tee -a "$RUNLOG"
  printf '%q ' "$@" | tee -a "$RUNLOG"
  echo | tee -a "$RUNLOG"

  "$@" 2>&1 | tee -a "$RUNLOG"
  local status=${PIPESTATUS[0]}

  echo ">>> exit status: $status" | tee -a "$RUNLOG"
  return "$status"
}

print_status() {
  {
    echo
    echo "=== ddrescuelog status ==="
    "$DDRESCUELOG" -t "$MAP"
  } 2>&1 | tee -a "$RUNLOG"
}

is_done() {
  "$DDRESCUELOG" -D "$MAP" >/dev/null 2>&1
}

SUDO=()
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  SUDO=(sudo)
fi

echo
echo "Unmounting $WHOLE..."
diskutil unmountDisk "$WHOLE" || true

echo
echo "=== Fast pass: copy easy sectors first ==="
run_logged "${SUDO[@]}" "$DDRESCUE" -n -b2048 "$RAW" "$ISO" "$MAP" || true
print_status

COMPLETE=0

if is_done; then
  COMPLETE=1
  echo
  echo "Fast pass completed the image. No retry pass needed."
else
  echo
  echo "Mapfile is not complete. Running retry pass..."

  retry_args=()
  if (( DIRECT )); then
    retry_args+=("-d")
  fi
  retry_args+=("-r${RETRIES}" "-b2048" "$RAW" "$ISO" "$MAP")

  run_logged "${SUDO[@]}" "$DDRESCUE" "${retry_args[@]}" || true
  print_status

  if is_done; then
    COMPLETE=1
  fi
fi

echo
echo "Writing SHA-256..."
if SHALINE="$(shasum -a 256 "$ISO")"; then
  echo "$SHALINE" | tee "$SHA" | tee -a "$RUNLOG" >/dev/null
  echo "$SHALINE"
else
  die "Failed to compute SHA-256"
fi

if (( EJECT )); then
  echo
  echo "Ejecting $WHOLE..."
  diskutil eject "$WHOLE" || true
fi

echo
if (( COMPLETE )); then
  echo "DONE: complete image rescued."
  echo "ISO: $ISO"
  echo "Map: $MAP"
  echo "SHA: $SHA"
  exit 0
else
  echo "WARNING: image is incomplete. Keep the ISO and mapfile; you can retry later."
  echo "Retry later with the same --name and --out to continue from the mapfile."
  echo "ISO: $ISO"
  echo "Map: $MAP"
  exit 2
fi
