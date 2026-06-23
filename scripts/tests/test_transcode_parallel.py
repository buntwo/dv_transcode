from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "transcode_parallel.sh"


def shell_python(code: str, *args: Path | str) -> str:
    parts = [shlex.quote(sys.executable), "-c", shlex.quote(f"exec({code!r})")]
    parts.extend(shlex.quote(str(arg)) for arg in args)
    return " ".join(parts)


def runner_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TRANSCODE_DEFAULT_JOBS": "1",
            "TRANSCODE_MAX_JOBS": "2",
            "TRANSCODE_POLL_SECONDS": "0.1",
            "TRANSCODE_THROUGHPUT_SECONDS": "0",
            "TRANSCODE_REDRAW_STATS": "0",
        }
    )
    env.update(overrides)
    return env


def run_runner(task_file: Path, cwd: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RUNNER), str(task_file)],
        cwd=cwd,
        env=runner_env(**env_overrides),
        text=True,
        capture_output=True,
        check=False,
    )


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def tick_task(path: Path, *, count: int = 80, sleep_seconds: float = 0.05) -> str:
    return shell_python(
        (
            "import pathlib, sys, time; "
            "p = pathlib.Path(sys.argv[1]); "
            "count = int(sys.argv[2]); "
            "sleep_seconds = float(sys.argv[3]); "
            "\nfor i in range(count):\n"
            "    with p.open('a', encoding='utf-8') as f:\n"
            "        f.write(f'{i}\\n')\n"
            "    time.sleep(sleep_seconds)\n"
        ),
        path,
        str(count),
        str(sleep_seconds),
    )


def start_runner(task_file: Path, cwd: Path, log_path: Path, **env_overrides: str) -> subprocess.Popen[str]:
    log_file = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            ["bash", str(RUNNER), str(task_file)],
            cwd=cwd,
            env=runner_env(**env_overrides),
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_file.close()


def test_default_control_and_run_dirs_live_in_cwd(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(
        shell_python(
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('done\\n')",
            out,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_runner(tasks, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.read_text(encoding="utf-8") == "done\n"
    assert (tmp_path / "vhs_transcode_parallelism").read_text(encoding="utf-8") == "1\n"
    assert (tmp_path / "transcode_parallel_runs").is_dir()
    assert "Control file: " + str(tmp_path / "vhs_transcode_parallelism") in result.stdout
    assert "Run directory: " + str(tmp_path / "transcode_parallel_runs") in result.stdout


def test_failure_returns_nonzero_but_drains_queue(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(
        "\n".join(
            [
                shell_python("from pathlib import Path; Path(__import__('sys').argv[1]).write_text('a\\n')", out),
                shell_python("import sys; sys.exit(7)"),
                shell_python("from pathlib import Path; Path(__import__('sys').argv[1]).open('a').write('b\\n')", out),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_runner(tasks, tmp_path, TRANSCODE_DEFAULT_JOBS="2")

    assert result.returncode == 1
    assert out.read_text(encoding="utf-8") == "a\nb\n"
    combined = result.stdout + result.stderr
    assert "FAILED task 2/3 rc=7" in combined
    assert "complete: 3/3 tasks, failures=1" in combined


def test_parallelism_zero_pauses_child_process_group(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks.txt"
    tasks = tmp_path / "tasks.txt"
    scheduler_log = tmp_path / "scheduler.log"
    control = tmp_path / "vhs_transcode_parallelism"
    tasks.write_text(tick_task(ticks, count=40, sleep_seconds=0.1) + "\n", encoding="utf-8")

    proc = start_runner(tasks, tmp_path, scheduler_log)

    try:
        wait_until(lambda: count_lines(ticks) >= 3)
        control.write_text("0\n", encoding="utf-8")
        wait_until(lambda: "paused task 1" in scheduler_log.read_text(encoding="utf-8"))

        paused_count = count_lines(ticks)
        time.sleep(0.5)
        assert count_lines(ticks) == paused_count

        control.write_text("1\n", encoding="utf-8")
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    assert proc.returncode == 0
    assert count_lines(ticks) == 40
    log = scheduler_log.read_text(encoding="utf-8")
    assert "resumed task 1" in log
    assert "complete: 1/1 tasks, failures=0" in log


def test_lowering_parallelism_from_two_to_one_pauses_one_running_task(tmp_path: Path) -> None:
    task_1_ticks = tmp_path / "task_1_ticks.txt"
    task_2_ticks = tmp_path / "task_2_ticks.txt"
    tasks = tmp_path / "tasks.txt"
    scheduler_log = tmp_path / "scheduler.log"
    control = tmp_path / "vhs_transcode_parallelism"
    tasks.write_text(
        "\n".join(
            [
                tick_task(task_1_ticks),
                tick_task(task_2_ticks),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    proc = start_runner(tasks, tmp_path, scheduler_log, TRANSCODE_DEFAULT_JOBS="2")

    try:
        wait_until(lambda: count_lines(task_1_ticks) >= 3 and count_lines(task_2_ticks) >= 3)
        control.write_text("1\n", encoding="utf-8")
        wait_until(lambda: "paused task 2" in scheduler_log.read_text(encoding="utf-8"))

        task_1_before = count_lines(task_1_ticks)
        task_2_before = count_lines(task_2_ticks)
        time.sleep(0.4)

        assert count_lines(task_1_ticks) > task_1_before
        assert count_lines(task_2_ticks) == task_2_before

        control.write_text("2\n", encoding="utf-8")
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    assert proc.returncode == 0
    log = scheduler_log.read_text(encoding="utf-8")
    assert "resumed task 2" in log
    assert "complete: 2/2 tasks, failures=0" in log


def test_raising_parallelism_resumes_paused_task_before_launching_queued_task(tmp_path: Path) -> None:
    task_1_ticks = tmp_path / "task_1_ticks.txt"
    task_2_ticks = tmp_path / "task_2_ticks.txt"
    task_3_ticks = tmp_path / "task_3_ticks.txt"
    tasks = tmp_path / "tasks.txt"
    scheduler_log = tmp_path / "scheduler.log"
    control = tmp_path / "vhs_transcode_parallelism"
    tasks.write_text(
        "\n".join(
            [
                tick_task(task_1_ticks),
                tick_task(task_2_ticks),
                tick_task(task_3_ticks),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    proc = start_runner(tasks, tmp_path, scheduler_log, TRANSCODE_DEFAULT_JOBS="2")

    try:
        wait_until(lambda: count_lines(task_1_ticks) >= 3 and count_lines(task_2_ticks) >= 3)
        control.write_text("1\n", encoding="utf-8")
        wait_until(lambda: "paused task 2" in scheduler_log.read_text(encoding="utf-8"))

        control.write_text("2\n", encoding="utf-8")
        wait_until(lambda: "resumed task 2" in scheduler_log.read_text(encoding="utf-8"))

        log_after_resume = scheduler_log.read_text(encoding="utf-8")
        assert "started task 3/3" not in log_after_resume

        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    assert proc.returncode == 0
    log = scheduler_log.read_text(encoding="utf-8")
    assert log.index("resumed task 2") < log.index("started task 3/3")
    assert "complete: 3/3 tasks, failures=0" in log
