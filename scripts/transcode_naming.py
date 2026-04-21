from __future__ import annotations

from pathlib import Path


def normalize_filename_part(value: str) -> str:
    normalized = value.replace("/", "_").replace(" ", "_").strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def path_parts_under_originals(path: str | Path, originals_dirname: str = "Originals") -> tuple[str, ...]:
    parts = Path(path).parts
    matches = [i for i, part in enumerate(parts) if part == originals_dirname]
    if not matches:
        raise ValueError(f"Path must be inside {originals_dirname}/: {path}")

    originals_idx = matches[-1]
    rel_parts = parts[originals_idx + 1 :]
    if len(rel_parts) < 3:
        raise ValueError(
            f"Path must include {originals_dirname}/<set>/<child>/<file>: {path}"
        )
    return rel_parts


def shortened_prefix_parts_from_rel_parts(rel_parts: tuple[str, ...] | list[str]) -> list[str]:
    if len(rel_parts) < 2:
        raise ValueError("Path must include at least <set>/<child>")

    set_name = rel_parts[0]
    child_name = rel_parts[1]
    deeper_dirs = rel_parts[2:]

    child_bits = child_name.split(" ", 1)
    if len(child_bits) == 2 and child_bits[0] and child_bits[1]:
        child_prefix = child_bits[0]
    else:
        child_prefix = child_name

    prefix_parts = [normalize_filename_part(set_name), normalize_filename_part(child_prefix)]
    prefix_parts.extend(normalize_filename_part(part) for part in deeper_dirs)
    return [part for part in prefix_parts if part]


def shortened_prefix_parts(path: str | Path, originals_dirname: str = "Originals") -> list[str]:
    rel_parts = path_parts_under_originals(path, originals_dirname)
    return shortened_prefix_parts_from_rel_parts(rel_parts[:-1])


def build_access_output_name(
    input_path: str | Path,
    *,
    originals_dirname: str = "Originals",
    output_suffix: str = "",
) -> str:
    stem = Path(input_path).stem
    prefix = "_".join(shortened_prefix_parts(input_path, originals_dirname))
    return f"{prefix}_{stem}{output_suffix}.mp4"


def build_legacy_access_output_name(
    input_path: str | Path,
    *,
    originals_dirname: str = "Originals",
    output_suffix: str = "",
) -> str:
    rel_parts = path_parts_under_originals(input_path, originals_dirname)
    input_dir = Path(*rel_parts[:-1])
    path_prefix = normalize_filename_part(str(input_dir))
    stem = Path(input_path).stem
    return f"{path_prefix}_{stem}{output_suffix}.mp4" if path_prefix else f"{stem}{output_suffix}.mp4"


def build_access_output_name_from_rel_dir(
    rel_dir: str | Path,
    *,
    stem: str,
    output_suffix: str = "",
) -> str:
    rel_parts = Path(rel_dir).parts
    prefix = "_".join(shortened_prefix_parts_from_rel_parts(rel_parts))
    return f"{prefix}_{stem}{output_suffix}.mp4"


def build_legacy_access_output_name_from_rel_dir(
    rel_dir: str | Path,
    *,
    stem: str,
    output_suffix: str = "",
) -> str:
    path_prefix = normalize_filename_part(str(Path(rel_dir)))
    return f"{path_prefix}_{stem}{output_suffix}.mp4" if path_prefix else f"{stem}{output_suffix}.mp4"
