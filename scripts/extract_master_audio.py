#!/usr/bin/env python3
"""Extract the first audio stream from master files into FLAC files."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


DEFAULT_AUDIO_CODEC = "flac"
OUTPUT_SUFFIX = ".flac"
AUDIO_STREAM_SELECTOR = "0:a:0"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the first audio stream from each input file as FLAC.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input_files", nargs="+", type=Path, help="Input master media files")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write extracted FLAC files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output FLAC files",
    )
    parser.add_argument(
        "--audio-channel",
        choices=["keep", "left", "right"],
        default="keep",
        help="Select a single source channel and duplicate it to both output channels",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if not args.output_dir.exists():
        args.output_dir.mkdir(parents=True, exist_ok=True)
    for input_path in args.input_files:
        if not input_path.exists():
            raise ValueError(f"input does not exist: {input_path}")
        if not input_path.is_file():
            raise ValueError(f"input is not a file: {input_path}")


def audio_output_path(input_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_path.stem}{OUTPUT_SUFFIX}"


def build_audio_channel_filter(audio_channel: str) -> str | None:
    if audio_channel == "left":
        return "pan=stereo|c0=c0|c1=c0"
    if audio_channel == "right":
        return "pan=stereo|c0=c1|c1=c1"
    return None


def build_extract_command(
    input_path: Path,
    output_path: Path,
    force: bool,
    audio_channel: str = "keep",
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-stats",
        "-loglevel",
        "info",
        *(["-y"] if force else []),
        "-i",
        str(input_path),
        "-map",
        AUDIO_STREAM_SELECTOR,
        "-vn",
        *(["-af", build_audio_channel_filter(audio_channel)] if build_audio_channel_filter(audio_channel) else []),
        "-c:a",
        DEFAULT_AUDIO_CODEC,
        str(output_path),
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        for input_path in args.input_files:
            output_path = audio_output_path(input_path, args.output_dir)
            if output_path.exists() and not args.force:
                raise ValueError(f"output already exists: {output_path}")
            cmd = build_extract_command(input_path, output_path, args.force, args.audio_channel)
            subprocess.run(cmd, check=True)
            print(f"Wrote {output_path}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"extract_master_audio.py: error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
