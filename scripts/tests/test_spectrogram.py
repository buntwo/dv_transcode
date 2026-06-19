from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

import spectrogram


def default_config(duration_seconds: float | None = None) -> spectrogram.SpectrogramConfig:
    return spectrogram.SpectrogramConfig(
        duration_seconds=duration_seconds,
        size="1920x1080",
        font=spectrogram.DEFAULT_FONT,
        cjk_font=spectrogram.DEFAULT_CJK_FONT,
        cjk_font_scale=spectrogram.DEFAULT_CJK_FONT_SCALE,
        cjk_y_offset=spectrogram.DEFAULT_CJK_Y_OFFSET,
        title_point_size=spectrogram.DEFAULT_TITLE_POINT_SIZE,
        title_x=spectrogram.DEFAULT_TITLE_X,
        title_y=spectrogram.DEFAULT_TITLE_Y,
        footer_cover_width=spectrogram.DEFAULT_FOOTER_COVER_WIDTH,
        footer_cover_height=spectrogram.DEFAULT_FOOTER_COVER_HEIGHT,
        postprocess_scale=spectrogram.DEFAULT_POSTPROCESS_SCALE,
    )


class TestSpectrogramArgs(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = spectrogram.parse_args(["video.mp4"])

        self.assertEqual(args.inputs, [Path("video.mp4")])
        self.assertIsNone(args.output)
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.duration)
        self.assertEqual(args.size, spectrogram.DEFAULT_SIZE)

    def test_parse_args_accepts_multiple_inputs(self) -> None:
        args = spectrogram.parse_args(["one.mp4", "two.mp4"])

        self.assertEqual(args.inputs, [Path("one.mp4"), Path("two.mp4")])

    def test_short_o_sets_output_dir(self) -> None:
        args = spectrogram.parse_args(["-o", "/tmp/sheets", "video.mp4"])

        self.assertIsNone(args.output)
        self.assertEqual(args.output_dir, Path("/tmp/sheets"))

    def test_parse_args_rejects_non_positive_duration(self) -> None:
        with self.assertRaises(SystemExit):
            spectrogram.parse_args(["--duration", "0", "video.mp4"])

    def test_validate_args_rejects_output_and_output_dir_together(self) -> None:
        args = argparse.Namespace(
            inputs=[Path(__file__)],
            output=Path("spectrogram.png"),
            output_dir=Path("/tmp"),
        )

        with self.assertRaisesRegex(ValueError, "--output and --output-dir"):
            spectrogram.validate_args(args)

    def test_validate_args_rejects_output_with_multiple_inputs(self) -> None:
        args = argparse.Namespace(
            inputs=[Path(__file__), Path(__file__)],
            output=Path("spectrogram.png"),
            output_dir=None,
        )

        with self.assertRaisesRegex(ValueError, "--output can only be used with one input"):
            spectrogram.validate_args(args)


class TestSpectrogramOutputPaths(unittest.TestCase):
    def test_default_output_path_appends_spectrogram_suffix_to_full_name(self) -> None:
        self.assertEqual(
            spectrogram.default_output_path(Path("/tmp/video.mp4")),
            Path("/tmp/video.mp4.spectrogram.png"),
        )

    def test_resolve_output_path_uses_explicit_output(self) -> None:
        self.assertEqual(
            spectrogram.resolve_output_path(Path("/tmp/video.mp4"), Path("/tmp/custom.png"), None),
            Path("/tmp/custom.png"),
        )

    def test_resolve_output_path_uses_output_dir_with_auto_name(self) -> None:
        self.assertEqual(
            spectrogram.resolve_output_path(Path("/tmp/video.mp4"), None, Path("/spectrograms")),
            Path("/spectrograms/video.mp4.spectrogram.png"),
        )


class TestSpectrogramCommand(unittest.TestCase):
    def test_build_ffmpeg_command_defaults_to_full_input(self) -> None:
        config = default_config()

        cmd = spectrogram.build_ffmpeg_command(Path("in.mp4"), Path("out.png"), config)

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "00:00:00",
                "-i",
                "in.mp4",
                "-filter_complex",
                "[0:a]showspectrumpic=s=1920x1080:mode=separate:legend=1:scale=log:fscale=lin",
                "-frames:v",
                "1",
                "out.png",
            ],
        )

    def test_build_ffmpeg_command_honors_explicit_duration(self) -> None:
        config = default_config(duration_seconds=300.0)

        cmd = spectrogram.build_ffmpeg_command(Path("in.mp4"), Path("out.png"), config)

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "00:00:00",
                "-t",
                "300",
                "-i",
                "in.mp4",
                "-filter_complex",
                "[0:a]showspectrumpic=s=1920x1080:mode=separate:legend=1:scale=log:fscale=lin",
                "-frames:v",
                "1",
                "out.png",
            ],
        )

    def test_format_duration_arg_keeps_fractional_values_when_needed(self) -> None:
        self.assertEqual(spectrogram.format_duration_arg(300.0), "300")
        self.assertEqual(spectrogram.format_duration_arg(12.5), "12.5")

    def test_build_postprocess_command_covers_footer_and_draws_filename(self) -> None:
        config = default_config()

        with patch.object(spectrogram, "identify_image_size", return_value=(2204, 1208)):
            with patch.object(spectrogram, "build_text_annotations", return_value=["TEXT_ARGS"]):
                cmd = spectrogram.build_postprocess_command(
                    "测试 Zoe.mp4  ·  356.86 MB  ·  00:07:22",
                    Path("raw.png"),
                    Path("out.png"),
                    config,
                )

        self.assertIn("rectangle 0,4696 2880,4832", cmd)
        self.assertIn("8816x4832!", cmd)
        self.assertIn("2204x1208!", cmd)
        self.assertIn("TEXT_ARGS", cmd)
        self.assertEqual(cmd[-1], "out.png")


if __name__ == "__main__":
    unittest.main()
