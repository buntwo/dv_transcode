#!/usr/bin/env python3
"""Blinded randomized A/B testing for choosing the better of two video files."""

from __future__ import annotations

import argparse
import math
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from multi_player import DEFAULT_HEIGHT
from multi_player import DEFAULT_WIDTH
from multi_player import DEFAULT_X
from multi_player import DEFAULT_Y
from multi_player import WindowGeometry
from multi_player import parse_timestamp


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
CORRECT_MARK = f"{GREEN}✓{RESET}"
INCORRECT_MARK = f"{RED}✗{RESET}"


@dataclass(frozen=True)
class Trial:
    round_index: int
    source_for_a: int
    source_for_b: int


@dataclass(frozen=True)
class TrialResult:
    trial: Trial
    choice: str

    @property
    def chosen_source(self) -> int:
        return self.trial.source_for_a if self.choice == "a" else self.trial.source_for_b

    @property
    def is_correct(self) -> bool:
        return self.chosen_source == 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a blinded randomized A/B test where video 1 is the better video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("videos", nargs=2, type=Path)
    parser.add_argument("--duration", type=positive_float, required=True, help="Seconds to play for each clip.")
    parser.add_argument("--rounds", type=positive_int, required=True, help="Number of rounds to run.")
    parser.add_argument("--width", type=positive_int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=positive_int, default=DEFAULT_HEIGHT)
    parser.add_argument("-x", type=non_negative_int, default=DEFAULT_X)
    parser.add_argument("-y", type=non_negative_int, default=DEFAULT_Y)
    parser.add_argument("--monitor", type=positive_int)
    parser.add_argument(
        "-ss",
        dest="ss",
        type=parse_timestamp,
        metavar="TIMESTAMP",
        help="Start offset for both videos. Numbered -ss1/-ss2 values override this per video.",
    )
    for index in (1, 2):
        parser.add_argument(
            f"-ss{index}",
            dest=f"ss{index}",
            type=parse_timestamp,
            metavar="TIMESTAMP",
            help=f"Start offset for video {index}. Accepts seconds, MM:SS, or HH:MM:SS.",
        )
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def collect_start_times(args: argparse.Namespace) -> list[float]:
    default_start_time = args.ss or 0.0
    return [
        specific if (specific := getattr(args, f"ss{index}")) is not None else default_start_time
        for index in (1, 2)
    ]


def validate_inputs(args: argparse.Namespace) -> None:
    for index, path in enumerate(args.videos, start=1):
        if not path.exists():
            raise ValueError(f"video {index} does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"video {index} is not a file: {path}")
    if shutil.which("mpv") is None:
        raise ValueError("mpv is required; install with: brew install mpv")


def make_trial(round_index: int, rng: random.Random) -> Trial:
    if rng.randrange(2) == 0:
        return Trial(round_index=round_index, source_for_a=1, source_for_b=2)
    return Trial(round_index=round_index, source_for_a=2, source_for_b=1)


def build_mpv_command(
    video: Path,
    title: str,
    geometry: WindowGeometry,
    start_seconds: float,
    duration_seconds: float,
    screen: int | None,
) -> list[str]:
    cmd = [
        "mpv",
        "--no-terminal",
        "--ontop",
        "--force-window=immediate",
        "--no-resume-playback",
        f"--start={start_seconds}",
        f"--length={duration_seconds}",
        f"--geometry={geometry.mpv_value()}",
        f"--title={title}",
    ]
    if screen is not None:
        cmd.append(f"--screen={screen}")
    cmd.append(str(video))
    return cmd


def play_clip(
    video: Path,
    title: str,
    geometry: WindowGeometry,
    start_seconds: float,
    duration_seconds: float,
    screen: int | None,
) -> None:
    subprocess.run(
        build_mpv_command(video, title, geometry, start_seconds, duration_seconds, screen),
        check=True,
    )


def prompt_choice(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> str:
    while True:
        print("Choose [a/b], replay [1=A, 2=B], quit [q]: ", end="", flush=True, file=stdout)
        response = stdin.readline()
        if response == "":
            return "q"
        choice = response.strip().lower()
        if choice in {"a", "b", "q", "1", "2"}:
            return choice
        print("Please enter a, b, 1, 2, or q.", file=stdout)


def correctness_counts(results: list[TrialResult]) -> tuple[int, int]:
    correct = sum(1 for result in results if result.is_correct)
    return correct, len(results) - correct


def two_sided_binomial_p_value(successes: int, trials: int) -> float:
    if trials <= 0:
        return 1.0
    tail = min(successes, trials - successes)
    probability = sum(math.comb(trials, count) for count in range(tail + 1)) / (2**trials)
    return min(1.0, 2 * probability)


def one_sided_binomial_p_value(successes: int, trials: int) -> float:
    if trials <= 0:
        return 1.0
    return sum(math.comb(trials, count) for count in range(successes, trials + 1)) / (2**trials)


def format_summary(results: list[TrialResult], alpha: float = 0.05) -> str:
    completed = len(results)
    if completed == 0:
        return "No completed rounds."

    correct, incorrect = correctness_counts(results)
    p_value = one_sided_binomial_p_value(correct, completed)
    significance = "statistically significant" if p_value < alpha else "not statistically significant"
    round_lines = [f"{result.trial.round_index:<5}  {CORRECT_MARK if result.is_correct else INCORRECT_MARK}" for result in results]
    return "\n".join(
        [
            "Results",
            "",
            "Round  Correct",
            "-----  -------",
            *round_lines,
            "",
            f"Total: {correct}/{completed} correct ({correct / completed:.1%})",
            f"Exact binomial p-value: {p_value:.6g}",
            f"Alpha {alpha:g}: {significance}",
        ]
    )


def play_trial_presentation(
    args: argparse.Namespace,
    trial: Trial,
    label: str,
    geometry: WindowGeometry,
    start_times: list[float],
    screen: int | None,
) -> None:
    source = trial.source_for_a if label == "A" else trial.source_for_b
    play_clip(
        args.videos[source - 1],
        f"Round {trial.round_index}/{args.rounds} - {label}",
        geometry,
        start_times[source - 1],
        args.duration,
        screen,
    )


def run_trials(
    args: argparse.Namespace,
    rng: random.Random,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> list[TrialResult]:
    start_times = collect_start_times(args)
    geometry = WindowGeometry(width=args.width, height=args.height, x=args.x, y=args.y)
    screen = args.monitor - 1 if args.monitor is not None else None
    results: list[TrialResult] = []

    for round_index in range(1, args.rounds + 1):
        trial = make_trial(round_index, rng)
        print(f"\nRound {round_index}/{args.rounds}: ", end="", file=stdout)
        for label in ("A", "B"):
            print(f"Playing {label}... ", end="", flush=True, file=stdout)
            play_trial_presentation(args, trial, label, geometry, start_times, screen)
        print(file=stdout)

        while True:
            choice = prompt_choice(stdin, stdout)
            if choice == "1":
                print(f"Round {round_index}/{args.rounds}: Replaying A...", file=stdout)
                play_trial_presentation(args, trial, "A", geometry, start_times, screen)
                continue
            if choice == "2":
                print(f"Round {round_index}/{args.rounds}: Replaying B...", file=stdout)
                play_trial_presentation(args, trial, "B", geometry, start_times, screen)
                continue
            break
        if choice == "q":
            break
        results.append(TrialResult(trial=trial, choice=choice))
    return results


def run(args: argparse.Namespace, rng: random.Random | None = None) -> int:
    validate_inputs(args)
    results = run_trials(args, rng or random.Random())
    print()
    print(format_summary(results))
    return 0 if results else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
