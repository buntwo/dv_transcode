#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

exec uv --project "$REPO_ROOT" run "$REPO_ROOT/vhs_workflow/vhs_color_split_audio.py" normalize-audio "$@"
