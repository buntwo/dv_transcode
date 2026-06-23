#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  transcode_parallel.sh TASK_FILE

Runs one shell command per non-empty, non-comment TASK_FILE line.

Live controls:
  echo 1 > vhs_transcode_parallelism   # keep one active task
  echo 3 > vhs_transcode_parallelism   # resume/start up to three active tasks
  echo 0 > vhs_transcode_parallelism   # pause all active tasks

Environment:
  TRANSCODE_DEFAULT_JOBS   initial active task target, default 1
  TRANSCODE_MAX_JOBS       clamp target to this value, default 3
  TRANSCODE_POLL_SECONDS   scheduler poll interval, default 2
  TRANSCODE_THROUGHPUT_SECONDS
                           throughput print interval, default 5; 0 disables
  TRANSCODE_REDRAW_STATS   auto, 1, or 0; default auto
  TRANSCODE_CONTROL_FILE   override control file path; default ./vhs_transcode_parallelism
  TRANSCODE_RUN_DIR        override run/log directory; default ./transcode_parallel_runs
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 2
fi

task_file=$1
if [[ ! -f "$task_file" ]]; then
  echo "Task file not found: $task_file" >&2
  exit 2
fi

default_jobs=${TRANSCODE_DEFAULT_JOBS:-1}
max_jobs=${TRANSCODE_MAX_JOBS:-3}
poll_seconds=${TRANSCODE_POLL_SECONDS:-2}
throughput_seconds=${TRANSCODE_THROUGHPUT_SECONDS:-5}
redraw_stats_setting=${TRANSCODE_REDRAW_STATS:-auto}
control_file=${TRANSCODE_CONTROL_FILE:-"$PWD/vhs_transcode_parallelism"}
run_root=${TRANSCODE_RUN_DIR:-"$PWD/transcode_parallel_runs"}
run_id=$(date +"%Y%m%d_%H%M%S")
run_dir="$run_root/$run_id"
log_dir="$run_dir/logs"
status_dir="$run_dir/status"
throughput_state_dir="$run_dir/throughput_state"

mkdir -p "$log_dir" "$status_dir" "$throughput_state_dir"

if [[ ! "$default_jobs" =~ ^[0-9]+$ || ! "$max_jobs" =~ ^[0-9]+$ ]]; then
  echo "TRANSCODE_DEFAULT_JOBS and TRANSCODE_MAX_JOBS must be non-negative integers" >&2
  exit 2
fi

if [[ ! "$throughput_seconds" =~ ^[0-9]+$ ]]; then
  echo "TRANSCODE_THROUGHPUT_SECONDS must be a non-negative integer" >&2
  exit 2
fi

case "$redraw_stats_setting" in
  auto|0|1) ;;
  *)
    echo "TRANSCODE_REDRAW_STATS must be auto, 1, or 0" >&2
    exit 2
    ;;
esac

if (( max_jobs < 1 )); then
  echo "TRANSCODE_MAX_JOBS must be at least 1" >&2
  exit 2
fi

if (( default_jobs > max_jobs )); then
  default_jobs=$max_jobs
fi

printf '%s\n' "$default_jobs" > "$control_file"

