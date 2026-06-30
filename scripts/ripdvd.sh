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
  ripdvd.sh --device /dev/diskN --name LABEL --out DIR [--retries 3] [--yes] [--no-eject] [--no-direct] [--raw-read] [--auto-slice] [--size-from-diskutil]

Examples:
  ./ripdvd.sh --list
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips"
  ./ripdvd.sh --device /dev/rdisk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips"
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips" --raw-read
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips" --auto-slice --raw-read
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips" --retries 10
  ./ripdvd.sh --device /dev/disk4 --name "2001-08_trip_disc01" --out "$HOME/DVD_Rips" --size-from-diskutil

Notes:
  The script reads from the exact --device path by default. Pass /dev/rdiskN
  yourself if you want the raw character device.
  Pass --raw-read to convert the selected read source to /dev/rdisk*.
  Pass --auto-slice to read a mounted optical filesystem slice such as
  /dev/disk4s0 instead of the whole disk node when --device is /dev/disk4.
  A fresh fast pass follows ddrescue's optical-media examples and does not use -d.
  A resumed fast pass and the retry pass use -d by default; pass --no-direct
  to disable that. If -d is unavailable, the script retries without it.
  Pass --size-from-diskutil to bound ddrescue to diskutil's Disk Size when
  the drive reports an implausible end position.
  The script will create:
    LABEL.iso
    LABEL.map
    LABEL.ddrescue-output.txt
    LABEL.iso.sha256 (only after a complete rescue)
  ddrescue may create LABEL.map.bak during a run. This script removes it
  only after the current mapfile is complete.
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

diskutil_size_bytes_from_plist() {
  local key plist size

  plist="$(cat)"
  for key in TotalSize MediaSize Size; do
    size="$(plutil -extract "$key" raw -o - - <<< "$plist" 2>/dev/null || true)"
    if [[ "$size" =~ ^[1-9][0-9]*$ ]]; then
      printf '%s\n' "$size"
      return 0
    fi
  done

  return 1
}

diskutil_size_bytes_from_info() {
  local line size

  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*(Disk|Total)[[:space:]]+Size: ]] &&
       [[ "$line" =~ \(([0-9][0-9,]*)[[:space:]]+Bytes\) ]]; then
      size="${BASH_REMATCH[1]//,/}"
      [[ "$size" =~ ^[1-9][0-9]*$ ]] || return 1
      printf '%s\n' "$size"
      return 0
    fi
  done

  return 1
}

plist_raw_value() {
  local key="$1"

  plutil -extract "$key" raw -o - - 2>/dev/null
}

volume_name_from_device() {
  local dev="$1"
  local info name

  info="$(diskutil info -plist "$dev" 2>/dev/null)" || return 1
  name="$(plist_raw_value VolumeName <<< "$info" || true)"

  if [[ -n "$name" && "$name" != "(null)" ]]; then
    printf '%s\n' "$name"
    return 0
  fi

  return 1
}

