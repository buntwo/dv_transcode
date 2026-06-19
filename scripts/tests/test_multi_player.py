from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import multi_player


class TestMpvClientResponseParsing(unittest.TestCase):
    def test_parse_mpv_response_accepts_multiple_json_lines(self) -> None:
        response = b'{"error":"success"}\n{"event":"playback-restart"}\n'

        self.assertEqual(multi_player.parse_mpv_response(response), {"error": "success"})

    def test_parse_mpv_response_skips_events_before_command_response(self) -> None:
        response = b'{"event":"property-change","name":"time-pos"}\n{"data":false,"error":"success"}\n'

        self.assertEqual(multi_player.parse_mpv_response(response), {"data": False, "error": "success"})

    def test_send_ipc_payload_retries_refused_connections(self) -> None:
        class FlakySocket:
            attempts = 0

            def __enter__(self) -> FlakySocket:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def connect(self, path: str) -> None:
                type(self).attempts += 1
                if type(self).attempts < 3:
                    raise ConnectionRefusedError(61, "Connection refused")

            def sendall(self, payload: bytes) -> None:
                self.payload = payload

            def recv(self, size: int) -> bytes:
                return b'{"error":"success"}\n'

        with patch.object(multi_player.socket, "socket", return_value=FlakySocket()), patch.object(multi_player.time, "sleep") as sleep:
            response = multi_player.send_ipc_payload(Path("player.sock"), b'{"command":["seek",5]}\n')

        self.assertEqual(response, b'{"error":"success"}\n')
        self.assertEqual(FlakySocket.attempts, 3)
        self.assertEqual(sleep.call_count, 2)


class FakeClient:
    def __init__(self, pause: bool = False, time_pos: float | None = None) -> None:
        self.pause = pause
        self.time_pos = time_pos
        self.time_positions: list[float | None] | None = None
        self.commands: list[tuple[object, ...]] = []

    def seek(self, seconds: float) -> None:
        self.commands.append(("seek", seconds))

    def seek_absolute(self, seconds: float) -> None:
        self.commands.append(("seek_absolute", seconds))

    def set_volume(self, volume: int) -> None:
        self.commands.append(("volume", volume))

    def set_title(self, title: str) -> None:
        self.commands.append(("title", title))

    def get_pause(self) -> bool:
        return self.pause

    def get_time_pos(self) -> float | None:
        self.commands.append(("time_pos",))
        if self.time_positions is not None:
            return self.time_positions.pop(0)
        return self.time_pos

    def set_pause(self, pause: bool) -> None:
        self.commands.append(("pause", pause))

    def show_text(self, text: str, duration_ms: int) -> None:
        self.commands.append(("show_text", text, duration_ms))

    def command(self, *parts: object) -> dict[str, object]:
        self.commands.append(("command", *parts))
        return {}


def make_players(count: int) -> list[multi_player.Player]:
    geometries = multi_player.calculate_geometry(count, width=800, height=450, gap=16, x=30, y=40)
    players: list[multi_player.Player] = []
    for index, geometry in enumerate(geometries, start=1):
        player = multi_player.Player(
            index=index,
            video=Path(f"video-{index}.mp4"),
            geometry=geometry,
            socket_path=Path(f"player-{index}.sock"),
        )
        player.client = FakeClient(time_pos=index * 10.0)
        players.append(player)
    return players


