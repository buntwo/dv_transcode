from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest.mock import call, patch

import contact_sheet
from utils import build_text_annotations
from utils import format_progress
from utils import scaled_point_size
from utils import split_text_runs


class TestContactSheetArgs(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = contact_sheet.parse_args(["video.mp4"])

        self.assertEqual(args.inputs, [Path("video.mp4")])
        self.assertIsNone(args.output)
        self.assertIsNone(args.output_dir)
        self.assertEqual(args.columns, contact_sheet.DEFAULT_COLUMNS)
        self.assertEqual(args.rows, contact_sheet.DEFAULT_ROWS)
        self.assertEqual(args.sheet_width, contact_sheet.DEFAULT_SHEET_WIDTH)
        self.assertEqual(args.jpeg_qscale, contact_sheet.DEFAULT_JPEG_QSCALE)

    def test_parse_args_rejects_invalid_sizes(self) -> None:
        with self.assertRaises(SystemExit):
            contact_sheet.parse_args(["--columns", "0", "video.mp4"])

    def test_parse_args_rejects_invalid_jpeg_qscale(self) -> None:
        with self.assertRaises(SystemExit):
            contact_sheet.parse_args(["--jpeg-qscale", "0", "video.mp4"])

    def test_parse_args_accepts_multiple_inputs(self) -> None:
        args = contact_sheet.parse_args(["one.mp4", "two.mp4"])

        self.assertEqual(args.inputs, [Path("one.mp4"), Path("two.mp4")])

    def test_short_o_sets_output_dir(self) -> None:
        args = contact_sheet.parse_args(["-o", "/tmp/sheets", "video.mp4"])

        self.assertIsNone(args.output)
        self.assertEqual(args.output_dir, Path("/tmp/sheets"))

    def test_validate_args_rejects_output_and_output_dir_together(self) -> None:
        args = argparse.Namespace(
            inputs=[Path(__file__)],
            output=Path("sheet.png"),
            output_dir=Path("/tmp"),
        )

        with self.assertRaisesRegex(ValueError, "--output and --output-dir"):
            contact_sheet.validate_args(args)

    def test_validate_args_rejects_output_with_multiple_inputs(self) -> None:
        args = argparse.Namespace(
            inputs=[Path(__file__), Path(__file__)],
            output=Path("sheet.png"),
            output_dir=None,
        )

        with self.assertRaisesRegex(ValueError, "--output can only be used with one input"):
            contact_sheet.validate_args(args)

    def test_config_from_args_uses_internal_visual_defaults(self) -> None:
        args = contact_sheet.parse_args(["video.mp4"])

        config = contact_sheet.config_from_args(args)

        self.assertEqual(config.font, contact_sheet.DEFAULT_FONT)
        self.assertEqual(config.header_font, contact_sheet.DEFAULT_HEADER_FONT)
        self.assertEqual(config.cjk_font, contact_sheet.DEFAULT_CJK_FONT)
        self.assertEqual(config.cjk_font_scale, contact_sheet.DEFAULT_CJK_FONT_SCALE)
        self.assertEqual(config.cjk_y_offset, contact_sheet.DEFAULT_CJK_Y_OFFSET)
        self.assertEqual(config.header_height, contact_sheet.DEFAULT_HEADER_HEIGHT)
        self.assertEqual(config.margin, contact_sheet.DEFAULT_MARGIN)
        self.assertEqual(config.padding, contact_sheet.DEFAULT_PADDING)
        self.assertEqual(config.header_scale, contact_sheet.DEFAULT_HEADER_SCALE)
        self.assertEqual(config.header_stroke_width, contact_sheet.DEFAULT_HEADER_STROKE_WIDTH)
        self.assertEqual(config.header_title_x, contact_sheet.DEFAULT_HEADER_TITLE_X)
        self.assertEqual(config.header_title_y, contact_sheet.DEFAULT_HEADER_TITLE_Y)
        self.assertEqual(config.header_detail_x, contact_sheet.DEFAULT_HEADER_DETAIL_X)
        self.assertEqual(config.header_detail_y, contact_sheet.DEFAULT_HEADER_DETAIL_Y)


class TestContactSheetOutputPaths(unittest.TestCase):
    def test_default_output_path_appends_contact_sheet_suffix_to_full_name(self) -> None:
        self.assertEqual(
            contact_sheet.default_output_path(Path("/tmp/video.mp4")),
            Path("/tmp/video.mp4.contact_sheet.jpg"),
        )

    def test_resolve_output_path_uses_explicit_output(self) -> None:
        self.assertEqual(
            contact_sheet.resolve_output_path(Path("/tmp/video.mp4"), Path("/tmp/custom.png"), None),
            Path("/tmp/custom.png"),
        )

    def test_resolve_output_path_uses_output_dir_with_auto_name(self) -> None:
        self.assertEqual(
            contact_sheet.resolve_output_path(Path("/tmp/video.mp4"), None, Path("/sheets")),
            Path("/sheets/video.mp4.contact_sheet.jpg"),
        )


class TestContactSheetOutputEncoding(unittest.TestCase):
    def test_append_header_writes_png_directly(self) -> None:
        config = contact_sheet.config_from_args(contact_sheet.parse_args(["video.mp4"]))

        with patch.object(contact_sheet.subprocess, "run") as run:
            contact_sheet.append_header(Path("header.png"), Path("tile.png"), Path("out.png"), config)

        run.assert_called_once_with(
            ["magick", "header.png", "tile.png", "-append", "out.png"],
            check=True,
        )

    def test_append_header_encodes_jpeg_with_configured_qscale(self) -> None:
        config = contact_sheet.config_from_args(contact_sheet.parse_args(["--jpeg-qscale", "5", "video.mp4"]))

        with patch.object(contact_sheet.subprocess, "run") as run:
            contact_sheet.append_header(Path("header.png"), Path("tile.png"), Path("out.jpg"), config)

        self.assertEqual(
            run.mock_calls,
            [
                call(["magick", "header.png", "tile.png", "-append", "contact-sheet-final.png"], check=True),
                call(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        "contact-sheet-final.png",
                        "-frames:v",
                        "1",
                        "-q:v",
                        "5",
                        "out.jpg",
                    ],
                    check=True,
                ),
            ],
        )


