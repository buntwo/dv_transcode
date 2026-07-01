from __future__ import annotations

import logging
import subprocess
import unicodedata
from pathlib import Path


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def format_progress(current: int, total: int, label: str | Path, width: int = 20) -> str:
    """Format a simple file-level progress bar."""

    filled = round(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    count_width = len(str(total))
    return f"[{bar}] {current:0{count_width}d}/{total:0{count_width}d} {label}"


def needs_cjk_font(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def split_text_runs(text: str, primary_font: str, cjk_font: str) -> list[tuple[str, str]]:
    if not text:
        return []
    runs: list[tuple[str, str]] = []
    current_font = cjk_font if needs_cjk_font(text[0]) else primary_font
    current_chars = [text[0]]
    for char in text[1:]:
        font = cjk_font if needs_cjk_font(char) else primary_font
        if font == current_font:
            current_chars.append(char)
        else:
            runs.append(("".join(current_chars), current_font))
            current_chars = [char]
            current_font = font
    runs.append(("".join(current_chars), current_font))
    return runs


def measure_text_width(text: str, font: str, point_size: int) -> int:
    if not text:
        return 0
    cmd = [
        "magick",
        "-background",
        "none",
        "-fill",
        "black",
        "-font",
        font,
        "-pointsize",
        str(point_size),
        f"label:{text}",
        "-format",
        "%w",
        "info:",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return int(result.stdout.strip())


def scaled_point_size(point_size: int, scale: float) -> int:
    return max(1, round(point_size * scale))


def build_text_annotations(
    text: str,
    *,
    primary_font: str,
    cjk_font: str,
    cjk_font_scale: float,
    point_size: int,
    x: int,
    y: int,
    cjk_y_offset: int = 0,
) -> list[str]:
    args: list[str] = []
    current_x = x
    for run_text, run_font in split_text_runs(text, primary_font, cjk_font):
        is_cjk_run = run_font == cjk_font
        run_point_size = scaled_point_size(point_size, cjk_font_scale) if is_cjk_run else point_size
        run_y = y + cjk_y_offset if is_cjk_run else y
        args.extend(["-font", run_font, "-pointsize", str(run_point_size), "-annotate", f"+{current_x}+{run_y}", run_text])
        current_x += measure_text_width(run_text, run_font, run_point_size)
    return args


def sibling_dir_for_path(
    path: str | Path,
    *,
    originals_dirname: str = "Originals",
    sibling_dirname: str = "Logs",
) -> Path:
    """Construct a sibling directory path for a file or directory under Originals/."""

    path = Path(path)

    # If the path exists and is a file, exclude the filename.
    # Otherwise, treat it as a directory path.
    parts = path.parts[:-1] if path.exists() and path.is_file() else path.parts

    matches = [i for i, part in enumerate(parts) if part == originals_dirname]
    if not matches:
        raise ValueError(
            f"Path must contain a directory named {originals_dirname!r}: {path}"
        )

    if len(matches) > 1:
        logging.warning(
            "Path contains multiple %r components; using the last one: %s",
            originals_dirname,
            path,
        )

    originals_idx = matches[-1]
    rel_under_originals = Path(*parts[originals_idx + 1 :])
    base_root = Path(".") if originals_idx == 0 else Path(*parts[:originals_idx])

    return base_root / sibling_dirname / rel_under_originals


def auto_sibling_dir_for_path(
    path: str | Path,
    *,
    originals_dirname: str = "Originals",
    sibling_dirname: str = "Logs",
) -> Path:
    """Construct and create a sibling directory for a file or directory under Originals/."""

    sibling_dir = sibling_dir_for_path(
        path,
        originals_dirname=originals_dirname,
        sibling_dirname=sibling_dirname,
    )
    sibling_dir.mkdir(parents=True, exist_ok=True)
    return sibling_dir