class TestMultiPlayerArgs(unittest.TestCase):
    def test_parse_args_accepts_two_to_four_videos(self) -> None:
        args = multi_player.parse_args(["one.mp4", "two.mp4"])
        self.assertEqual(args.videos, [Path("one.mp4"), Path("two.mp4")])
        self.assertEqual(args.width, multi_player.DEFAULT_WIDTH)
        self.assertEqual(args.height, multi_player.DEFAULT_HEIGHT)
        self.assertEqual(args.gap, 0)
        self.assertEqual(args.seek_small, multi_player.DEFAULT_SEEK_SMALL)
        self.assertEqual(args.nudge_small, multi_player.DEFAULT_NUDGE_SMALL)
        self.assertIsNone(args.ss1)
        self.assertIsNone(args.ss2)
        self.assertIsNone(args.ss3)
        self.assertIsNone(args.ss4)

        self.assertEqual(len(multi_player.parse_args(["1.mp4", "2.mp4", "3.mp4"]).videos), 3)
        self.assertEqual(len(multi_player.parse_args(["1.mp4", "2.mp4", "3.mp4", "4.mp4"]).videos), 4)

    def test_parse_args_accepts_numbered_start_seeks(self) -> None:
        args = multi_player.parse_args(["-ss1", "01:02.5", "-ss2", "1:02:03.5", "one.mp4", "two.mp4"])

        self.assertEqual(args.ss1, 62.5)
        self.assertEqual(args.ss2, 3723.5)

    def test_parse_args_rejects_start_seek_for_unloaded_video(self) -> None:
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["-ss3", "10", "one.mp4", "two.mp4"])

    def test_parse_timestamp_rejects_invalid_values(self) -> None:
        for value in ("-1", "nan", "inf", "1:2:3:4", "01:", "1.5:02", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    multi_player.parse_timestamp(value)

    def test_collect_start_times_uses_zero_for_unspecified_videos(self) -> None:
        args = multi_player.parse_args(["-ss2", "12.5", "one.mp4", "two.mp4", "three.mp4"])

        self.assertEqual(multi_player.collect_start_times(args), [0.0, 12.5, 0.0])

    def test_parse_args_accepts_short_position_flags_and_monitor(self) -> None:
        args = multi_player.parse_args(["-x", "10", "-y", "20", "--monitor", "2", "one.mp4", "two.mp4"])

        self.assertEqual(args.x, 10)
        self.assertEqual(args.y, 20)
        self.assertEqual(args.monitor, 2)

    def test_parse_args_rejects_long_position_flags(self) -> None:
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["--x", "10", "one.mp4", "two.mp4"])

    def test_parse_args_rejects_wrong_video_counts(self) -> None:
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["one.mp4"])

        with self.assertRaises(SystemExit):
            multi_player.parse_args(["1.mp4", "2.mp4", "3.mp4", "4.mp4", "5.mp4"])

    def test_parse_args_rejects_non_positive_values(self) -> None:
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["--width", "0", "left.mp4", "right.mp4"])

        with self.assertRaises(SystemExit):
            multi_player.parse_args(["--seek-small", "0", "left.mp4", "right.mp4"])


class TestMultiPlayerGeometry(unittest.TestCase):
    def test_calculate_geometry_places_players_left_to_right_then_top_to_bottom(self) -> None:
        geometry = multi_player.calculate_geometry(count=4, width=800, height=450, gap=16, x=30, y=40)

        self.assertEqual([window.mpv_value() for window in geometry], [
            "800x450+30+40",
            "800x450+846+40",
            "800x450+30+506",
            "800x450+846+506",
        ])