recorded_names_from_diskutil() {
  local disk_base="$1"
  local list_plist count i ident line mount_dev dev name
  local seen=$'\n'
  local candidates=$'\n'

  list_plist="$(diskutil list -plist "$WHOLE" 2>/dev/null || true)"

  candidates+="$WHOLE"$'\n'

  while IFS= read -r line; do
    if [[ "$line" =~ ^(/dev/${disk_base}s[0-9]+)[[:space:]]+on[[:space:]]+.+[[:space:]]+\( ]]; then
      mount_dev="${BASH_REMATCH[1]}"
      candidates+="$mount_dev"$'\n'
    fi
  done < <(mount)

  if [[ -n "$list_plist" ]]; then
    count="$(plist_raw_value AllDisksAndPartitions.0.Partitions <<< "$list_plist" || true)"
    if [[ "$count" =~ ^[0-9]+$ ]]; then
      for (( i = 0; i < count; i++ )); do
        ident="$(plist_raw_value "AllDisksAndPartitions.0.Partitions.$i.DeviceIdentifier" <<< "$list_plist" || true)"
        [[ -n "$ident" ]] || continue
        candidates+="/dev/$ident"$'\n'
      done
    fi
  fi

  for dev in /dev/"$disk_base"s[0-9]*; do
    [[ -e "$dev" ]] || continue
    candidates+="$dev"$'\n'
  done

  while IFS= read -r dev; do
    [[ -n "$dev" ]] || continue
    [[ "$seen" != *$'\n'"$dev"$'\n'* ]] || continue
    seen+="$dev"$'\n'

    name="$(volume_name_from_device "$dev" || true)"
    if [[ -n "$name" && "$seen" != *$'\n'"$name"$'\n'* ]]; then
      printf '%s\n' "$name"
      seen+="$name"$'\n'
    fi
  done <<< "$candidates"
}

mounted_optical_slice_for_disk() {
  local disk_base="$1"
  local line dev fstype

  while IFS= read -r line; do
    if [[ "$line" =~ ^(/dev/${disk_base}s[0-9]+)[[:space:]]+on[[:space:]]+.+[[:space:]]+\(([^,]+) ]]; then
      dev="${BASH_REMATCH[1]}"
      fstype="${BASH_REMATCH[2]}"
      case "$fstype" in
        cd9660|udf)
          printf '%s\n' "$dev"
          return 0
          ;;
      esac
    fi
  done < <(mount)

  return 1
}

raw_device_for() {
  local dev="$1"
  local dev_base

  dev_base="$(basename "$dev")"
  if [[ "$dev_base" == r* ]]; then
    printf '/dev/%s\n' "$dev_base"
  else
    printf '/dev/r%s\n' "$dev_base"
  fi
}

validate_dvd_size_bytes() {
  local size="$1"
  local max_dvd_size=20000000000

  [[ "$size" =~ ^[1-9][0-9]*$ ]] || die "Parsed source size is not a positive byte count: $size"

  if (( size > max_dvd_size )); then
    die "Parsed source size is implausibly large for DVD media: $size bytes. Refusing to use it as a ddrescue size limit."
  fi
}

DEVICE=""
LABEL=""
OUT_DIR="$PWD"
RETRIES=3
ASSUME_YES=0
EJECT=1
DIRECT=1
RAW_READ=0
AUTO_SLICE=0
SIZE_FROM_DISKUTIL=0
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
    --raw-read)
      RAW_READ=1
      shift
      ;;
    --auto-slice)
      AUTO_SLICE=1
      shift
      ;;
    --size-from-diskutil)
      SIZE_FROM_DISKUTIL=1
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
[[ "$RETRIES" =~ ^(-1|0|[1-9][0-9]*)$ ]] || die "--retries must be -1 or a non-negative integer, e.g. 3, 10, or -1"

base="$(basename "$DEVICE")"
device_base="${base#r}"

if [[ "$device_base" =~ ^(disk[0-9]+)(s[0-9]+)*$ ]]; then
  whole_base="${BASH_REMATCH[1]}"
  WHOLE="/dev/$whole_base"
  REQUESTED_DEVICE="/dev/$base"
else
  die "Device must look like /dev/diskN, /dev/rdiskN, or a slice like /dev/diskNsM"
fi

mkdir -p "$OUT_DIR" || die "Could not create output directory: $OUT_DIR"

ISO="$OUT_DIR/$LABEL.iso"
MAP="$OUT_DIR/$LABEL.map"
MAP_BAK="$MAP.bak"
RUNLOG="$OUT_DIR/$LABEL.ddrescue-output.txt"
SHA="$OUT_DIR/$LABEL.iso.sha256"
RESUMING=0
SOURCE_INFO=""
READ_DEVICE=""
READ_INFO=""
READ_INFO_PLIST=""
RECORDED_NAMES=""
SOURCE_SIZE=""
SOURCE_SIZE_SOURCE=""
DDRESCUE_SIZE_ARGS=()
DDRESCUELOG_SIZE_ARGS=()

if [[ -e "$ISO" && ! -e "$MAP" ]]; then
  die "ISO exists but mapfile does not: $ISO. Choose a different --name or move the old file."
fi

if [[ ! -e "$ISO" && -e "$MAP" ]]; then
  die "Mapfile exists but ISO does not: $MAP. Choose a different --name or move the old mapfile."
fi

READ_DEVICE="$REQUESTED_DEVICE"
if (( AUTO_SLICE )) && [[ "$device_base" == "$whole_base" ]]; then
  READ_DEVICE="$(mounted_optical_slice_for_disk "$whole_base" || true)"
  if [[ -z "$READ_DEVICE" ]]; then
    READ_DEVICE="$REQUESTED_DEVICE"
  fi
