from __future__ import annotations

import io
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest

import verify_access_mirror


def test_discover_videos_preserves_exact_relative_names(tmp_path: Path) -> None:
    batch = tmp_path / "Set 3"
    (batch / "17 Brians").mkdir(parents=True)
    (batch / "17 Brians" / "Tape.MP4").touch()
    (batch / "17 Brians" / ".DS_Store").touch()

    assert set(verify_access_mirror.discover_videos(tmp_path, "Set 3")) == {
        Path("17 Brians/Tape.MP4")
    }


def test_probe_video_duration_uses_exact_timestamp_rational(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.touch()
    payload = {
        "streams": [{"time_base": "1/60000", "duration_ts": 61513452, "duration": "1025.224200"}],
        "format": {"duration": "1025.224200"},
    }
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")

    with patch.object(verify_access_mirror.subprocess, "run", return_value=completed):
        result = verify_access_mirror.probe_video_duration(video)

    assert result.seconds == Fraction(61513452, 60000)
    assert result.source == "stream duration_ts × time_base"


def test_verify_batch_reports_name_and_duration_mismatches(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    reference_root = tmp_path / "reference"
    for root in (local_root, reference_root):
        (root / "Set 1" / "1 Disney").mkdir(parents=True)
        (root / "Set 1" / "1 Disney" / "same.mp4").touch()
    (local_root / "Set 1" / "1 Disney" / "extra.mp4").touch()
    (reference_root / "Set 1" / "1 Disney" / "missing.mp4").touch()

    def fake_probe(path: Path, _ffprobe: str) -> verify_access_mirror.VideoDuration:
        root_duration = 2 if path.is_relative_to(local_root) else 3
        return verify_access_mirror.VideoDuration(Fraction(root_duration), "test")

    output = io.StringIO()
    passed = verify_access_mirror.verify_batch(
        local_root=local_root,
        reference_root=reference_root,
        batch="Set 1",
        quiet=True,
        output=output,
        probe=fake_probe,
    )

    assert not passed
    report = output.getvalue()
    assert "MISSING LOCAL: 1 Disney/missing.mp4" in report
    assert "EXTRA LOCAL:   1 Disney/extra.mp4" in report
    assert "DURATION MISMATCH: 1 Disney/same.mp4" in report


def test_verify_batch_accepts_sub_millisecond_duration_difference(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    reference_root = tmp_path / "reference"
    for root in (local_root, reference_root):
        (root / "Set 3" / "25").mkdir(parents=True)
        (root / "Set 3" / "25" / "same.mp4").touch()

    def fake_probe(path: Path, _ffprobe: str) -> verify_access_mirror.VideoDuration:
        duration = Fraction(52519, 1000)
        if path.is_relative_to(reference_root):
            duration += Fraction(2, 15000)
        return verify_access_mirror.VideoDuration(duration, "test")

    output = io.StringIO()
    passed = verify_access_mirror.verify_batch(
        local_root=local_root,
        reference_root=reference_root,
        batch="Set 3",
        quiet=True,
        output=output,
        probe=fake_probe,
    )

    assert passed
    assert "matched=1, mismatched=0" in output.getvalue()


def test_discover_videos_requires_batch_directory(tmp_path: Path) -> None:
    with pytest.raises(verify_access_mirror.VerificationError, match="batch directory not found"):
        verify_access_mirror.discover_videos(tmp_path, "Set 2")
