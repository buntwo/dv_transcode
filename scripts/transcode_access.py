#!/usr/bin/env python3
"""Generic access-copy transcoding CLI for non-Originals source layouts."""

from __future__ import annotations

import argparse
from pathlib import Path

from transcode_core import Config
from transcode_core import add_common_transcode_args
from transcode_core import config_from_args
from transcode_core import run_transcode_workflow
from transcode_core import validate_common_transcode_args


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
    add_common_transcode_args(parser)
    parser.add_argument("--access-dirname", default="Access")
    parser.add_argument("--logs-dirname", default="Logs")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("input_files", nargs="+")
    args = parser.parse_args()
    validate_common_transcode_args(parser, args)

    cfg = config_from_args(args, layout="access")
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
