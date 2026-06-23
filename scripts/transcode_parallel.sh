#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  transcode_parallel.sh [--run-dir RUN_DIR] TASK_FILE
  transcode_parallel.sh --resume RUN_DIR TASK_FILE

Runs one shell command per non-empty, non-comment TASK_FILE line.

Live controls:
  echo 1 > vhs_transcode_parallelism   # keep one active task
  echo 3 > vhs_transcode_parallelism   # resume/start up to three active tasks
  echo 0 > vhs_transcode_parallelism   # pause all active tasks

Environment:
  TRANSCODE_DEFAULT_JOBS   initial active task target, default 1
  TRANSCODE_MAX_JOBS       clamp target to this value, default 3
  TRANSCODE_POLL_SECONDS   scheduler poll interval, default 1
  TRANSCODE_THROUGHPUT_SECONDS
                           throughput print interval, default 5; 0 disables
  TRANSCODE_REDRAW_STATS   auto, 1, or 0; default auto
  TRANSCODE_CONTROL_FILE   override control file path; default ./vhs_transcode_parallelism
  TRANSCODE_RUN_DIR        exact run/log directory; default ./transcode_parallel_runs/YYYYMMDD_HHMMSS
EOF
}

run_dir_arg=${TRANSCODE_RUN_DIR:-}
resume_run_dir=
positional=()
while (($# > 0)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --run-dir)
      if [[ -z "${2:-}" ]]; then
        echo "--run-dir requires a directory path" >&2
        exit 2
      fi
      run_dir_arg=$2
      shift 2
      ;;
    --resume)
      if [[ -z "${2:-}" ]]; then
        echo "--resume requires an existing run directory" >&2
        exit 2
      fi
      resume_run_dir=$2
      shift 2
      ;;
    --)
      shift
      while (($# > 0)); do
        positional+=("$1")
        shift
      done
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      positional+=("$1")
      shift
      ;;
  esac
done

if ((${#positional[@]} != 1)); then
  usage >&2
  exit 2
fi

if [[ -n "$resume_run_dir" && -n "$run_dir_arg" ]]; then
  echo "--resume and --run-dir/TRANSCODE_RUN_DIR are mutually exclusive" >&2
  exit 2
fi

task_file=${positional[0]}
if [[ ! -f "$task_file" ]]; then
  echo "Task file not found: $task_file" >&2
  exit 2
fi

default_jobs=${TRANSCODE_DEFAULT_JOBS:-1}
max_jobs=${TRANSCODE_MAX_JOBS:-3}
poll_seconds=${TRANSCODE_POLL_SECONDS:-1}
throughput_seconds=${TRANSCODE_THROUGHPUT_SECONDS:-5}
redraw_stats_setting=${TRANSCODE_REDRAW_STATS:-auto}
control_file=${TRANSCODE_CONTROL_FILE:-"$PWD/vhs_transcode_parallelism"}
if [[ -n "$resume_run_dir" ]]; then
  run_dir=$resume_run_dir
elif [[ -n "$run_dir_arg" ]]; then
  run_dir=$run_dir_arg
else
  run_dir="$PWD/transcode_parallel_runs/$(date +"%Y%m%d_%H%M%S")"
fi
log_dir="$run_dir/logs"
status_dir="$run_dir/status"
throughput_state_dir="$run_dir/throughput_state"

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

if [[ -n "$resume_run_dir" ]]; then
  if [[ ! -d "$run_dir" ]]; then
    echo "Resume run directory does not exist: $run_dir" >&2
    exit 2
  fi
  if [[ ! -d "$log_dir" || ! -d "$status_dir" ]]; then
    echo "Resume run directory must contain logs/ and status/: $run_dir" >&2
    exit 2
  fi
else
  if [[ -d "$run_dir" && -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "Run directory already exists and is not empty: $run_dir" >&2
    echo "Use --resume '$run_dir' to continue an existing run." >&2
    exit 2
  fi
fi

mkdir -p "$log_dir" "$status_dir" "$throughput_state_dir"

resume_completed_output=
if [[ -n "$resume_run_dir" ]]; then
  resume_completed_output=$(python3 - "$task_file" "$run_dir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

task_file = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
log_dir = run_dir / "logs"
status_dir = run_dir / "status"


def parse_tasks(path: Path) -> list[str]:
    tasks: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tasks.append(line)
    return tasks


def file_size(path_text: str | None) -> str:
    if not path_text:
        return "unknown"
    path = Path(path_text)
    try:
        return str(path.stat().st_size)
    except OSError:
        return "missing"


def parse_log(log_path: Path) -> tuple[str | None, str | None]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    command = None
    output = None
    for line in text.splitlines():
        if command is None and line.startswith("command: "):
            command = line[len("command: ") :]
        elif line.startswith("Output: "):
            output = line[len("Output: ") :].strip()
    return command, output


def read_status(status_path: Path) -> str | None:
    try:
        return status_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None


def task_numbers(path: Path, suffix: str) -> set[int]:
    numbers: set[int] = set()
    pattern = re.compile(r"^task_([0-9]+)" + re.escape(suffix) + r"$")
    for child in path.iterdir():
        match = pattern.match(child.name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


tasks = parse_tasks(task_file)
total = len(tasks)
old_numbers = task_numbers(log_dir, ".log") | task_numbers(status_dir, ".status")
errors: list[str] = []
rows: list[dict[str, str | int | None]] = []
completed: list[int] = []

for task_number in sorted(n for n in old_numbers if n > total):
    errors.append(
        f"existing run has task {task_number}, but current task file only has {total} task(s)"
    )

for task_number in range(1, total + 1):
    log_path = log_dir / f"task_{task_number}.log"
    status_path = status_dir / f"task_{task_number}.status"
    has_log = log_path.exists()
    has_status = status_path.exists()

    if not has_log and not has_status:
        rows.append(
            {
                "task": task_number,
                "status": "not-started",
                "action": "run",
                "output": None,
                "size": "unknown",
            }
        )
        continue

    if has_status and not has_log:
        errors.append(f"task {task_number} has a status file but no log file")
        continue

    command, output = parse_log(log_path)
    if command is None:
        errors.append(f"task {task_number} log is missing its command line")
        continue
    if command != tasks[task_number - 1]:
        errors.append(
            "task "
            f"{task_number} command mismatch\n"
            f"  log:  {command}\n"
            f"  task: {tasks[task_number - 1]}"
        )
        continue

    status = read_status(status_path)
    if status == "0":
        if not output:
            errors.append(f"task {task_number} completed but log has no Output line to verify")
            continue
        if not Path(output).is_file():
            errors.append(f"task {task_number} completed but output is missing: {output}")
            continue
        completed.append(task_number)
        rows.append(
            {
                "task": task_number,
                "status": "0",
                "action": "skip",
                "output": output,
                "size": file_size(output),
            }
        )
    else:
        rows.append(
            {
                "task": task_number,
                "status": status or "interrupted",
                "action": "rerun",
                "output": output,
                "size": file_size(output),
            }
        )

print(f"Resume report for {run_dir}", file=sys.stderr)
print(f"Tasks in current task file: {total}", file=sys.stderr)
print("", file=sys.stderr)
print(
    f"{'task':>4}  {'old_status':>11}  {'action':>6}  {'output_bytes':>12}  output",
    file=sys.stderr,
)
print(f"{'-' * 4}  {'-' * 11}  {'-' * 6}  {'-' * 12}  {'-' * 6}", file=sys.stderr)
for row in rows:
    print(
        f"{row['task']:>4}  {str(row['status']):>11}  {str(row['action']):>6}  "
        f"{str(row['size']):>12}  {row['output'] or '(unknown)'}",
        file=sys.stderr,
    )

if errors:
    print("", file=sys.stderr)
    print("Refusing to resume because validation failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(2)

rerun_count = sum(1 for row in rows if row["action"] == "rerun")
run_count = sum(1 for row in rows if row["action"] in {"run", "rerun"})
skip_count = len(completed)
print("", file=sys.stderr)
print(
    f"Summary: skip {skip_count}, run new {run_count - rerun_count}, rerun incomplete {rerun_count}",
    file=sys.stderr,
)

print(f"RUN_COUNT\t{run_count}")
for task_number in completed:
    print(f"COMPLETED\t{task_number}")
PY
  )
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
skip_tasks=()

for ((i=0; i<total; i++)); do
  skip_tasks[$i]=0
done

resume_run_count=0
if [[ -n "$resume_completed_output" ]]; then
  while IFS=$'\t' read -r resume_record_type resume_record_value; do
    [[ -z "$resume_record_type" ]] && continue
    if [[ "$resume_record_type" == "RUN_COUNT" ]]; then
      resume_run_count=$resume_record_value
    elif [[ "$resume_record_type" == "COMPLETED" ]]; then
      skip_tasks[$((resume_record_value - 1))]=1
      completed=$((completed + 1))
    fi
  done <<< "$resume_completed_output"
fi

if [[ -n "$resume_run_dir" && "$resume_run_count" != "0" ]]; then
  printf '\nType RESUME to start/rerun the non-completed tasks:\n' >&2
  IFS= read -r resume_answer
  if [[ "$resume_answer" != "RESUME" ]]; then
    echo "Resume aborted." >&2
    exit 130
  fi
fi

printf '%s\n' "$default_jobs" > "$control_file"

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

  rm -f "$status_file"
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
    while (( next_task < total && skip_tasks[$next_task] == 1 )); do
      next_task=$((next_task + 1))
    done
    (( next_task >= total )) && break
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