class TestMultiPlayerTitles(unittest.TestCase):
    def test_player_title_marks_selected_and_audio_player(self) -> None:
        players = make_players(4)
        state = multi_player.ControllerState(selected_index=2, audio_index=3)

        self.assertEqual(multi_player.player_title(players[0], state), "      1: video-1.mp4")
        self.assertEqual(multi_player.player_title(players[1], state), "*     2: video-2.mp4")
        self.assertEqual(multi_player.player_title(players[2], state), "  [A] 3: video-3.mp4")

        state = multi_player.ControllerState(selected_index=1, audio_index=1)
        self.assertEqual(multi_player.player_title(players[0], state), "* [A] 1: video-1.mp4")

    def test_select_player_updates_titles_and_flashes_selected_video(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=1, audio_index=1, display_enabled=False)

        multi_player.select_player(players, state, 2)

        self.assertEqual(state.selected_index, 2)
        self.assertIn(("show_text", "SELECTED 2", multi_player.SELECTED_OSD_MS), players[1].client.commands)
        self.assertIn(("title", "*     2: video-2.mp4"), players[1].client.commands)
        self.assertIn(("title", "  [A] 1: video-1.mp4"), players[0].client.commands)

    def test_select_player_refreshes_persistent_osd_without_flash_when_display_enabled(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=1, audio_index=1, display_enabled=True)

        multi_player.select_player(players, state, 2)

        self.assertEqual(state.selected_index, 2)
        self.assertNotIn(("show_text", "SELECTED 2", multi_player.SELECTED_OSD_MS), players[1].client.commands)
        for player in players:
            self.assertIn(
                ("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS),
                player.client.commands,
            )

    def test_select_player_ignores_unloaded_index(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1, audio_index=1)

        multi_player.select_player(players, state, 4)

        self.assertEqual(state.selected_index, 1)
        self.assertEqual(players[0].client.commands, [])
        self.assertEqual(players[1].client.commands, [])


class TestMultiPlayerAudio(unittest.TestCase):
    def test_activate_audio_sets_one_player_audible_and_updates_titles(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=1, audio_index=1, display_enabled=False)

        multi_player.activate_audio(players, state, 3)

        self.assertEqual(state.audio_index, 3)
        self.assertIn(("volume", 0), players[0].client.commands)
        self.assertIn(("volume", 0), players[1].client.commands)
        self.assertIn(("volume", 100), players[2].client.commands)
        self.assertIn(("show_text", "AUDIO 3", multi_player.AUDIO_OSD_MS), players[2].client.commands)
        self.assertIn(("title", "  [A] 3: video-3.mp4"), players[2].client.commands)

    def test_activate_audio_refreshes_persistent_osd_without_flash_when_display_enabled(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=1, audio_index=1, display_enabled=True)

        multi_player.activate_audio(players, state, 3)

        self.assertEqual(state.audio_index, 3)
        self.assertNotIn(("show_text", "AUDIO 3", multi_player.AUDIO_OSD_MS), players[2].client.commands)
        for player in players:
            self.assertIn(
                ("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS),
                player.client.commands,
            )

    def test_activate_audio_ignores_unloaded_index(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1, audio_index=1)

        multi_player.activate_audio(players, state, 4)

        self.assertEqual(state.audio_index, 1)
        self.assertEqual(players[0].client.commands, [])
        self.assertEqual(players[1].client.commands, [])


class TestMultiPlayerKeys(unittest.TestCase):
    def test_normalize_key_maps_requested_shortcuts(self) -> None:
        cases = {
            b" ": "space",
            b"\r": "selected_pause",
            b"\n": "selected_pause",
            b"z": "seek_all_back_xs",
            b"x": "seek_all_forward_xs",
            b"Z": "seek_all_back_s",
            b"X": "seek_all_forward_s",
            b"a": "seek_all_back_m",
            b"s": "seek_all_forward_m",
            b"A": "seek_all_back_l",
            b"S": "seek_all_forward_l",
            b",": "nudge_back_xs",
            b".": "nudge_forward_xs",
            b"<": "nudge_back_s",
            b">": "nudge_forward_s",
            b"k": "nudge_back_m",
            b"l": "nudge_forward_m",
            b"K": "nudge_back_l",
            b"L": "nudge_forward_l",
            b"1": "select_1",
            b"2": "select_2",
            b"3": "select_3",
            b"4": "select_4",
            b"!": "audio_1",
            b"@": "audio_2",
            b"#": "audio_3",
            b"$": "audio_4",
            b"m": "mute",
            b"d": "display",
            b"q": "quit",
        }

        for sequence, expected in cases.items():
            with self.subTest(sequence=sequence):
                self.assertEqual(multi_player.normalize_key(sequence), expected)

    def test_normalize_key_ignores_removed_shortcuts(self) -> None:
        for sequence in (b"\x1b[D", b"\x1b[C", b"\x1b[1;2D", b"\x1b[1;2C", b"c", b"v", b"C", b"V", b"g", b"h", b"0"):
            with self.subTest(sequence=sequence):
                self.assertIsNone(multi_player.normalize_key(sequence))

    def test_seek_keys_are_marked_for_input_flush(self) -> None:
        self.assertEqual(
            multi_player.SEEK_KEYS,
            {
                "seek_all_back_xs",
                "seek_all_forward_xs",
                "seek_all_back_s",
                "seek_all_forward_s",
                "seek_all_back_m",
                "seek_all_forward_m",
                "seek_all_back_l",
                "seek_all_forward_l",
            },
        )


class TestMultiPlayerControls(unittest.TestCase):
    def test_launch_mpv_starts_player_paused(self) -> None:
        geometry = multi_player.WindowGeometry(width=800, height=450, x=30, y=40)

        with patch.object(multi_player.subprocess, "Popen") as popen:
            multi_player.launch_mpv(Path("video.mp4"), "title", geometry, Path("player.sock"), 100)

        cmd = popen.call_args.args[0]
        self.assertIn("--pause", cmd)
        self.assertIn("--ontop", cmd)
        self.assertIn("--osd-align-x=left", cmd)
        self.assertIn("--osd-align-y=top", cmd)
        self.assertIn("--osd-font=monospace", cmd)
        self.assertLess(cmd.index("--pause"), cmd.index("video.mp4"))

    def test_launch_mpv_passes_screen_when_monitor_is_set(self) -> None:
        geometry = multi_player.WindowGeometry(width=800, height=450, x=30, y=40)

        with patch.object(multi_player.subprocess, "Popen") as popen:
            multi_player.launch_mpv(Path("video.mp4"), "title", geometry, Path("player.sock"), 100, screen=1)

        cmd = popen.call_args.args[0]
        self.assertIn("--screen=1", cmd)

    def test_start_all_playback_unpauses_every_player_without_osd(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.start_all_playback(players, state)

        for player in players:
            self.assertIn(("pause", False), player.client.commands)
            self.assertNotIn(("show_text", "PLAYING", multi_player.ACTION_OSD_MS), player.client.commands)
        self.assertEqual(state.last_action, "started")

    def test_preseek_players_moves_requested_players_before_playback(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.preseek_players(players, [0.0, 12.5, 62.0], state)

        self.assertNotIn(("seek_absolute", 0.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 12.5), players[1].client.commands)
        self.assertIn(("seek_absolute", 62.0), players[2].client.commands)
        for player in players:
            self.assertIn(("time_pos",), player.client.commands)
        self.assertEqual(state.last_action, "pre-seeked")

    def test_controller_state_starts_with_persistent_display_enabled(self) -> None:
        self.assertTrue(multi_player.ControllerState().display_enabled)

    def test_wait_for_player_ready_waits_for_time_pos(self) -> None:
        player = make_players(1)[0]
        player.client.time_positions = [None, None, 0.0]

        with patch.object(multi_player.time, "sleep") as sleep:
            multi_player.wait_for_player_ready(player)

        self.assertEqual(player.position_seconds, 0.0)
        self.assertEqual(player.client.commands, [("time_pos",), ("time_pos",), ("time_pos",)])
        self.assertEqual(sleep.call_count, 2)

    def test_wait_for_player_ready_rejects_exited_process(self) -> None:
        class ExitedProcess:
            def poll(self) -> int:
                return 1

        player = make_players(1)[0]
        player.process = ExitedProcess()

        with self.assertRaisesRegex(RuntimeError, "exited before it was ready"):
            multi_player.wait_for_player_ready(player)

    def test_format_status_includes_current_state(self) -> None:
        players = make_players(3)
        players[0].position_seconds = 12.345
        players[1].position_seconds = 3661.2
        players[1].offset_seconds = 0.5
        state = multi_player.ControllerState(selected_index=2, audio_index=3, muted=True, last_action="nudged")

        status = multi_player.format_status(players, state)

        self.assertEqual(
            status,
            "selected 2 | audio 3 | mute on | pos 1:00:00:12.345 2:01:01:01.200 3:--:--:--.--- | "
            "offsets 1:+0.000s 2:+0.500s 3:+0.000s | nudged",
        )

    def test_format_timestamp_handles_none_and_signed_times(self) -> None:
        self.assertEqual(multi_player.format_timestamp(None), "--:--:--.---")
        self.assertEqual(multi_player.format_timestamp(62.345), "00:01:02.345")
        self.assertEqual(multi_player.format_timestamp(-1.2), "-00:00:01.200")

    def test_persistent_osd_text_shows_timestamp_and_total_nudge(self) -> None:
        player = make_players(1)[0]
        player.position_seconds = 62.345
        player.offset_seconds = -0.25
        state = multi_player.ControllerState(selected_index=1, audio_index=2)

        self.assertEqual(multi_player.persistent_osd_text(player, state), "time  00:01:02.345\nnudge -0.250s\n[V]    ")
        self.assertEqual(
            multi_player.format_osd_state(player, state, "delta -0.250s"),
            "time  00:01:02.345\nnudge -0.250s\n[V]    \ndelta -0.250s",
        )

    def test_toggle_display_shows_and_clears_persistent_osd(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState()

        multi_player.toggle_display(players, state)

        self.assertFalse(state.display_enabled)
        self.assertEqual(state.last_action, "ready")
        for player in players:
            self.assertIn(("show_text", "", 1), player.client.commands)

        multi_player.toggle_display(players, state)

        self.assertTrue(state.display_enabled)
        self.assertEqual(state.last_action, "ready")
        for player in players:
            self.assertIn(("time_pos",), player.client.commands)
            self.assertIn(
                ("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS),
                player.client.commands,
            )

    def test_update_persistent_display_waits_for_temporary_osd_to_expire(self) -> None:
        player = make_players(1)[0]
        player.position_seconds = 10.0
        player.osd_block_until = 100.0
        state = multi_player.ControllerState(display_enabled=True)

        multi_player.update_persistent_display(player, state, now=99.0)
        self.assertNotIn(("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS), player.client.commands)

        multi_player.update_persistent_display(player, state, now=100.0)
        self.assertIn(("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS), player.client.commands)
        self.assertEqual(player.osd_block_until, 0.0)

    def test_schedules_due_persistent_display_restores(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(display_enabled=True)
        players[0].osd_block_until = 12.0
        players[1].osd_block_until = 8.0

        self.assertEqual(multi_player.next_persistent_restore_time(players, state), 8.0)
        self.assertAlmostEqual(multi_player.next_loop_timeout(now=7.9, next_status_refresh=20.0, next_osd_restore=8.0), 0.1)
        self.assertAlmostEqual(multi_player.next_loop_timeout(now=7.95, next_status_refresh=20.0, next_osd_restore=8.0), 0.05)

        multi_player.restore_due_persistent_displays(players, state, now=9.0)

        self.assertNotIn(("show_text", multi_player.persistent_osd_text(players[0], state), multi_player.PERSISTENT_OSD_MS), players[0].client.commands)
        self.assertIn(("show_text", multi_player.persistent_osd_text(players[1], state), multi_player.PERSISTENT_OSD_MS), players[1].client.commands)
        self.assertEqual(players[0].osd_block_until, 12.0)
        self.assertEqual(players[1].osd_block_until, 0.0)

    def test_nudge_selected_only_moves_selected_player(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=2, audio_index=1)

        multi_player.nudge_selected(players, state, 0.5)

        self.assertEqual(players[0].offset_seconds, 0.0)
        self.assertEqual(players[1].offset_seconds, 0.5)
        self.assertEqual(players[2].offset_seconds, 0.0)
        self.assertEqual(players[0].client.commands, [])
        self.assertIn(("seek", 0.5), players[1].client.commands)
        self.assertIn(("time_pos",), players[1].client.commands)
        self.assertIn(
            ("show_text", "time  00:00:20.000\nnudge +0.500s\n[V]    \ndelta +0.500s", multi_player.ACTION_OSD_MS),
            players[1].client.commands,
        )
        self.assertEqual(players[2].client.commands, [])
        self.assertEqual(state.last_action, "video 2 offset +0.500s")
        self.assertEqual(players[1].position_seconds, 20.0)

    def test_seek_all_moves_every_player(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.seek_all(players, state, -5.0)

        for player in players:
            self.assertIn(("seek", -5.0), player.client.commands)
            self.assertIn(("time_pos",), player.client.commands)
            expected_state = "[V] [A]" if player.index == 1 else "       "
            self.assertIn(
                (
                    "show_text",
                    f"time  00:00:{player.index * 10:02}.000\nnudge +0.000s\n{expected_state}\nseek  -5.000s",
                    multi_player.ACTION_OSD_MS,
                ),
                player.client.commands,
            )
        self.assertEqual(state.last_action, "seek all -5.000s")

    def test_toggle_mute_cycles_mute_on_every_player(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.toggle_mute(players, state)

        for player in players:
            self.assertIn(("command", "cycle", "mute"), player.client.commands)
            self.assertIn(("show_text", "MUTED", multi_player.ACTION_OSD_MS), player.client.commands)
        self.assertTrue(state.muted)
        self.assertEqual(state.last_action, "muted")

    def test_print_status_rewrites_current_terminal_line(self) -> None:
        with patch.object(multi_player.sys.stdout, "write") as write, patch.object(multi_player.sys.stdout, "flush") as flush:
            multi_player.print_status("seek all +5.000s")

        write.assert_called_once_with("\r\033[Kseek all +5.000s")
        flush.assert_called_once_with()

    def test_flush_pending_input_discards_queued_terminal_input(self) -> None:
        with patch.object(multi_player.termios, "tcflush") as tcflush:
            multi_player.flush_pending_input()

        tcflush.assert_called_once_with(multi_player.sys.stdin, multi_player.termios.TCIFLUSH)


class TestMultiPlayerValidation(unittest.TestCase):
    def test_validate_inputs_rejects_missing_files(self) -> None:
        args = argparse.Namespace(videos=[Path("/missing/left.mp4"), Path("/missing/right.mp4")])

        with self.assertRaisesRegex(ValueError, "video 1 does not exist"):
            multi_player.validate_inputs(args)

    def test_validate_inputs_rejects_missing_mpv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.mp4"
            right = Path(tmp) / "right.mp4"
            left.write_bytes(b"left")
            right.write_bytes(b"right")
            args = argparse.Namespace(videos=[left, right])

            with patch.object(multi_player.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "mpv is required"):
                    multi_player.validate_inputs(args)
