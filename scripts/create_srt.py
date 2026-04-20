#!/usr/bin/env python3

import argparse
import csv
import logging
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create an SRT subtitle file from a CSV with rdt and play_time_seconds columns."
    )
    parser.add_argument("input_csv", help="Input CSV file")
    parser.add_argument("-o", "--output", help="Output SRT file")
    parser.add_argument("--time-col", default="play_time_seconds", help="Playback time column")
    parser.add_argument("--rdt-col", default="rdt", help="Record timestamp column")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level), format="%(levelname)s: %(message)s")


def get_output_path(input_path: Path, output_arg: str | None) -> Path:
    if output_arg:
        return Path(output_arg)
    return input_path.with_name(f"{input_path.stem}.srt")


def truncate_rdt_to_seconds(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "." in value:
        value = value.split(".", 1)[0]
    return value


def format_srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    secs = total_ms // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def load_rows(input_path: Path) -> list[dict[str, str]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        return list(reader)


def collect_second_buckets(rows: list[dict[str, str]], time_col: str, rdt_col: str):
    buckets: dict[int, str] = {}

    for i, row in enumerate(rows, start=1):
        raw_time = (row.get(time_col) or "").strip()
        raw_rdt = truncate_rdt_to_seconds(row.get(rdt_col) or "")

        if not raw_time:
            logging.warning("Row %d: empty %s", i, time_col)
            continue
        if not raw_rdt:
            continue

        try:
            play_time = float(raw_time)
        except ValueError:
            logging.warning("Row %d: invalid %s=%r", i, time_col, raw_time)
            continue

        sec = int(play_time)

        if sec not in buckets:
            buckets[sec] = raw_rdt

    return buckets


def build_cues(second_map: dict[int, str]):
    cues = []
    for sec in sorted(second_map):
        text = second_map[sec]
        start = float(sec)
        end = float(sec + 1)
        cues.append((start, end, text))
    return cues


def write_srt(output_path: Path, cues):
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        for idx, (start, end, text) in enumerate(cues, start=1):
            f.write(f"{idx}\n")
            f.write(f"{format_srt_time(start)} --> {format_srt_time(end)}\n")
            f.write(f"{text}\n\n")


def main():
    args = parse_args()
    setup_logging(args.log_level)

    input_path = Path(args.input_csv)
    if not input_path.exists():
        logging.error("Input file not found: %s", input_path)
        return 1

    output_path = get_output_path(input_path, args.output)

    try:
        logging.info("Reading %s", input_path)
        rows = load_rows(input_path)

        if rows:
            header = rows[0].keys()
            if args.time_col not in header:
                raise ValueError(f"Column '{args.time_col}' not found")
            if args.rdt_col not in header:
                raise ValueError(f"Column '{args.rdt_col}' not found")

        second_map = collect_second_buckets(rows, args.time_col, args.rdt_col)
        cues = build_cues(second_map)

        logging.info("Writing %s", output_path)
        write_srt(output_path, cues)

        logging.info("Done. Wrote %d subtitle cues", len(cues))
        return 0

    except ValueError as e:
        logging.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
