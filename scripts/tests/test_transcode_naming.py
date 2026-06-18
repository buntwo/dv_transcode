import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcode_naming  # noqa: E402


class TestTranscodeNaming(unittest.TestCase):
    def test_direct_child_case(self) -> None:
        self.assertEqual(
            transcode_naming.build_access_output_name("Originals/Set 2/1 2001/out.dv"),
            "Set_2_1_out.mp4",
        )

    def test_deeper_path_case(self) -> None:
        self.assertEqual(
            transcode_naming.build_access_output_name("Originals/Set 1/1 Disney/retakes/out.dv"),
            "Set_1_1_retakes_out.mp4",
        )

    def test_unnumbered_case(self) -> None:
        self.assertEqual(
            transcode_naming.build_access_output_name(
                'Originals/Unnumbered/H 灵灵 3rd Grade "ShoeBeDo" Show/out.dv'
            ),
            "Unnumbered_H_out.mp4",
        )

    def test_single_word_child_directory_is_used_as_prefix(self) -> None:
        self.assertEqual(
            transcode_naming.build_access_output_name("Originals/Set 9/23/out.dv"),
            "Set_9_23_out.mp4",
        )


if __name__ == "__main__":
    unittest.main()
