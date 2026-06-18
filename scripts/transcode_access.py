#!/usr/bin/env python3
"""Generic access-copy transcoding CLI for non-Originals source layouts."""

from __future__ import annotations

import argparse
from pathlib import Path

from transcode_core import DEFAULT_VALIDATE_DURATION_TOLERANCE
from transcode_core import Config
from transcode_core import default_mask_bottom
from transcode_core import default_mask_top
from transcode_core import default_denoise
from transcode_core import run_transcode_workflow


def parse_args() -> tuple[Config, list[Path]]:
    """Parse generic access-copy command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Transcode source media into access MP4s without requiring an Originals/ archive layout.",
        epilog=(
            "Examples:\n"
            "  transcode_access.py --format vhs masters/tape/08.mkv\n"
            "  transcode_access.py --mode validate-duration --format vhs masters/tape/08.mkv\n"
            "  transcode_access.py --format vhs --source-root masters masters/tape/08.mkv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["transcode", "preview", "validate-duration"], default="transcode")
    parser.add_argument("--format", dest="format_type", choices=["video8", "digital8", "vhs"], required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mask-top", type=int)
    parser.add_argument("--mask-bottom", type=int)
    parser.add_argument("--denoise", choices=["off", "verylight", "light", "medium", "strong"])
    parser.add_argument("--q", type=int, default=70)
    parser.add_argument("--codec", choices=["h264", "hevc"], default="hevc")
    parser.add_argument("--deint-mode", choices=["send_frame", "send_field"], default="send_field")
    parser.add_argument("--map-both-audio", action="store_true")
    parser.add_argument("--log-level", choices=["quiet", "error", "warning", "info"], default="warning")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-validate-duration", action="store_true")
    parser.add_argument("--validate-duration-tolerance", type=float, default=DEFAULT_VALIDATE_DURATION_TOLERANCE)
    parser.add_argument("--vhs-notch", choices=["auto", "ntsc", "pal", "off"], default="auto")
    parser.add_argument("--audio-channel", choices=["keep", "left", "right"], default="keep")
    parser.add_argument("--output-suffix", default="")
    parser.add_argument("--access-dirname", default="Access")
    parser.add_argument("--logs-dirname", default="Logs")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("input_files", nargs="+")
    args = parser.parse_args()

    mask_top = args.mask_top if args.mask_top is not None else default_mask_top(args.format_type)
    mask_bottom = args.mask_bottom if args.mask_bottom is not None else default_mask_bottom(args.format_type)
    denoise = args.denoise if args.denoise is not None else default_denoise(args.format_type)

    cfg = Config(
        mode=args.mode,
        validate_duration=not args.no_validate_duration,
        validate_duration_tolerance=args.validate_duration_tolerance,
        format_type=args.format_type,
        start=args.start,
        end=args.end,
        mask_top=mask_top,
        mask_bottom=mask_bottom,
        denoise=denoise,
        q=args.q,
        codec=args.codec,
        deint_mode=args.deint_mode,
        map_both_audio=args.map_both_audio,
        log_level=args.log_level,
        assume_yes=args.yes,
        output_suffix=args.output_suffix,
        originals_dirname="Originals",
        access_dirname=args.access_dirname,
        logs_dirname=args.logs_dirname,
        vhs_notch=args.vhs_notch,
        audio_channel=args.audio_channel,
        layout="access",
        source_root=args.source_root,
        output_dir=args.output_dir,
        log_dir=args.log_dir,
    )
    return cfg, [Path(p) for p in args.input_files]


def main() -> int:
    """Run the generic access-copy workflow."""
    cfg, input_files = parse_args()
    return run_transcode_workflow(cfg, input_files)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