commands=()
while IFS= read -r line || [[ -n "$line" ]]; do
  trimmed=${line#"${line%%[![:space:]]*}"}
  trimmed=${trimmed%"${trimmed##*[![:space:]]}"}
  [[ -z "$trimmed" ]] && continue
  [[ "$trimmed" == \#* ]] && continue
  commands+=("$line")
done < "$task_file"

total=${#commands[@]}
if (( total == 0 )); then
  echo "No tasks found in $task_file" >&2
  exit 2
fi

pids=()
states=()
task_numbers=()
task_commands=()
log_files=()
status_files=()
next_task=0
completed=0
failed=0
shutting_down=0
last_throughput_report=0
stats_lines_printed=0
stats_redraw=0

if [[ "$redraw_stats_setting" == "1" || ( "$redraw_stats_setting" == "auto" && -t 1 ) ]]; then
  stats_redraw=1
fi

now() {
  date +"%Y-%m-%d %H:%M:%S"
}

clear_stats_block() {
  if (( stats_redraw == 1 && stats_lines_printed > 0 )); then
    printf '\033[%dA\033[J' "$stats_lines_printed"
    stats_lines_printed=0
  fi
}

log_line() {
  clear_stats_block
  printf '%s\n' "$*"
}

log_error() {
  clear_stats_block
  printf '%s\n' "$*" >&2
}

print_stats_block() {
  local stats_text=$1
  local stats_line
  local line_count=0

  clear_stats_block
  while IFS= read -r stats_line; do
    printf '[%s] %s\n' "$(now)" "$stats_line"
    line_count=$((line_count + 1))
  done <<< "$stats_text"

  if (( stats_redraw == 1 )); then
    stats_lines_printed=$line_count
  fi
}

read_target() {
  local target
  if [[ ! -f "$control_file" ]]; then
    printf '%s\n' "$default_jobs" > "$control_file"
  fi
  target=$(tr -d '[:space:]' < "$control_file" || true)
  if [[ ! "$target" =~ ^[0-9]+$ ]]; then
    echo "Invalid parallelism '$target' in $control_file; keeping 1 active task" >&2
    target=1
    printf '%s\n' "$target" > "$control_file"
  fi
  if (( target > max_jobs )); then
    target=$max_jobs
    printf '%s\n' "$target" > "$control_file"
  fi
  printf '%s\n' "$target"
}

children_of() {
  local parent=$1
  local child
  command -v pgrep >/dev/null 2>&1 || return 0
  pgrep -P "$parent" 2>/dev/null | while IFS= read -r child; do
    [[ -z "$child" ]] && continue
    children_of "$child"
    printf '%s\n' "$child"
  done
}

signal_task() {
  local signal=$1
  local pid=$2

  # Tasks are launched in their own process group, with pgid == pid. Signal the
  # group first so uv/python/ffmpeg descendants pause together; fall back to the
  # wrapper pid for very early startup or already-exiting tasks.
  kill "-$signal" "-$pid" 2>/dev/null || kill "-$signal" "$pid" 2>/dev/null || true
}

count_state() {
  local wanted=$1
  local count=0
  local i
  for i in "${!states[@]}"; do
    [[ "${states[$i]}" == "$wanted" ]] && ((count += 1))
  done
  printf '%s\n' "$count"
}

running_count() {
  count_state running
}

paused_count() {
  count_state paused
}

active_count() {
  local count=0
  local i
  for i in "${!states[@]}"; do
    if [[ "${states[$i]}" == "running" || "${states[$i]}" == "paused" ]]; then
      ((count += 1))
    fi
  done
  printf '%s\n' "$count"
}

start_task() {
  local task_index=$1
  local task_number=$((task_index + 1))
  local command_text=${commands[$task_index]}
  local status_file="$status_dir/task_${task_number}.status"
  local log_file="$log_dir/task_${task_number}.log"
  local pid

  {
    printf '[%s] task %03d start\n' "$(now)" "$task_number"
    printf 'command: %s\n\n' "$command_text"
  } > "$log_file"

  python3 -c '
import os
import subprocess
import sys
import time

command_text, log_file, status_file, task_number = sys.argv[1:]

try:
    os.setpgrp()
except OSError as exc:
    with open(log_file, "a", buffering=1) as log:
        log.write(f"[worker] warning: os.setpgrp failed: {exc}\n")

rc = 0
with open(log_file, "a", buffering=1) as log:
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", command_text],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        rc = proc.returncode
    except BaseException as exc:
        rc = 125
        log.write(f"[worker] exception: {exc}\n")
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log.write(f"\n[{timestamp}] task {int(task_number):03d} exit {rc}\n")

with open(status_file, "w") as status:
    status.write(f"{rc}\n")
sys.exit(rc)
' "$command_text" "$log_file" "$status_file" "$task_number" &
  pid=$!

  pids+=("$pid")
  states+=("running")
  task_numbers+=("$task_number")
  task_commands+=("$command_text")
  log_files+=("$log_file")
  status_files+=("$status_file")

  log_line "[$(now)] started task $task_number/$total pid=$pid log=$log_file"
}

reap_finished() {
  local i rc
  for i in "${!states[@]}"; do
    [[ "${states[$i]}" == "done" || "${states[$i]}" == "failed" ]] && continue
    [[ ! -f "${status_files[$i]}" ]] && continue

    rc=$(cat "${status_files[$i]}")
    wait "${pids[$i]}" 2>/dev/null || true
    completed=$((completed + 1))
    if [[ "$rc" == "0" ]]; then
      states[$i]=done
      log_line "[$(now)] finished task ${task_numbers[$i]}/$total"
    else
      states[$i]=failed
      failed=$((failed + 1))
      log_error "[$(now)] FAILED task ${task_numbers[$i]}/$total rc=$rc log=${log_files[$i]}"
    fi
  done
}

pause_to_target() {
  local target=$1
  local i
  while (( $(running_count) > target )); do
    for ((i=${#states[@]} - 1; i >= 0; i--)); do
      if [[ "${states[$i]}" == "running" ]]; then
        signal_task STOP "${pids[$i]}"
        states[$i]=paused
        log_line "[$(now)] paused task ${task_numbers[$i]} pid=${pids[$i]}"
        break
      fi
    done
  done
}

resume_to_target() {
  local target=$1
  local i
  while (( $(running_count) < target && $(paused_count) > 0 )); do
    for i in "${!states[@]}"; do
      if [[ "${states[$i]}" == "paused" ]]; then
        signal_task CONT "${pids[$i]}"
        states[$i]=running
        log_line "[$(now)] resumed task ${task_numbers[$i]} pid=${pids[$i]}"
        break
      fi
    done
  done
}

launch_to_target() {
  local target=$1
  while (( $(running_count) < target && next_task < total )); do
    start_task "$next_task"
    next_task=$((next_task + 1))
  done
}

report_throughput() {
  local current_time=$1
  local running_logs=()
  local running_labels=()
  local parsed
  local i
  throughput_line=""

  if (( throughput_seconds == 0 )); then
    return 0
  fi
  if (( current_time - last_throughput_report < throughput_seconds )); then
    return 0
  fi

  for i in "${!states[@]}"; do
    if [[ "${states[$i]}" == "running" ]]; then
      running_logs+=("${log_files[$i]}")
      running_labels+=("${task_numbers[$i]}")
    fi
  done
  if ((${#running_logs[@]} == 0)); then
    return 0
  fi

  parsed=$(python3 - "$current_time" "$throughput_state_dir" "${#running_logs[@]}" "${running_labels[@]}" "${running_logs[@]}" <<'PY'
import os
import re
import math
import subprocess
import sys

current_time = float(sys.argv[1])
state_dir = sys.argv[2]
n = int(sys.argv[3])
labels = sys.argv[4 : 4 + n]
paths = sys.argv[4 + n :]
speed_re = re.compile(rb"speed=\s*([0-9.]+)x")
fps_re = re.compile(rb"fps=\s*([0-9.]+)")
frame_re = re.compile(rb"frame=\s*([0-9]+)")
time_re = re.compile(rb"time=\s*([0-9:.]+)")
bitrate_re = re.compile(rb"bitrate=\s*([^ \r\n]+)")
input_re = re.compile(rb"^Input:\s*(.+)$", re.MULTILINE)


def parse_time_seconds(value):
    if value is None:
        return None
    try:
        parts = value.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(value)
    except ValueError:
        return None


def format_eta(seconds):
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def read_duration_seconds(label, log_head):
    input_matches = input_re.findall(log_head)
    if not input_matches:
        return None

    input_path = input_matches[-1].decode(errors="replace").strip()
    if not input_path or not os.path.isfile(input_path):
        return None

    cache_path = os.path.join(state_dir, f"task_{label}.duration")
    try:
        with open(cache_path, "r", encoding="utf-8") as cache_file:
            cached = cache_file.read().strip()
        if cached:
            duration = float(cached)
            return duration if math.isfinite(duration) and duration > 0 else None
        return None
    except OSError:
        pass
    except ValueError:
        pass

    duration = None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                input_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        duration = float(proc.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            duration = None
    except (OSError, subprocess.SubprocessError, ValueError):
        duration = None

    tmp_cache_path = f"{cache_path}.tmp"
    with open(tmp_cache_path, "w", encoding="utf-8") as cache_file:
        cache_file.write("" if duration is None else f"{duration}\n")
    os.replace(tmp_cache_path, cache_path)
    return duration

metrics = []
task_rows = []
inst_fps_values = []
inst_speed_values = []
for label, path in zip(labels, paths):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            log_head = handle.read(min(size, 65536))
            handle.seek(max(0, size - 65536))
            log_tail = handle.read()
            data = log_head + b"\n" + log_tail
    except OSError:
        continue

    speed_matches = speed_re.findall(data)
    fps_matches = fps_re.findall(data)
    if not speed_matches and not fps_matches:
        continue

    speed = float(speed_matches[-1]) if speed_matches else None
    fps = float(fps_matches[-1]) if fps_matches else None
    frame_matches = frame_re.findall(data)
    time_matches = time_re.findall(data)
    bitrate_matches = bitrate_re.findall(data)
    time_value = time_matches[-1].decode() if time_matches else None
    media_seconds = parse_time_seconds(time_value)
    duration_seconds = read_duration_seconds(label, log_head)
    metrics.append((speed, fps))
    frame = int(frame_matches[-1]) if frame_matches else None
    inst_fps = None
    inst_speed = None

    if frame is not None:
        state_path = os.path.join(state_dir, f"task_{label}.tsv")
        try:
            with open(state_path, "r", encoding="utf-8") as state_file:
                previous = state_file.read().strip().split("\t")
            if len(previous) >= 2:
                previous_time = float(previous[0])
                previous_frame = int(previous[1])
                dt = current_time - previous_time
                df = frame - previous_frame
                if dt > 0 and df >= 0:
                    inst_fps = df / dt
                if len(previous) >= 3 and media_seconds is not None:
                    previous_media_seconds = float(previous[2])
                    dmedia = media_seconds - previous_media_seconds
                    if dt > 0 and dmedia >= 0:
                        inst_speed = dmedia / dt
        except OSError:
            pass
        except ValueError:
            pass

        tmp_state_path = f"{state_path}.tmp"
        with open(tmp_state_path, "w", encoding="utf-8") as state_file:
            state_file.write(
                f"{current_time}\t{frame}\t"
                f"{'' if media_seconds is None else media_seconds}\n"
            )
        os.replace(tmp_state_path, state_path)

    if inst_fps is not None:
        inst_fps_values.append(inst_fps)
    if inst_speed is not None:
        inst_speed_values.append(inst_speed)

    progress = None
    eta = None
    if duration_seconds is not None and media_seconds is not None:
        progress = max(0.0, min(999.9, media_seconds / duration_seconds * 100.0))
        if inst_speed is not None and inst_speed > 0:
            eta = format_eta((duration_seconds - media_seconds) / inst_speed)

    task_rows.append(
        {
            "label": f"task {label}",
            "i_fps": inst_fps,
            "i_speed": inst_speed,
            "progress": progress,
            "eta": eta,
            "frame": frame,
            "fps": fps,
            "time": time_value,
            "bitrate": bitrate_matches[-1].decode(errors="replace") if bitrate_matches else None,
            "speed": speed,
        }
    )

if not metrics:
    sys.exit(0)

speeds = [speed for speed, _ in metrics if speed is not None]
fps_values = [fps for _, fps in metrics if fps is not None]

rows = [
    {
        "label": "overall",
        "i_fps": sum(inst_fps_values) if inst_fps_values else None,
        "i_speed": sum(inst_speed_values) if inst_speed_values else None,
        "progress": None,
        "eta": None,
        "frame": None,
        "fps": sum(fps_values) if fps_values else None,
        "time": None,
        "bitrate": None,
        "speed": sum(speeds) if speeds else None,
        "running": n,
    }
]
rows.extend(task_rows)

label_width = max(len(row["label"]) for row in rows)
frame_width = max(
    len(str(row["frame"])) if row["frame"] is not None else 1
    for row in rows
)


def field(key, value, width, *, blank_key=False, align=">"):
    field_width = len(key) + 1 + width
    if value is None:
        return " " * field_width if blank_key else f"{key}={'n/a':>{width}}"
    if align == "<":
        return f"{key}={value:<{width}}"
    return f"{key}={value:>{width}}"


def numeric(value, decimals=1):
    if value is None:
        return None
    return f"{value:.{decimals}f}"


for row in rows:
    is_overall = "running" in row
    line = (
        f"{row['label']:<{label_width}} "
        f"{field('i_fps', numeric(row['i_fps']), 5)} "
        f"{field('i_speed', None if row['i_speed'] is None else f'{row['i_speed']:.2f}x', 6)} "
        f"{field('progress', None if row['progress'] is None else f'{row['progress']:.1f}%', 6, blank_key=is_overall)} "
        f"{field('eta', row['eta'], 8, blank_key=is_overall)} "
        f"{field('frame', None if row['frame'] is None else str(row['frame']), frame_width, blank_key=is_overall)} "
        f"{field('fps', numeric(row['fps']), 5)} "
        f"{field('speed', None if row['speed'] is None else f'{row['speed']:.2f}x', 6)}"
    )
    if is_overall:
        line += f" running={row['running']}"
    else:
        line += (
            f" {field('time', row['time'], 11, align='<')} "
            f"{field('bitrate', row['bitrate'], 14, align='<')}"
        )
    print(line)
PY
)
  throughput_line=$parsed
  if [[ -n "$throughput_line" ]]; then
    last_throughput_report=$current_time
  fi
  return 0
}

cleanup() {
  local i
  if (( shutting_down == 1 )); then
    return
  fi
  shutting_down=1
  log_error "[$(now)] stopping active tasks"
  for i in "${!states[@]}"; do
    if [[ "${states[$i]}" == "paused" ]]; then
      signal_task CONT "${pids[$i]}"
    fi
  done
  for i in "${!states[@]}"; do
    if [[ "${states[$i]}" == "running" || "${states[$i]}" == "paused" ]]; then
      signal_task TERM "${pids[$i]}"
    fi
  done
}

trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

log_line "Task file: $task_file"
log_line "Tasks: $total"
log_line "Control file: $control_file"
log_line "Run directory: $run_dir"
log_line "Set target active jobs with: echo N > '$control_file'"

while (( completed < total )); do
  loop_time=$(date +%s)
  target=$(read_target)
  reap_finished
  pause_to_target "$target"
  resume_to_target "$target"
  launch_to_target "$target"
  report_throughput "$loop_time"
  if [[ -n "$throughput_line" ]]; then
    print_stats_block "$throughput_line"
  fi
  sleep "$poll_seconds"
done

reap_finished
clear_stats_block

log_line "[$(now)] complete: $completed/$total tasks, failures=$failed"
log_line "Logs: $log_dir"

if (( failed > 0 )); then
  exit 1
fi
