#!/usr/bin/env python3
"""Archive-layout DV transcoding CLI."""

from __future__ import annotations

import sys

import transcode_core as _core


def parse_args():
    """Parse archive-layout command-line arguments."""
    return _core.parse_archive_args()


def main() -> int:
    """Run the archive-layout transcode workflow."""
    cfg, input_files = _core.parse_args()
    return _core.run_transcode_workflow(cfg, input_files)


_core.parse_args = parse_args
_core.main = main
sys.modules[__name__] = _core


if __name__ == "__main__":
    try:
        raise SystemExit(_core.main())
    except KeyboardInterrupt:
        raise SystemExit(130)
