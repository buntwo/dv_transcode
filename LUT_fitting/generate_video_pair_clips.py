#!/usr/bin/env python3
import argparse
import shlex
import subprocess
from pathlib import Path


DEFAULT_JOBS = (
    ("video_pairs_raw_training", "generated_video_pairs/train"),
    ("video_pairs_raw_validation", "generated_video_pairs/validation"),
)


def resolve_input(path_text, roots):
    path = Path(path_text).expanduser()
    if path.is_absolute():
        if path.exists():
            return path
        raise FileNotFoundError(path)

    for root in roots:
        candidate = root / path
        if candidate.exists():
            return candidate

    raise FileNotFoundError(path_text)


def parse_row(line, roots):
    tokens = shlex.split(line)
    ss1 = None
    ss2 = None

    index = 0
    while index < len(tokens):
        if tokens[index] == "-ss1":
            ss1 = tokens[index + 1]
            index += 2
        elif tokens[index] == "-ss2":
            ss2 = tokens[index + 1]
            index += 2
        else:
            index += 1

    if ss1 is None or ss2 is None:
        raise ValueError(f"Missing -ss1/-ss2 in row: {line}")
    if len(tokens) < 2:
        raise ValueError(f"Missing input paths in row: {line}")

    input_a = resolve_input(tokens[-2], roots)
    input_b = resolve_input(tokens[-1], roots)
    return ss1, ss2, input_a, input_b


def read_manifest(path):
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(line)
    return rows


def run_ffmpeg(start, input_path, output_path, duration, overwrite):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if overwrite:
        cmd.append("-y")
    else:
        cmd.append("-n")

    cmd.extend(
        [
            "-ss",
            str(start),
            "-i",
            str(input_path),
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "setpts=PTS-STARTPTS",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-slicecrc",
            "1",
            str(output_path),
        ]
    )
    subprocess.run(cmd, check=True)


def generate_manifest(manifest_path, output_dir, roots, duration, overwrite):
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = read_manifest(manifest_path)

    for row_index, row in enumerate(rows, start=1):
        ss1, ss2, input_a, input_b = parse_row(row, roots)
        output_a = output_dir / f"pair_{row_index:03d}_A.mkv"
        output_b = output_dir / f"pair_{row_index:03d}_B.mkv"

        print(f"{manifest_path.name} row {row_index:03d}: A -> {output_a}")
        run_ffmpeg(ss1, input_a, output_a, duration, overwrite)

        print(f"{manifest_path.name} row {row_index:03d}: B -> {output_b}")
        run_ffmpeg(ss2, input_b, output_b, duration, overwrite)


def main():
    parser = argparse.ArgumentParser(
        description="Generate 10-second FFV1 pair clips from clip-selection manifests."
    )
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        default=[],
        help="Extra root for resolving relative input paths. May be repeated.",
    )
    parser.add_argument(
        "--job",
        action="append",
        nargs=2,
        metavar=("MANIFEST", "OUTPUT_DIR"),
        help="Manifest/output pair. Defaults to training and validation manifests.",
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    roots = [cwd, *[root.expanduser() for root in args.root]]
    jobs = args.job or DEFAULT_JOBS

    for manifest_text, output_text in jobs:
        generate_manifest(
            manifest_path=Path(manifest_text),
            output_dir=Path(output_text),
            roots=roots,
            duration=args.duration,
            overwrite=args.overwrite,
        )


if __name__ == "__main__":
    main()