fi
if (( RAW_READ )); then
  RAW="$(raw_device_for "$READ_DEVICE")"
else
  RAW="$READ_DEVICE"
fi

SOURCE_INFO="$(diskutil info "$WHOLE")" || die "Could not inspect $WHOLE"
READ_INFO="$(diskutil info "$READ_DEVICE")" || die "Could not inspect read source $READ_DEVICE"
READ_INFO_PLIST="$(diskutil info -plist "$READ_DEVICE" 2>/dev/null || true)"
RECORDED_NAMES="$(recorded_names_from_diskutil "$whole_base" || true)"

if (( SIZE_FROM_DISKUTIL )); then
  if [[ -n "$READ_INFO_PLIST" ]] &&
     SOURCE_SIZE="$(diskutil_size_bytes_from_plist <<< "$READ_INFO_PLIST")"; then
    SOURCE_SIZE_SOURCE="diskutil info -plist $READ_DEVICE"
  else
    SOURCE_SIZE="$(diskutil_size_bytes_from_info <<< "$READ_INFO")" || die "Could not parse Disk Size from diskutil info for $READ_DEVICE"
    SOURCE_SIZE_SOURCE="diskutil info $READ_DEVICE"
  fi

  validate_dvd_size_bytes "$SOURCE_SIZE"
  DDRESCUE_SIZE_ARGS=("-s" "$SOURCE_SIZE")
  DDRESCUELOG_SIZE_ARGS=("-s" "$SOURCE_SIZE")
fi

if [[ -e "$ISO" && -e "$MAP" ]] && "$DDRESCUELOG" -D "$MAP" >/dev/null 2>&1; then
  die "Existing ISO and mapfile already describe a complete image: $ISO. Choose a different --name or move the old files."
fi

if [[ -e "$ISO" && -e "$MAP" ]]; then
  RESUMING=1
  echo "Existing ISO and mapfile found. This run will resume/fill gaps:"
  echo "  $ISO"
  echo "  $MAP"
fi

echo
echo "=== Source device ==="
printf '%s\n' "$SOURCE_INFO"

echo
echo "=== Planned output ==="
echo "Read source: $RAW"
echo "Requested:   $REQUESTED_DEVICE"
echo "Read node:   $READ_DEVICE"
echo "ISO:        $ISO"
echo "Mapfile:    $MAP"
echo "Run log:    $RUNLOG"
echo "SHA-256:    $SHA"
echo "Retries:    $RETRIES"
if [[ -n "$RECORDED_NAMES" ]]; then
  echo "Disc name(s):"
  while IFS= read -r name; do
    echo "  $name"
  done <<< "$RECORDED_NAMES"
else
  echo "Disc name:   (not reported)"
fi
if [[ -n "$SOURCE_SIZE" ]]; then
  echo "Size limit: $SOURCE_SIZE bytes from $SOURCE_SIZE_SOURCE"
else
  echo "Size limit: none"
fi
if (( AUTO_SLICE )); then
  echo "Auto-slice: enabled"
else
  echo "Auto-slice: disabled"
fi
if (( RAW_READ )); then
  echo "Raw read:   enabled"
else
  echo "Raw read:   disabled"
fi
if (( DIRECT )); then
  if (( RESUMING )); then
    echo "Direct I/O: resumed fast pass and retry pass, with automatic fallback"
  else
    echo "Direct I/O: retry pass only, with automatic fallback"
  fi
else
  echo "Direct I/O: disabled"
fi

if (( ! ASSUME_YES )); then
  echo
  read -r -p "Type RIP to continue: " confirm
  [[ "$confirm" == "RIP" ]] || die "Aborted"
fi

{
  echo
  echo "=== ripdvd run: $(date) ==="
  echo "WHOLE=$WHOLE"
  echo "REQUESTED_DEVICE=$REQUESTED_DEVICE"
  echo "READ_DEVICE=$READ_DEVICE"
  echo "RAW=$RAW"
  echo "ISO=$ISO"
  echo "MAP=$MAP"
  echo "RETRIES=$RETRIES"
  echo "DIRECT=$DIRECT"
  echo "RAW_READ=$RAW_READ"
  echo "AUTO_SLICE=$AUTO_SLICE"
  echo "RESUMING=$RESUMING"
  echo "RECORDED_NAMES=$RECORDED_NAMES"
  echo "SIZE_FROM_DISKUTIL=$SIZE_FROM_DISKUTIL"
  echo "SOURCE_SIZE=$SOURCE_SIZE"
  echo "SOURCE_SIZE_SOURCE=$SOURCE_SIZE_SOURCE"
} >> "$RUNLOG"

