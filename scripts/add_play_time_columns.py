#!/usr/bin/env python3

import argparse
import csv
import logging
import sys
from pathlib import Path

FPS = 30000 / 1001  # standard NTSC DV


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add play time columns to a DVRescue CSV using FramePos and NTSC DV frame rate."
    )
    parser.add_argument("input_csv", help="Input CSV file")
    parser.add_argument("-o", "--output", help="Output CSV file")
    parser.add_argument("--frame-col", default="FramePos", help="Frame index column name")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(levelname)s: %(message)s",
    )


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:09.6f}"


def get_output_path(input_path: Path, output_arg: str | None) -> Path:
    if output_arg:
        return Path(output_arg)
    return input_path.with_name(f"{input_path.stem}.with_play_time.csv")


def load_csv(input_path: Path, frame_col: str) -> tuple[list[dict[str, str]], list[str]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        if frame_col not in reader.fieldnames:
            raise ValueError(
                f"Column '{frame_col}' not found. Available columns: {', '.join(reader.fieldnames)}"
            )
        rows = list(reader)
        fieldnames = list(reader.fieldnames)
    return rows, fieldnames


def ensure_output_columns(fieldnames: list[str]) -> list[str]:
    output_fields = list(fieldnames)
    if "play_time_seconds" not in output_fields:
        output_fields.append("play_time_seconds")
    if "play_time_hhmmss" not in output_fields:
        output_fields.append("play_time_hhmmss")
    return output_fields


def add_play_time_columns(
    rows: list[dict[str, str]],
    frame_col: str,
) -> tuple[list[dict[str, str]], int, int]:
    processed = 0
    skipped = 0

    for i, row in enumerate(rows, start=1):
        raw = (row.get(frame_col) or "").strip()
        if not raw:
            skipped += 1
            row["play_time_seconds"] = ""
            row["play_time_hhmmss"] = ""
            logging.warning("Row %d: empty %s", i, frame_col)
            continue

        try:
            frame_pos = int(raw)
        except ValueError:
            skipped += 1
            row["play_time_seconds"] = ""
            row["play_time_hhmmss"] = ""
            logging.warning("Row %d: invalid %s=%r", i, frame_col, raw)
            continue

        seconds = frame_pos / FPS
        row["play_time_seconds"] = f"{seconds:.6f}"
        row["play_time_hhmmss"] = format_time(seconds)
        processed += 1

    return rows, processed, skipped


def write_csv(output_path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    input_path = Path(args.input_csv)
    if not input_path.exists():
        logging.error("Input file not found: %s", input_path)
        return 1

    output_path = get_output_path(input_path, args.output)

    try:
        logging.info("Reading %s", input_path)
        rows, fieldnames = load_csv(input_path, args.frame_col)
        output_fields = ensure_output_columns(fieldnames)

        rows, processed, skipped = add_play_time_columns(rows, args.frame_col)

        logging.info("Writing %s", output_path)
        write_csv(output_path, rows, output_fields)

        logging.info("Done. Processed %d rows, skipped %d rows", processed, skipped)
        return 0

    except ValueError as e:
        logging.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
