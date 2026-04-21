#!/usr/bin/env python3
# Rewrite of transcode2.sh to support Digital8 as well
# Coded by ChatGPT

from __future__ import annotations

import argparse
import csv
import shlex
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from transcode_naming import build_access_output_name
from utils import auto_sibling_dir_for_path


@dataclass
class Config:
    mode: str
    format_type: str
    start: str | None
    end: str | None
    crop_bottom: int
    pad_bottom: int
    denoise: str
    q: int
    codec: str
    deint_mode: str
    map_both_audio: bool
    log_level: str
    assume_yes: bool
    output_suffix: str
    originals_dirname: str
    access_dirname: str
    logs_dirname: str


@dataclass
class Paths:
    input_file: Path
    stem: str
    out_dir: Path
    log_dir: Path
    output_file: Path
    ffmpeg_log_file: Path
    command_log_file: Path
    csv_raw: Path
    csv_with_play: Path
    srt_file: Path
    add_play_time_script: Path
    create_srt_script: Path


try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


def parse_args() -> tuple[Config, list[Path]]:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Transcode DV with VideoToolbox, with optional Digital8 DVRescue subtitle burn-in."
    )
    parser.add_argument("--mode", choices=["transcode", "preview"], default="transcode")
    parser.add_argument("--format", dest="format_type", choices=["video8", "digital8"], required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--crop-bottom", type=int)
    parser.add_argument("--pad-bottom", type=int)
    parser.add_argument("--denoise", choices=["off", "verylight", "light", "medium", "strong"])
    parser.add_argument("--q", type=int, default=70)
    parser.add_argument("--codec", choices=["h264", "hevc"], default="hevc")
    parser.add_argument("--deint-mode", choices=["send_frame", "send_field"], default="send_field")
    parser.add_argument("--map-both-audio", action="store_true")
    parser.add_argument("--log-level", choices=["quiet", "error", "warning", "info"], default="warning")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-suffix", default="")
    parser.add_argument("--originals-dirname", default="Originals")
    parser.add_argument("--access-dirname", default="Access")
    parser.add_argument("--logs-dirname", default="Logs")
    parser.add_argument("input_files", nargs="+")
    args = parser.parse_args()

    crop_bottom = args.crop_bottom
    denoise = args.denoise
    if crop_bottom is None:
        crop_bottom = 7 if args.format_type == "video8" else 0
    if denoise is None:
        denoise = "light"

    cfg = Config(
        mode=args.mode,
        format_type=args.format_type,
        start=args.start,
        end=args.end,
        crop_bottom=crop_bottom,
        pad_bottom=args.pad_bottom if args.pad_bottom is not None else crop_bottom,
        denoise=denoise,
        q=args.q,
        codec=args.codec,
        deint_mode=args.deint_mode,
        map_both_audio=args.map_both_audio,
        log_level=args.log_level,
        assume_yes=args.yes,
        output_suffix=args.output_suffix,
        originals_dirname=args.originals_dirname,
        access_dirname=args.access_dirname,
        logs_dirname=args.logs_dirname,
    )
    input_files = [Path(p) for p in args.input_files]
    return cfg, input_files


def build_paths(cfg: Config, input_file: Path) -> Paths:
    """Build commonly used input, output, and log paths."""
    input_file = input_file.resolve()
    if not input_file.is_file():
        raise SystemExit(f"Input is not a regular file: {input_file}")

    out_dir = auto_sibling_dir_for_path(
        input_file,
        originals_dirname=cfg.originals_dirname,
        sibling_dirname=cfg.access_dirname,
    )
    log_dir = auto_sibling_dir_for_path(
        input_file,
        originals_dirname=cfg.originals_dirname,
        sibling_dirname=cfg.logs_dirname,
    )

    stem = input_file.stem
    try:
        output_name = build_access_output_name(
            input_file,
            originals_dirname=cfg.originals_dirname,
            output_suffix=cfg.output_suffix,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).resolve().parent

    return Paths(
        input_file=input_file,
        stem=stem,
        out_dir=out_dir,
        log_dir=log_dir,
        output_file=out_dir / output_name,
        ffmpeg_log_file=log_dir / f"{stem}_access_{timestamp}.log",
        command_log_file=log_dir / f"{stem}_transcode_cmd_{timestamp}.log",
        csv_raw=log_dir / f"{stem}.frameinfo.csv",
        csv_with_play=log_dir / f"{stem}.frameinfo.with_play_time.csv",
        srt_file=log_dir / f"{stem}.record_time_overlay.srt",
        add_play_time_script=script_dir / "add_play_time_columns.py",
        create_srt_script=script_dir / "create_srt.py",
    )


def build_runtime_paths(paths: Paths, artifact_dir: Path) -> Paths:
    """Return a copy of Paths that writes runtime artifacts under artifact_dir."""
    return replace(
        paths,
        log_dir=artifact_dir,
        ffmpeg_log_file=artifact_dir / paths.ffmpeg_log_file.name,
        csv_raw=artifact_dir / paths.csv_raw.name,
        csv_with_play=artifact_dir / paths.csv_with_play.name,
        srt_file=artifact_dir / paths.srt_file.name,
    )


def get_hqdn3d_args(preset: str) -> str | None:
    """Return hqdn3d parameters for the named preset."""
    mapping = {
        "off": None,
        "verylight": "1.5:1.125:2.25:1.6875",
        "light": "2:1.5:3:2",
        "medium": "3:2.25:4.5:3.375",
        "strong": "4:3:6:4.5",
    }
    if preset not in mapping:
        raise SystemExit(f"Unknown denoise preset: {preset}")
    return mapping[preset]


def escape_ffmpeg_filter_value(value: str) -> str:
    """Escape a filename for safe use inside an ffmpeg filter expression."""
    for a, b in [
        ("\\", "\\\\"),
        (":", r"\:"),
        (",", r"\,"),
        (";", r"\;"),
        ("[", r"\["),
        ("]", r"\]"),
        ("=", r"\="),
        ("'", r"\'"),
    ]:
        value = value.replace(a, b)
    return value


def build_vf(cfg: Config, paths: Paths) -> str:
    """Build the ffmpeg video filter chain."""
    filters = [f"bwdif=mode={cfg.deint_mode}:parity=auto:deint=all"]

    if cfg.crop_bottom > 0:
        filters.append(f"crop=iw:ih-{cfg.crop_bottom}:0:0")
    if cfg.pad_bottom > 0:
        filters.append(f"pad=iw:ih+{cfg.pad_bottom}:0:0:black")

    if hqdn3d := get_hqdn3d_args(cfg.denoise):
        filters.append(f"hqdn3d={hqdn3d}")

    filters += [
        "scale='trunc(ih*dar/2)*2:ih'",
        "setsar=1",
        "setparams=range=limited:color_primaries=smpte170m:color_trc=smpte170m:colorspace=smpte170m",
    ]

    if cfg.format_type == "digital8":
        style = (
            "Alignment=3,"
            "MarginV=3,"
            "MarginR=3,"
            "FontName=Helvetica,"
            "Fontsize=12,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,"
            "Outline=0.5,"
            "Shadow=0"
        )
        filters.append(
            f"subtitles=filename={escape_ffmpeg_filter_value(str(paths.srt_file))}:force_style='{style}'"
        )

    return ",".join(filters)


def build_ffmpeg_args(cfg: Config, paths: Paths, vf: str, preview: bool) -> list[str]:
    """Build the ffmpeg command-line argument list."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", cfg.log_level, "-stats", "-stats_period", "1"]

    if cfg.start:
        args += ["-ss", cfg.start]
    if cfg.end:
        args += ["-to", cfg.end]

    args += ["-i", str(paths.input_file), "-vf", vf, "-map", "0:v:0"]
    args += ["-map", "0:a:0?", "-map", "0:a:1?"] if cfg.map_both_audio else ["-map", "0:a:0?"]

    if cfg.codec == "h264":
        args += ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-coder", "cabac"]
    else:
        args += ["-c:v", "hevc_videotoolbox", "-profile:v", "main", "-tag:v", "hvc1"]

    args += [
        "-spatial_aq", "1",
        "-max_ref_frames", "4",
        "-q:v", str(cfg.q),
        "-color_range", "tv",
        "-color_primaries", "smpte170m",
        "-color_trc", "smpte170m",
        "-colorspace", "smpte170m",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
    ]

    return args + (["-f", "matroska", "-"] if preview else [str(paths.output_file)])


def shjoin(args: list[str]) -> str:
    """Return a shell-escaped command string for display/logging."""
    return shlex.join(args)


def run_checked(
    args: list[str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    stdout=None,
) -> None:
    """Run a subprocess, optionally redirecting stdout/stderr."""
    stdout_file = None
    stderr_file = None
    try:
        if stdout_path is not None:
            stdout_file = stdout_path.open("w", encoding="utf-8", newline="\n")
            stdout = stdout_file
        if stderr_path is not None:
            stderr_file = stderr_path.open("w", encoding="utf-8", newline="\n")
        subprocess.run(args, check=True, stdout=stdout, stderr=stderr_file)
    finally:
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()


def extract_first_rdt_yyyymmdd(csv_path: Path) -> str | None:
    """Extract the first YYYYMMDD date prefix from the Digital8 frameinfo CSV."""
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if raw := (row.get("rdt") or "").strip():
                parts = raw.split(" ", 1)[0].split("-")
                if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
                    return "".join(parts)
    return None


def generate_digital8_sidecars(paths: Paths) -> None:
    """Generate CSV, play-time CSV, and SRT sidecars for Digital8 inputs."""
    for script in (paths.add_play_time_script, paths.create_srt_script):
        if not script.exists():
            raise SystemExit(f"Missing script: {script}")

    print("Generating Digital8 CSV/SRT sidecars...")
    run_checked(
        ["dvrescue", "--csv", str(paths.input_file), "-m", "-"],
        stderr_path=paths.csv_raw,
        stdout=subprocess.DEVNULL,
    )
    run_checked(["python3", str(paths.add_play_time_script), str(paths.csv_raw), "-o", str(paths.csv_with_play)])
    run_checked(["python3", str(paths.create_srt_script), str(paths.csv_with_play), "-o", str(paths.srt_file)])

    if yyyymmdd := extract_first_rdt_yyyymmdd(paths.csv_with_play):
        paths.output_file = paths.output_file.with_name(f"{yyyymmdd}_{paths.output_file.name}")
    else:
        print("Warning: could not find first-frame rdt date; leaving output filename unchanged.")


def print_summary(cfg: Config, paths: Paths, ffmpeg_args: list[str], preview: bool) -> None:
    """Print a summary of the transcode job and ffmpeg command."""
    print(f"Mode: {cfg.mode}")
    print(f"Format: {cfg.format_type}")
    print(f"Input: {paths.input_file}")
    print(f"Output dir: {paths.out_dir}")
    print(f"Log dir: {paths.log_dir}")
    if cfg.start or cfg.end:
        print(f"Range: {cfg.start or 'beginning'} -> {cfg.end or 'end'}")
    print(f"Codec: {cfg.codec}")
    print(f"Denoise preset: {cfg.denoise}")
    print(f"Bottom crop rows: {cfg.crop_bottom}")
    print(f"Bottom pad rows: {cfg.pad_bottom}")
    print(f"Deinterlace mode: {cfg.deint_mode}")
    if cfg.format_type == "digital8":
        print(f"CSV: {paths.csv_raw}")
        print(f"CSV w/ play time: {paths.csv_with_play}")
        print(f"SRT: {paths.srt_file}")

    if preview:
        print("\nPreview pipeline:\n")
    else:
        print(f"Output: {paths.output_file}")
        print(f"Log: {paths.ffmpeg_log_file}\n")
        print("Running command:\n")

    print(shjoin(ffmpeg_args))
    print()


def write_command_log(cfg: Config, paths: Paths, ffmpeg_args: list[str]) -> None:
    """Write a human-readable command log for the transcode."""
    lines = [
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Input: {paths.input_file}",
        f"Output: {paths.output_file}",
        f"Mode: {cfg.mode}",
        f"Format: {cfg.format_type}",
        f"Codec: {cfg.codec}",
        f"Denoise: {cfg.denoise}",
        f"Crop bottom: {cfg.crop_bottom}",
        f"Pad bottom: {cfg.pad_bottom}",
    ]
    if cfg.format_type == "digital8":
        lines += [
            f"CSV: {paths.csv_raw}",
            f"CSV with play time: {paths.csv_with_play}",
            f"SRT: {paths.srt_file}",
        ]
    lines += ["", "Command:", shjoin(ffmpeg_args)]
    paths.command_log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tee_stream(stream, outputs: list, mirror_to_stderr: bool = False) -> None:
    """Tee a binary stream to one or more outputs, optionally mirroring to stderr."""
    try:
        while chunk := stream.read(8192):
            for out in outputs:
                out.write(chunk)
                out.flush()
            if mirror_to_stderr:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
    finally:
        stream.close()


def run_ffmpeg(ffmpeg_args: list[str], log_path: Path, preview_stem: str | None = None) -> int:
    """Run ffmpeg, optionally piping preview output to ffplay, while teeing stderr to a log."""
    with log_path.open("wb") as log_file:
        if preview_stem is None:
            proc = subprocess.Popen(ffmpeg_args, stderr=subprocess.PIPE, bufsize=0)
            assert proc.stderr is not None
            t = threading.Thread(target=tee_stream, args=(proc.stderr, [log_file], True), daemon=True)
            t.start()
            rc = proc.wait()
            t.join()
            return rc

        ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        assert ffmpeg_proc.stdout is not None
        assert ffmpeg_proc.stderr is not None

        ffplay_proc = subprocess.Popen(
            ["ffplay", "-hide_banner", "-window_title", f"{preview_stem} preview", "-"],
            stdin=ffmpeg_proc.stdout,
            bufsize=0,
        )
        ffmpeg_proc.stdout.close()

        t = threading.Thread(target=tee_stream, args=(ffmpeg_proc.stderr, [log_file], True), daemon=True)
        t.start()
        ffplay_rc = ffplay_proc.wait()
        ffmpeg_rc = ffmpeg_proc.wait()
        t.join()

        return ffmpeg_rc if ffmpeg_rc != 0 else ffplay_rc


def process_one_file(cfg: Config, input_file: Path, prompt: bool) -> int:
    """Process one input file through the transcode workflow."""
    preview = cfg.mode == "preview"
    if preview:
        persistent_paths = build_paths(cfg, input_file)
        with tempfile.TemporaryDirectory(prefix=f"{persistent_paths.stem}_preview_") as tmp:
            paths = build_runtime_paths(persistent_paths, Path(tmp))
            if cfg.format_type == "digital8":
                generate_digital8_sidecars(paths)
            ffmpeg_args = build_ffmpeg_args(cfg, paths, build_vf(cfg, paths), preview=True)
            print_summary(cfg, paths, ffmpeg_args, preview=True)
            return run_ffmpeg(ffmpeg_args, paths.ffmpeg_log_file, preview_stem=paths.stem)

    paths = build_paths(cfg, input_file)
    if cfg.format_type == "digital8":
        generate_digital8_sidecars(paths)

    ffmpeg_args = build_ffmpeg_args(cfg, paths, build_vf(cfg, paths), preview=False)
    print_summary(cfg, paths, ffmpeg_args, preview=False)

    if prompt:
        input("Press Enter to start transcode batch, or Ctrl-C to cancel...")

    write_command_log(cfg, paths, ffmpeg_args)
    rc = run_ffmpeg(ffmpeg_args, paths.ffmpeg_log_file)
    if rc == 0:
        print(f"Done: {paths.output_file}")
    return rc


def main() -> int:
    """Run the transcode workflow for one or more files."""
    cfg, input_files = parse_args()

    failures = 0
    prompted = cfg.assume_yes or cfg.mode == "preview"

    for input_file in input_files:
        print(f'processing {input_file.name}')
        try:
            rc = process_one_file(cfg, input_file, prompt=not prompted)
            prompted = True
            if rc != 0:
                failures += 1
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"ERROR processing {input_file}: {e}", file=sys.stderr)
            failures += 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