LAST_RUN_OUTPUT=""

cleanup_last_run_output() {
  if [[ -n "$LAST_RUN_OUTPUT" && -e "$LAST_RUN_OUTPUT" ]]; then
    rm -f -- "$LAST_RUN_OUTPUT"
  fi
}

trap cleanup_last_run_output EXIT

run_logged() {
  local status

  cleanup_last_run_output
  LAST_RUN_OUTPUT="$(mktemp "${TMPDIR:-/tmp}/ripdvd-command-output.XXXXXX")" || die "Could not create temporary command log"

  echo | tee -a "$RUNLOG"
  printf '>>> ' | tee -a "$RUNLOG"
  printf '%q ' "$@" | tee -a "$RUNLOG"
  echo | tee -a "$RUNLOG"

  "$@" 2>&1 | tee -a "$RUNLOG" | tee "$LAST_RUN_OUTPUT"
  status=${PIPESTATUS[0]}

  echo ">>> exit status: $status" | tee -a "$RUNLOG"
  return "$status"
}

run_ddrescue() {
  local arg status has_direct=0
  local -a args=("$@")
  local -a fallback_args=()

  for arg in "${args[@]}"; do
    if [[ "$arg" == "-d" || "$arg" == "--idirect" ]]; then
      has_direct=1
    else
      fallback_args+=("$arg")
    fi
  done

  run_logged "${SUDO[@]}" "$DDRESCUE" "${args[@]}"
  status=$?

  if (( status != 0 && has_direct )) &&
     [[ -n "$LAST_RUN_OUTPUT" ]] &&
     grep -qi "Direct disc access not available" "$LAST_RUN_OUTPUT"; then
    {
      echo
      echo "WARNING: direct disc access is unavailable. Retrying this pass without -d."
      echo "WARNING: direct disc access will stay disabled for the rest of this run."
    } | tee -a "$RUNLOG"

    DIRECT=0
    run_logged "${SUDO[@]}" "$DDRESCUE" "${fallback_args[@]}"
    status=$?
  fi

  return "$status"
}

print_status() {
  {
    echo
    echo "=== ddrescuelog status ==="
    "$DDRESCUELOG" -t "${DDRESCUELOG_SIZE_ARGS[@]}" "$MAP"
  } 2>&1 | tee -a "$RUNLOG"
}

is_done() {
  "$DDRESCUELOG" -D "${DDRESCUELOG_SIZE_ARGS[@]}" "$MAP" >/dev/null 2>&1
}

