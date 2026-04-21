#!/usr/bin/env python3
# Rewrite of transcode2.sh to support Digital8 as well
# Coded by ChatGPT

from __future__ import annotations

import argparse
import csv
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Config:
    input_file: Path
    access_root: Path
    log_root: Path
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


@dataclass
class Paths:
    input_file: Path
    input_dir_abs: Path
    stem: str
    rel_dir: Path
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


def parse_args() -> Config:
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
    parser.add_argument("input_file")
    parser.add_argument("access_root")
    parser.add_argument("log_root")
    args = parser.parse_args()

    crop_bottom = args.crop_bottom
    denoise = args.denoise
    if crop_bottom is None:
        crop_bottom = 7 if args.format_type == "video8" else 0
    if denoise is None:
        denoise = "light" if args.format_type == "video8" else "light"
    pad_bottom = args.pad_bottom if args.pad_bottom is not None else crop_bottom

    return Config(
        input_file=Path(args.input_file),
        access_root=Path(args.access_root),
        log_root=Path(args.log_root),
        mode=args.mode,
        format_type=args.format_type,
        start=args.start,
        end=args.end,
        crop_bottom=crop_bottom,
        pad_bottom=pad_bottom,
        denoise=denoise,
        q=args.q,
        codec=args.codec,
        deint_mode=args.deint_mode,
        map_both_audio=args.map_both_audio,
        log_level=args.log_level,
        assume_yes=args.yes,
        output_suffix=args.output_suffix,
    )


def build_paths(cfg: Config) -> Paths:
    if not cfg.input_file.is_file():
        raise SystemExit(f"Input is not a regular file: {cfg.input_file}")

    input_dir_abs = (cfg.input_file.parent if cfg.input_file.parent != Path("") else Path(".")).resolve()
    dir_str = str(input_dir_abs)
    marker = f"{os.sep}Originals{os.sep}"
    if marker not in dir_str:
        raise SystemExit(f"Input path must be inside a dir under Originals/: {input_dir_abs}")
    rel = dir_str.split(marker, 1)[1]
    rel_dir = Path(rel) if rel else Path()

    out_dir = cfg.access_root / rel_dir
    log_dir = cfg.log_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    path_prefix = str(rel_dir).replace("/", "_").replace(" ", "_").strip("_")
    while "__" in path_prefix:
        path_prefix = path_prefix.replace("__", "_")

    stem = cfg.input_file.stem
    suffix = ""
    output_name = f"{path_prefix}_{stem}{suffix}{cfg.output_suffix}.mp4" if path_prefix else f"{stem}_{suffix}{cfg.output_suffix}.mp4"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).resolve().parent

    return Paths(
        input_file=cfg.input_file,
        input_dir_abs=input_dir_abs,
        stem=stem,
        rel_dir=rel_dir,
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


def get_hqdn3d_args(preset: str) -> str | None:
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
    value = value.replace("\\", "\\\\")
    value = value.replace(":", r"\:")
    value = value.replace(",", r"\,")
    value = value.replace(";", r"\;")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    value = value.replace("=", r"\=")
    value = value.replace("'", r"\'")
    return value


def build_vf(cfg: Config, paths: Paths) -> str:
    filters: list[str] = [f"bwdif=mode={cfg.deint_mode}:parity=auto:deint=all"]

    if cfg.crop_bottom > 0:
        filters.append(f"crop=iw:ih-{cfg.crop_bottom}:0:0")
    if cfg.pad_bottom > 0:
        filters.append(f"pad=iw:ih+{cfg.pad_bottom}:0:0:black")

    hqdn3d = get_hqdn3d_args(cfg.denoise)
    if hqdn3d:
        filters.append(f"hqdn3d={hqdn3d}")

    filters.extend([
        "scale='trunc(ih*dar/2)*2:ih'",
        "setsar=1",
        "setparams=range=limited:color_primaries=smpte170m:color_trc=smpte170m:colorspace=smpte170m",
    ])

    if cfg.format_type == "digital8":
        srt_path = escape_ffmpeg_filter_value(str(paths.srt_file))
        style = (
            "Alignment=3,"
            "MarginV=3,"
            "MarginR=3,"
            "FontName=Helvetica,"
            "Fontsize=12,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,"
            "Outline=0,"
            "Shadow=0"
        )
        filters.append(f"subtitles=filename={srt_path}:force_style='{style}'")

    return ",".join(filters)


def build_ffmpeg_args(cfg: Config, paths: Paths, vf: str, preview: bool) -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", cfg.log_level, "-stats", "-stats_period", "1"]

    if cfg.start:
        args += ["-ss", cfg.start]
    if cfg.end:
        args += ["-to", cfg.end]

    args += ["-i", str(paths.input_file), "-vf", vf, "-map", "0:v:0"]

    if cfg.map_both_audio:
        args += ["-map", "0:a:0?", "-map", "0:a:1?"]
    else:
        args += ["-map", "0:a:0?"]

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

    args += ["-f", "matroska", "-"] if preview else [str(paths.output_file)]
    return args


def shjoin(args: list[str]) -> str:
    return shlex.join(args)


def run_checked(args: list[str], stdout_path: Path | None = None) -> None:
    if stdout_path is None:
        subprocess.run(args, check=True)
    else:
        with stdout_path.open("w", encoding="utf-8", newline="\n") as f:
            subprocess.run(args, check=True, stdout=f)


def extract_first_rdt_yyyymmdd(csv_path: Path) -> str | None:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("rdt") or "").strip()
            if not raw:
                continue
            date_part = raw.split(" ", 1)[0]
            parts = date_part.split("-")
            if len(parts) == 3 and all(parts):
                yyyy, mm, dd = parts
                if len(yyyy) == 4 and len(mm) == 2 and len(dd) == 2:
                    return f"{yyyy}{mm}{dd}"
    return None


