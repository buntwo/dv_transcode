#!/usr/bin/env python3
"""Burn EXIF DateTimeOriginal timestamps into derivative image copies."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


IMAGE_EXTENSIONS = (
    "jpg",
    "jpeg",
    "tif",
    "tiff",
    "gif",
    "bmp",
    "png",
    "heic",
    "heif",
    "webp",
)

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) ")


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Missing required tool: {name}")


def valid_datetime_original(value: object) -> bool:
    match = DATE_RE.match(str(value)) if value else None
    if not match:
        return False
    year, month, day = map(int, match.groups())
    return year >= 1 and 1 <= month <= 12 and 1 <= day <= 31


def discover_images(source_root: Path) -> tuple[list[tuple[Path, str, str]], list[tuple[Path, str, str]]]:
    cmd = [
        "exiftool",
        "-r",
        "-q",
        "-q",
        "-json",
        "-FileType",
        "-MIMEType",
        "-EXIF:DateTimeOriginal",
        "-d",
        "%Y-%m-%d %H:%M:%S",
    ]
    for ext in IMAGE_EXTENSIONS:
        cmd += ["-ext", ext]
    cmd.append(str(source_root))

    items = json.loads(subprocess.check_output(cmd))
    valid: list[tuple[Path, str, str]] = []
    skipped: list[tuple[Path, str, str]] = []

    for item in items:
        source = Path(item["SourceFile"])
        date_time_original = item.get("DateTimeOriginal", "")
        file_type = item.get("FileType", "")
        row = (source, date_time_original, file_type)
        if valid_datetime_original(date_time_original):
            valid.append(row)
        else:
            skipped.append(row)

    return valid, skipped


def burn_timestamp(source: Path, destination: Path, timestamp: str) -> None:
    width, height = map(
        int,
        subprocess.check_output(
            ["magick", "identify", "-format", "%w %h", str(source)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).split(),
    )

    base = min(width, height)
    point_size = max(14, round(base * 0.035 * 0.75))
    padding = max(12, round(base * 0.020))
    stroke_width = round(max(0.5, point_size / 45), 2)

    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "magick",
            str(source),
            "-auto-orient",
            "-gravity",
            "southeast",
            "-font",
            "Helvetica",
            "-pointsize",
            str(point_size),
            "-fill",
            "white",
            "-stroke",
            "black",
            "-strokewidth",
            str(stroke_width),
            "-annotate",
            f"+{padding}+{padding}",
            timestamp,
            "-quality",
            "95",
            str(destination),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Preserve source metadata but clear Orientation because pixels were auto-oriented.
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-q",
            "-overwrite_original",
            "-TagsFromFile",
            str(source),
            "-all:all",
            "-unsafe",
            "-Orientation=",
            str(destination),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_manifest(path: Path, source_root: Path, rows: list[tuple[Path, str, str]]) -> None:
    with path.open("w") as file:
        file.write("source_relative_path\tDateTimeOriginal\tFileType\n")
        for source, date_time_original, file_type in rows:
            file.write(f"{source.relative_to(source_root)}\t{date_time_original or ''}\t{file_type}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror an image tree and burn EXIF DateTimeOriginal into derivative image files."
    )
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("/Users/btu/scratch/Videos/Disc_rips/Family"),
        help="Source image tree.",
    )
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        help="Destination tree. Defaults to a sibling named SOURCE_withTimestamp.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress after this many attempted conversions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source.resolve()
    destination_root = (
        args.destination.resolve()
        if args.destination
        else source_root.with_name(f"{source_root.name}_withTimestamp")
    )

    require_tool("exiftool")
    require_tool("magick")

    if not source_root.is_dir():
        raise SystemExit(f"Source directory does not exist: {source_root}")
    if destination_root.exists():
        raise SystemExit(f"Refusing to overwrite existing destination: {destination_root}")

    valid, skipped = discover_images(source_root)
    print(f"DISCOVERED_IMAGES\t{len(valid) + len(skipped)}", flush=True)
    print(f"TO_CONVERT_VALID_DATETIMEORIGINAL\t{len(valid)}", flush=True)
    print(f"SKIP_NO_VALID_DATETIMEORIGINAL\t{len(skipped)}", flush=True)

    destination_root.mkdir(parents=True)
    start = time.time()
    converted = 0
    failed: list[tuple[str, str]] = []

    for index, (source, timestamp, _file_type) in enumerate(valid, 1):
        relative_path = source.relative_to(source_root)
        destination = destination_root / relative_path
        try:
            burn_timestamp(source, destination, timestamp)
            converted += 1
        except Exception as exc:  # noqa: BLE001 - report and continue batch work.
            failed.append((str(relative_path), str(exc)))

        if index % args.progress_every == 0 or index == len(valid):
            elapsed = time.time() - start
            print(
                f"PROGRESS\t{index}/{len(valid)}"
                f"\tconverted={converted}\tfailed={len(failed)}\telapsed={elapsed:.1f}s",
                flush=True,
            )

    write_manifest(destination_root / "_timestamp_manifest.tsv", source_root, valid)
    write_manifest(destination_root / "_skipped_no_valid_DateTimeOriginal.tsv", source_root, skipped)

    if failed:
        failed_manifest = destination_root / "_failed_timestamp_conversion.tsv"
        with failed_manifest.open("w") as file:
            file.write("source_relative_path\terror\n")
            for relative_path, error in failed:
                file.write(f"{relative_path}\t{error}\n")
        print(f"FAILED_MANIFEST\t{failed_manifest}", flush=True)

    print(f"DONE\tconverted={converted}\tfailed={len(failed)}\tdestination={destination_root}", flush=True)
    print(f"MANIFEST\t{destination_root / '_timestamp_manifest.tsv'}", flush=True)
    print(
        f"SKIPPED_MANIFEST\t{destination_root / '_skipped_no_valid_DateTimeOriginal.tsv'}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