class TestContactSheetProgress(unittest.TestCase):
    def test_format_progress_shows_bar_count_and_input(self) -> None:
        self.assertEqual(
            format_progress(2, 4, Path("video.mp4"), width=10),
            "[#####-----] 2/4 video.mp4",
        )

    def test_format_progress_pads_count_to_total_width(self) -> None:
        self.assertEqual(
            format_progress(1, 21, Path("video.mp4"), width=10),
            "[----------] 01/21 video.mp4",
        )


class TestContactSheetMetadata(unittest.TestCase):
    def test_metadata_from_ffprobe_uses_first_video_and_audio_stream(self) -> None:
        data = {
            "format": {"duration": "441.975", "size": "356864954"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 648,
                    "height": 486,
                    "display_aspect_ratio": "4:3",
                    "avg_frame_rate": "60000/1001",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                },
            ],
        }

        metadata = contact_sheet.metadata_from_ffprobe(Path("25 Zoe.mp4"), data)

        self.assertEqual(metadata.filename, "25 Zoe.mp4")
        self.assertEqual(metadata.duration_seconds, 441.975)
        self.assertEqual(metadata.width, 648)
        self.assertEqual(metadata.height, 486)
        self.assertEqual(metadata.display_aspect_ratio, "4:3")
        self.assertAlmostEqual(metadata.frame_rate or 0.0, 59.94, places=2)
        self.assertEqual(metadata.detail_text, "356.86 MB  ·  00:07:22  ·  648x486 (4:3)  ·  59.94fps  ·  hevc  ·  aac 2ch 48000Hz")

    def test_metadata_rejects_missing_video_stream(self) -> None:
        with self.assertRaisesRegex(ValueError, "no video stream"):
            contact_sheet.metadata_from_ffprobe(Path("audio.wav"), {"streams": []})


class TestContactSheetFormatting(unittest.TestCase):
    def test_format_duration_rounds_to_nearest_second(self) -> None:
        self.assertEqual(contact_sheet.format_duration(441.4), "00:07:21")
        self.assertEqual(contact_sheet.format_duration(441.5), "00:07:22")

    def test_format_dimensions_falls_back_to_pixel_ratio(self) -> None:
        self.assertEqual(contact_sheet.format_dimensions(720, 486, None), "720x486 (40:27)")

    def test_format_size_uses_decimal_units(self) -> None:
        self.assertEqual(contact_sheet.format_size(356_864_954), "356.86 MB")
        self.assertEqual(contact_sheet.format_size(5_040_000_000), "5.04 GB")

    def test_calculate_sample_step_leaves_even_gaps_at_start_and_end(self) -> None:
        self.assertAlmostEqual(contact_sheet.calculate_sample_step(441.0, 20), 21.0)

    def test_display_aspect_ratio_prefers_metadata_ratio(self) -> None:
        metadata = contact_sheet.VideoMetadata(
            filename="video.mp4",
            size_bytes=1024,
            duration_seconds=100.0,
            width=720,
            height=486,
            display_aspect_ratio="4:3",
            frame_rate=29.97,
            video_codec="h264",
            audio_codec=None,
            audio_channels=None,
            audio_sample_rate=None,
        )

        self.assertEqual(contact_sheet.display_aspect_ratio(metadata), (4, 3))

    def test_split_text_runs_uses_cjk_font_for_chinese_characters(self) -> None:
        self.assertEqual(
            split_text_runs("Test 测试.mp4", "Helvetica-Neue", "Heiti-SC-Medium"),
            [
                ("Test ", "Helvetica-Neue"),
                ("测试", "Heiti-SC-Medium"),
                (".mp4", "Helvetica-Neue"),
            ],
        )