file_size_bytes() {
  local size

  if size="$(stat -f %z "$1" 2>/dev/null)" && [[ "$size" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$size"
    return 0
  fi

  if size="$(stat -c %s "$1" 2>/dev/null)" && [[ "$size" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$size"
    return 0
  fi

  return 1
}

map_extent_bytes() {
  local line status_seen=0 pos size status rest end max=0

  while IFS= read -r line; do
    line="${line%%#*}"
    [[ "$line" =~ [^[:space:]] ]] || continue

    if (( ! status_seen )); then
      status_seen=1
      continue
    fi

    read -r pos size status rest <<< "$line"
    [[ -n "${pos:-}" && -n "${size:-}" && -n "${status:-}" && -z "${rest:-}" ]] || return 1
    [[ "$status" =~ ^[?*/+-]$ ]] || return 1

    pos="${pos//_/}"
    size="${size//_/}"
    [[ "$pos" =~ ^(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*|0)$ ]] || return 1
    [[ "$size" =~ ^(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*|0)$ ]] || return 1

    end=$((pos + size))
    if (( end > max )); then
      max=$end
    fi
  done < "$MAP"

  (( status_seen )) || return 1
  printf '%s\n' "$max"
}

verify_complete_iso() {
  local expected_size iso_size

  [[ -f "$ISO" ]] || die "ISO is missing or not a regular file: $ISO"

  iso_size="$(file_size_bytes "$ISO")" || die "Could not determine ISO size: $ISO"
  if [[ -n "$SOURCE_SIZE" ]]; then
    expected_size="$SOURCE_SIZE"
  else
    expected_size="$(map_extent_bytes)" || die "Could not determine mapfile extent: $MAP"
  fi

  if [[ -n "$SOURCE_SIZE" && "$iso_size" =~ ^[0-9]+$ && "$expected_size" =~ ^[0-9]+$ ]] &&
     (( iso_size > expected_size )); then
    command -v truncate >/dev/null 2>&1 || die "ISO is larger than diskutil size ($iso_size > $expected_size bytes), but truncate was not found: $ISO"
    {
      echo
      echo "Truncating ISO from $iso_size bytes to diskutil size $expected_size bytes: $ISO"
    } | tee -a "$RUNLOG"
    truncate -s "$expected_size" "$ISO" || die "Failed to truncate ISO to diskutil size: $ISO"
    iso_size="$(file_size_bytes "$ISO")" || die "Could not determine ISO size after truncating: $ISO"
  fi

  if [[ "$iso_size" != "$expected_size" ]]; then
    die "ISO size ($iso_size bytes) does not match expected complete size ($expected_size bytes): $ISO"
  fi
}

cleanup_map_backup() {
  [[ -e "$MAP_BAK" ]] || return 0

  if ! is_done; then
    {
      echo
      echo "Keeping mapfile backup because the current mapfile is not complete: $MAP_BAK"
    } | tee -a "$RUNLOG"
    return 0
  fi

  {
    echo
    echo "Deleting completed-run mapfile backup: $MAP_BAK"
  } | tee -a "$RUNLOG"

  if ! rm -f -- "$MAP_BAK"; then
    echo "WARNING: failed to delete mapfile backup: $MAP_BAK" | tee -a "$RUNLOG"
  fi
}

cleanup_incomplete_sha() { [[ -e "$SHA" ]] || return 0

  {
    echo
    echo "Deleting SHA-256 file because the image is incomplete: $SHA"
  } | tee -a "$RUNLOG"

  if ! rm -f -- "$SHA"; then
    echo "WARNING: failed to delete SHA-256 file for incomplete image: $SHA" | tee -a "$RUNLOG"
  fi
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
fast_args=("${DDRESCUE_SIZE_ARGS[@]}")
if (( DIRECT && RESUMING )); then
  fast_args+=("-d")
fi
fast_args+=("-n" "-b2048" "$RAW" "$ISO" "$MAP")

run_ddrescue "${fast_args[@]}" || true
print_status

COMPLETE=0

if is_done; then
  COMPLETE=1
  verify_complete_iso
  echo
  echo "Fast pass completed the image. No retry pass needed."
else
  echo
  echo "Mapfile is not complete. Running retry pass..."

  retry_args=("${DDRESCUE_SIZE_ARGS[@]}")
  if (( DIRECT )); then
    retry_args+=("-d")
  fi
  retry_args+=("-r${RETRIES}" "-b2048" "$RAW" "$ISO" "$MAP")

  run_ddrescue "${retry_args[@]}" || true
  print_status

  if is_done; then
    COMPLETE=1
    verify_complete_iso
  fi
fi

if (( COMPLETE )); then
  echo
  echo "Writing SHA-256..."
  if SHALINE="$(shasum -a 256 "$ISO")"; then
    echo "$SHALINE" | tee "$SHA" | tee -a "$RUNLOG" >/dev/null
    echo "$SHALINE"
  else
    die "Failed to compute SHA-256"
  fi
fi

if (( EJECT )); then
  echo
  echo "Ejecting $WHOLE..."
  diskutil eject "$WHOLE" || true
fi

echo
if (( COMPLETE )); then
  cleanup_map_backup
  echo "DONE: complete image rescued."
  echo "ISO: $ISO"
  echo "Map: $MAP"
  echo "SHA: $SHA"
  exit 0
else
  cleanup_incomplete_sha
  echo "WARNING: image is incomplete. Keep the ISO and mapfile; you can retry later."
  echo "Retry later with the same --name and --out to continue from the mapfile."
  echo "ISO: $ISO"
  echo "Map: $MAP"
  exit 2
fi