def generate_digital8_sidecars(paths: Paths) -> None:
    if not paths.add_play_time_script.exists():
        raise SystemExit(f"Missing script: {paths.add_play_time_script}")
    if not paths.create_srt_script.exists():
        raise SystemExit(f"Missing script: {paths.create_srt_script}")

    print("Generating Digital8 CSV/SRT sidecars...")
    run_checked(["dvrescue", "--csv", str(paths.input_file), "-m", "/dev/null"], stdout_path=paths.csv_raw)
    run_checked(["python3", str(paths.add_play_time_script), str(paths.csv_raw), "-o", str(paths.csv_with_play)])
    run_checked(["python3", str(paths.create_srt_script), str(paths.csv_with_play), "-o", str(paths.srt_file)])

    yyyymmdd = extract_first_rdt_yyyymmdd(paths.csv_with_play)
    if yyyymmdd:
        paths.output_file = paths.output_file.with_name(f"{yyyymmdd}_{paths.output_file.name}")
    else:
        print("Warning: could not find first-frame rdt date; leaving output filename unchanged.")


def print_summary(cfg: Config, paths: Paths, ffmpeg_args: list[str], preview: bool) -> None:
    print(f"Mode: {cfg.mode}")
    print(f"Format: {cfg.format_type}")
    print(f"Input: {paths.input_file}")
    print(f"Output dir: {paths.out_dir}")
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
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            for out in outputs:
                out.write(chunk)
                out.flush()
            if mirror_to_stderr:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
    finally:
        stream.close()


def run_ffmpeg(ffmpeg_args: list[str], log_path: Path, preview_stem: str | None = None) -> int:
    with log_path.open("wb") as log_file:
        if preview_stem is None:
            proc = subprocess.Popen(ffmpeg_args, stderr=subprocess.PIPE, bufsize=0)
            assert proc.stderr is not None
            t = threading.Thread(target=tee_stream, args=(proc.stderr, [log_file], True), daemon=True)
            t.start()
            rc = proc.wait()
            t.join()
            return rc

        ffplay_args = ["ffplay", "-hide_banner", "-window_title", f"{preview_stem} preview", "-"]
        ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        assert ffmpeg_proc.stdout is not None
        assert ffmpeg_proc.stderr is not None

        ffplay_proc = subprocess.Popen(ffplay_args, stdin=ffmpeg_proc.stdout, bufsize=0)
        ffmpeg_proc.stdout.close()

        t = threading.Thread(target=tee_stream, args=(ffmpeg_proc.stderr, [log_file], True), daemon=True)
        t.start()

        ffplay_rc = ffplay_proc.wait()
        ffmpeg_rc = ffmpeg_proc.wait()
        t.join()

        return ffmpeg_rc if ffmpeg_rc != 0 else ffplay_rc


def main() -> int:
    cfg = parse_args()
    paths = build_paths(cfg)

    if cfg.format_type == "digital8":
        generate_digital8_sidecars(paths)

    vf = build_vf(cfg, paths)
    preview = cfg.mode == "preview"
    ffmpeg_args = build_ffmpeg_args(cfg, paths, vf, preview=preview)

    print_summary(cfg, paths, ffmpeg_args, preview=preview)

    if preview:
        return run_ffmpeg(ffmpeg_args, paths.ffmpeg_log_file, preview_stem=paths.stem)

    if not cfg.assume_yes:
        input("Press Enter to start transcode, or Ctrl-C to cancel...")

    write_command_log(cfg, paths, ffmpeg_args)
    rc = run_ffmpeg(ffmpeg_args, paths.ffmpeg_log_file)
    if rc != 0:
        return rc

    print(f"Done: {paths.output_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