class TestContactSheetFilter(unittest.TestCase):
    def test_build_frame_filter_scales_frame_and_draws_fixed_timestamp(self) -> None:
        metadata = contact_sheet.VideoMetadata(
            filename="video.mp4",
            size_bytes=1024,
            duration_seconds=100.0,
            width=640,
            height=480,
            display_aspect_ratio="4:3",
            frame_rate=29.97,
            video_codec="h264",
            audio_codec=None,
            audio_channels=None,
            audio_sample_rate=None,
        )
        config = contact_sheet.SheetConfig(
            columns=5,
            rows=4,
            sheet_width=2340,
            header_height=108,
            margin=20,
            padding=5,
            font="Helvetica",
            header_font="Helvetica-Neue",
            cjk_font="Heiti-SC-Medium",
            cjk_font_scale=0.94,
            cjk_y_offset=-2,
            header_scale=4,
            header_stroke_width=0.0,
            header_title_x=36,
            header_title_y=34,
            header_detail_x=36,
            header_detail_y=75,
            point_size=18,
            header_point_size=36,
            detail_point_size=22,
            jpeg_qscale=contact_sheet.DEFAULT_JPEG_QSCALE,
        )

        frame_filter = contact_sheet.build_frame_filter(metadata, config, "00:00:05")

        self.assertIn("scale=456:342:force_original_aspect_ratio=decrease", frame_filter)
        self.assertIn("pad=456:342:(ow-iw)/2:(oh-ih)/2:white", frame_filter)
        self.assertIn("drawtext=font=Helvetica:text='00\\:00\\:05'", frame_filter)
        self.assertIn("boxcolor=black", frame_filter)
        self.assertIn("x=w-tw-6:y=h-th-6", frame_filter)

    def test_calculate_sample_times_leaves_even_gaps_at_start_and_end(self) -> None:
        self.assertEqual(
            contact_sheet.calculate_sample_times(105.0, 4),
            [21.0, 42.0, 63.0, 84.0],
        )

    def test_tile_dimensions_are_derived_from_sheet_width_and_aspect_ratio(self) -> None:
        metadata = contact_sheet.VideoMetadata(
            filename="video.mp4",
            size_bytes=1024,
            duration_seconds=100.0,
            width=640,
            height=480,
            display_aspect_ratio="4:3",
            frame_rate=29.97,
            video_codec="h264",
            audio_codec=None,
            audio_channels=None,
            audio_sample_rate=None,
        )
        config = contact_sheet.SheetConfig(
            columns=5,
            rows=4,
            sheet_width=2340,
            header_height=108,
            margin=20,
            padding=5,
            font="Helvetica",
            header_font="Helvetica-Neue",
            cjk_font="Heiti-SC-Medium",
            cjk_font_scale=0.94,
            cjk_y_offset=-2,
            header_scale=4,
            header_stroke_width=0.0,
            header_title_x=36,
            header_title_y=34,
            header_detail_x=36,
            header_detail_y=75,
            point_size=18,
            header_point_size=36,
            detail_point_size=22,
            jpeg_qscale=contact_sheet.DEFAULT_JPEG_QSCALE,
        )

        self.assertEqual(config.tile_width, 456)
        self.assertEqual(config.tile_height_for(metadata), 342)
        self.assertEqual(config.tile_grid_width(), 2340)

    def test_scaled_point_size_rounds_and_stays_positive(self) -> None:
        self.assertEqual(scaled_point_size(144, 0.94), 135)
        self.assertEqual(scaled_point_size(1, 0.1), 1)

    def test_build_text_annotations_applies_cjk_y_offset(self) -> None:
        annotations = build_text_annotations(
            "A测",
            primary_font="Helvetica-Neue",
            cjk_font="Heiti-SC-Medium",
            cjk_font_scale=1.0,
            point_size=20,
            x=10,
            y=30,
            cjk_y_offset=-4,
        )

        self.assertIn("+10+30", annotations)
        self.assertTrue(any(value.endswith("+26") for value in annotations))


if __name__ == "__main__":
    unittest.main()
