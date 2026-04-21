from pathlib import Path
import logging


def auto_sibling_dir_for_path(
    path: str | Path,
    *,
    originals_dirname: str = "Originals",
    sibling_dirname: str = "Logs",
) -> Path:
    """Construct and create the log directory for a file or directory under Originals/."""

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

    log_dir = base_root / sibling_dirname / rel_under_originals
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
