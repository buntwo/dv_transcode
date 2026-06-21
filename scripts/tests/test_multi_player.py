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
    def __init__(
        self,
        pause: bool = False,
        time_pos: float | None = None,
        paused_for_cache: bool = False,
        frame_rate: float | None = None,
    ) -> None:
        self.pause = pause
        self.time_pos = time_pos
        self.paused_for_cache = paused_for_cache
        self.frame_rate = frame_rate
        self.time_positions: list[float | None] | None = None
        self.commands: list[tuple[object, ...]] = []

    def seek(self, seconds: float) -> None:
        self.commands.append(("seek", seconds))

    def seek_absolute(self, seconds: float) -> None:
        self.commands.append(("seek_absolute", seconds))

    def set_volume(self, volume: int) -> None:
        self.commands.append(("volume", volume))

    def set_speed(self, speed: float) -> None:
        self.commands.append(("speed", speed))

    def set_title(self, title: str) -> None:
        self.commands.append(("title", title))

    def set_osd_font_size(self, size: int) -> None:
        self.commands.append(("osd_font_size", size))

    def get_pause(self) -> bool:
        return self.pause

    def get_paused_for_cache(self) -> bool:
        self.commands.append(("paused_for_cache",))
        return self.paused_for_cache

    def get_time_pos(self) -> float | None:
        self.commands.append(("time_pos",))
        if self.time_positions is not None:
            return self.time_positions.pop(0)
        return self.time_pos

    def get_frame_rate(self) -> float | None:
        self.commands.append(("frame_rate",))
        return self.frame_rate

    def set_pause(self, pause: bool) -> None:
        self.pause = pause
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
        self.assertEqual(args.x, multi_player.DEFAULT_X)
        self.assertEqual(args.y, multi_player.DEFAULT_Y)
        self.assertEqual(args.seek_medium, multi_player.DEFAULT_SEEK_MEDIUM)
        self.assertEqual(args.seek_small, multi_player.DEFAULT_SEEK_SMALL)
        self.assertEqual(args.nudge_small, multi_player.DEFAULT_NUDGE_SMALL)
        self.assertIsNone(args.ss)
        self.assertIsNone(args.ss1)
        self.assertIsNone(args.ss2)
        self.assertIsNone(args.ss3)
        self.assertIsNone(args.ss4)
        self.assertIsNone(args.vol1)
        self.assertIsNone(args.vol2)
        self.assertIsNone(args.vol3)
        self.assertIsNone(args.vol4)

        self.assertEqual(len(multi_player.parse_args(["1.mp4", "2.mp4", "3.mp4"]).videos), 3)
        self.assertEqual(len(multi_player.parse_args(["1.mp4", "2.mp4", "3.mp4", "4.mp4"]).videos), 4)

    def test_parse_args_accepts_numbered_start_seeks(self) -> None:
        args = multi_player.parse_args(["-ss1", "01:02.5", "-ss2", "1:02:03.5", "one.mp4", "two.mp4"])

        self.assertEqual(args.ss1, 62.5)
        self.assertEqual(args.ss2, 3723.5)

    def test_parse_args_rejects_start_seek_for_unloaded_video(self) -> None:
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["-ss3", "10", "one.mp4", "two.mp4"])

    def test_parse_args_accepts_and_rejects_numbered_volumes(self) -> None:
        args = multi_player.parse_args(["--vol1", "150", "--vol2", "0", "one.mp4", "two.mp4"])

        self.assertEqual(args.vol1, 150)
        self.assertEqual(args.vol2, 0)

        with self.assertRaises(SystemExit):
            multi_player.parse_args(["--vol3", "100", "one.mp4", "two.mp4"])
        with self.assertRaises(SystemExit):
            multi_player.parse_args(["--vol1", "201", "one.mp4", "two.mp4"])

    def test_parse_timestamp_rejects_invalid_values(self) -> None:
        for value in ("-1", "nan", "inf", "1:2:3:4", "01:", "1.5:02", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    multi_player.parse_timestamp(value)

    def test_collect_start_times_uses_zero_for_unspecified_videos(self) -> None:
        args = multi_player.parse_args(["-ss2", "12.5", "one.mp4", "two.mp4", "three.mp4"])

        self.assertEqual(multi_player.collect_start_times(args), [0.0, 12.5, 0.0])

    def test_collect_start_times_uses_global_start_seek_with_numbered_overrides(self) -> None:
        args = multi_player.parse_args(["-ss", "5", "-ss2", "12.5", "one.mp4", "two.mp4", "three.mp4"])

        self.assertEqual(multi_player.collect_start_times(args), [5.0, 12.5, 5.0])

    def test_collect_volumes_uses_default_with_numbered_overrides(self) -> None:
        args = multi_player.parse_args(["--vol2", "150", "one.mp4", "two.mp4", "three.mp4"])

        self.assertEqual(multi_player.collect_volumes(args), [100, 150, 100])

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
        players[2].volume = 135

        multi_player.activate_audio(players, state, 3)

        self.assertEqual(state.audio_index, 3)
        self.assertIn(("volume", 0), players[0].client.commands)
        self.assertIn(("volume", 0), players[1].client.commands)
        self.assertIn(("volume", 135), players[2].client.commands)
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

    def test_change_audio_volume_updates_audio_player_even_when_different_video_is_selected(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=2, audio_index=1, display_enabled=False)

        multi_player.change_audio_volume(players, state, 5)

        self.assertEqual(players[0].volume, 105)
        self.assertEqual(players[1].volume, 100)
        self.assertIn(("volume", 105), players[0].client.commands)
        self.assertIn(("time_pos",), players[0].client.commands)
        self.assertIn(
            (
                "show_text",
                "time  00:00:10.000\nnudge +0.000s\n    [A]\nvol +5 -> 105",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )
        self.assertEqual(state.last_action, "video 1 volume 105")

    def test_change_audio_volume_ignores_unloaded_audio_index(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1, audio_index=3, display_enabled=False)

        multi_player.change_audio_volume(players, state, 20)

        self.assertEqual(players[0].client.commands, [])
        self.assertEqual(players[1].client.commands, [])
        self.assertEqual(state.last_action, "video 3 is not loaded")

    def test_change_audio_volume_clamps_and_resets(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1, audio_index=1, display_enabled=False)
        players[0].volume = 198

        multi_player.change_audio_volume(players, state, 20)
        self.assertEqual(players[0].volume, multi_player.MAX_VOLUME)
        self.assertIn(("volume", multi_player.MAX_VOLUME), players[0].client.commands)
        self.assertIn(
            (
                "show_text",
                "time  00:00:10.000\nnudge +0.000s\n[V] [A]\nvol +2 -> 200",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )

        multi_player.change_audio_volume(players, state, None)
        self.assertEqual(players[0].volume, multi_player.DEFAULT_VOLUME)
        self.assertIn(("volume", multi_player.DEFAULT_VOLUME), players[0].client.commands)
        self.assertIn(
            (
                "show_text",
                "time  00:00:10.000\nnudge +0.000s\n[V] [A]\nvol -100 -> 100",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )

        players[0].volume = 2
        multi_player.change_audio_volume(players, state, -20)
        self.assertEqual(players[0].volume, 0)

    def test_change_speed_updates_every_player_and_flashes_osd(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=2, audio_index=1)

        multi_player.change_speed(players, state, 0.1)

        self.assertAlmostEqual(state.speed, 1.1)
        for player in players:
            self.assertIn(("speed", 1.1), player.client.commands)
            self.assertIn(("time_pos",), player.client.commands)
        self.assertIn(
            (
                "show_text",
                "time  00:00:10.000\nnudge +0.000s\n    [A]\nspeed +0.10 -> 1.10x",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )
        self.assertIn(
            (
                "show_text",
                "time  00:00:20.000\nnudge +0.000s\n[V]    \nspeed +0.10 -> 1.10x",
                multi_player.ACTION_OSD_MS,
            ),
            players[1].client.commands,
        )
        self.assertEqual(state.last_action, "speed 1.10x")

    def test_change_speed_clamps_and_resets(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(speed=3.9)

        multi_player.change_speed(players, state, 0.25)
        self.assertEqual(state.speed, multi_player.MAX_SPEED)
        for player in players:
            self.assertIn(("speed", multi_player.MAX_SPEED), player.client.commands)

        multi_player.change_speed(players, state, None)
        self.assertEqual(state.speed, multi_player.DEFAULT_SPEED)
        for player in players:
            self.assertIn(("speed", multi_player.DEFAULT_SPEED), player.client.commands)

        state.speed = 0.2
        multi_player.change_speed(players, state, -0.25)
        self.assertEqual(state.speed, multi_player.MIN_SPEED)


class TestMultiPlayerKeys(unittest.TestCase):
    def test_normalize_key_maps_requested_shortcuts(self) -> None:
        cases = {
            b" ": "space",
            b"\r": "selected_pause",
            b"\n": "selected_pause",
            b"\x1a": "seek_all_back_xs",
            b"\x18": "seek_all_forward_xs",
            b"z": "seek_all_back_s",
            b"x": "seek_all_forward_s",
            b"Z": "seek_all_back_m",
            b"X": "seek_all_forward_m",
            b"a": "seek_all_back_l",
            b"s": "seek_all_forward_l",
            b"A": "seek_all_back_xl",
            b"S": "seek_all_forward_xl",
            b",": "nudge_back_xs",
            b".": "nudge_forward_xs",
            b"<": "nudge_back_s",
            b">": "nudge_forward_s",
            b"k": "nudge_back_m",
            b"l": "nudge_forward_m",
            b"K": "nudge_back_l",
            b"L": "nudge_forward_l",
            b"[": "volume_down_s",
            b"]": "volume_up_s",
            b"{": "volume_down_l",
            b"}": "volume_up_l",
            b"\\": "volume_reset",
            b"y": "speed_down_s",
            b"u": "speed_up_s",
            b"Y": "speed_down_l",
            b"U": "speed_up_l",
            b"i": "speed_reset",
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
            b"t": "sync_to_selected_time",
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
                "seek_all_back_xl",
                "seek_all_forward_xl",
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
        self.assertIn(f"--volume-max={multi_player.MAX_VOLUME}", cmd)
        self.assertIn(f"--osd-font={multi_player.OSD_FONT}", cmd)
        self.assertIn(f"--osd-font-size={multi_player.DEFAULT_OSD_FONT_SIZE}", cmd)
        self.assertLess(cmd.index("--pause"), cmd.index("video.mp4"))

    def test_launch_mpv_passes_screen_when_monitor_is_set(self) -> None:
        geometry = multi_player.WindowGeometry(width=800, height=450, x=30, y=40)

        with patch.object(multi_player.subprocess, "Popen") as popen:
            multi_player.launch_mpv(Path("video.mp4"), "title", geometry, Path("player.sock"), 100, screen=1)

        cmd = popen.call_args.args[0]
        self.assertIn("--screen=1", cmd)

    def test_restore_terminal_focus_activates_macos_bundle_id(self) -> None:
        with patch.object(multi_player.sys, "platform", "darwin"), patch.object(multi_player.subprocess, "run") as run:
            multi_player.restore_terminal_focus("io.alacritty")

        run.assert_called_once_with(
            ["osascript", "-e", 'tell application id "io.alacritty" to activate'],
            check=False,
            stdout=multi_player.subprocess.DEVNULL,
            stderr=multi_player.subprocess.DEVNULL,
        )

    def test_restore_terminal_focus_skips_without_macos_or_bundle_id(self) -> None:
        with patch.object(multi_player.sys, "platform", "linux"), patch.object(multi_player.subprocess, "run") as run:
            multi_player.restore_terminal_focus("io.alacritty")

        run.assert_not_called()

        with (
            patch.object(multi_player.sys, "platform", "darwin"),
            patch.dict(multi_player.os.environ, {}, clear=True),
            patch.object(multi_player.subprocess, "run") as run,
        ):
            multi_player.restore_terminal_focus()

        run.assert_not_called()

    def test_start_all_playback_unpauses_every_player_without_osd(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.start_all_playback(players, state)

        for player in players:
            self.assertIn(("pause", False), player.client.commands)
            self.assertNotIn(
                ("show_text", multi_player.play_pause_osd_text(False), multi_player.ACTION_OSD_MS),
                player.client.commands,
            )
        self.assertEqual(state.last_action, "started")

    def test_set_all_pause_refreshes_positions_for_restored_persistent_osd(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState()
        for player in players:
            player.position_seconds = 0.0

        multi_player.set_all_pause(players, state)

        for player in players:
            self.assertEqual(player.position_seconds, player.index * 10.0)
            self.assertIn(("time_pos",), player.client.commands)
            self.assertIn(("osd_font_size", multi_player.PLAY_PAUSE_OSD_FONT_SIZE), player.client.commands)
            self.assertIn(
                ("show_text", multi_player.play_pause_osd_text(True), multi_player.ACTION_OSD_MS),
                player.client.commands,
            )
            self.assertNotIn(
                ("show_text", multi_player.persistent_osd_text(player, state), multi_player.ACTION_OSD_MS),
                player.client.commands,
            )

    def test_toggle_selected_pause_refreshes_position_for_restored_persistent_osd(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=2)
        players[1].position_seconds = 0.0

        multi_player.toggle_selected_pause(players, state)

        self.assertEqual(players[1].position_seconds, 20.0)
        self.assertIn(("time_pos",), players[1].client.commands)
        self.assertIn(("osd_font_size", multi_player.PLAY_PAUSE_OSD_FONT_SIZE), players[1].client.commands)
        self.assertIn(
            ("show_text", multi_player.play_pause_osd_text(True), multi_player.ACTION_OSD_MS),
            players[1].client.commands,
        )
        self.assertNotIn(
            ("show_text", multi_player.persistent_osd_text(players[1], state), multi_player.ACTION_OSD_MS),
            players[1].client.commands,
        )

    def test_preseek_players_moves_requested_players_before_playback(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.preseek_players(players, [0.0, 12.5, 62.0], state)

        self.assertEqual(players[0].start_seconds, 0.0)
        self.assertEqual(players[1].start_seconds, 12.5)
        self.assertEqual(players[2].start_seconds, 62.0)
        self.assertNotIn(("seek_absolute", 0.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 12.5), players[1].client.commands)
        self.assertIn(("seek_absolute", 62.0), players[2].client.commands)
        for player in players:
            self.assertIn(("time_pos",), player.client.commands)
        self.assertEqual(state.last_action, "pre-seeked")

    def test_pause_all_if_any_player_is_buffering_pauses_every_player(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()
        players[1].client.paused_for_cache = True

        self.assertTrue(multi_player.pause_all_if_any_player_is_buffering(players, state))

        for player in players:
            self.assertIn(("pause", True), player.client.commands)
            self.assertIn(("time_pos",), player.client.commands)
            self.assertIn(
                ("show_text", f"{multi_player.PAUSE_OSD_TEXT} video 2 buffering", multi_player.ACTION_OSD_MS),
                player.client.commands,
            )
        self.assertEqual(state.cache_pause_index, 2)
        self.assertTrue(state.auto_paused_for_cache)
        self.assertEqual(state.last_action, "paused: video 2 buffering")

    def test_pause_all_if_any_player_is_buffering_debounces_same_player(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(cache_pause_index=1)
        players[0].client.paused_for_cache = True

        self.assertTrue(multi_player.pause_all_if_any_player_is_buffering(players, state))

        for player in players:
            self.assertNotIn(("pause", True), player.client.commands)
        self.assertEqual(state.last_action, "ready")

    def test_pause_all_if_any_player_is_buffering_clears_cache_pause_state(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(cache_pause_index=1)

        self.assertFalse(multi_player.pause_all_if_any_player_is_buffering(players, state))

        self.assertIsNone(state.cache_pause_index)

    def test_pause_all_if_any_player_is_buffering_resumes_after_auto_pause(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(cache_pause_index=1, auto_paused_for_cache=True)

        self.assertTrue(multi_player.pause_all_if_any_player_is_buffering(players, state))

        for player in players:
            self.assertIn(("pause", False), player.client.commands)
            self.assertIn(("time_pos",), player.client.commands)
            self.assertIn(
                ("show_text", multi_player.play_pause_osd_text(False), multi_player.ACTION_OSD_MS),
                player.client.commands,
            )
        self.assertIsNone(state.cache_pause_index)
        self.assertFalse(state.auto_paused_for_cache)
        self.assertEqual(state.last_action, "resumed after buffering")

    def test_manual_pause_cancels_buffering_auto_resume(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(cache_pause_index=1, auto_paused_for_cache=True)

        multi_player.set_all_pause(players, state)

        self.assertIsNone(state.cache_pause_index)
        self.assertFalse(state.auto_paused_for_cache)

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

    def test_format_seconds_argument_trims_unneeded_zeroes(self) -> None:
        self.assertEqual(multi_player.format_seconds_argument(62.0), "62")
        self.assertEqual(multi_player.format_seconds_argument(62.5), "62.5")
        self.assertEqual(multi_player.format_seconds_argument(62.3456), "62.346")

    def test_format_copyable_positions_includes_start_flags_and_quoted_filenames(self) -> None:
        players = make_players(3)
        players[0].video = Path("Access/one.mp4")
        players[1].video = Path("Access/two words.mp4")
        players[2].video = Path("Access/three's.mp4")
        players[0].position_seconds = 10.0
        players[1].position_seconds = 20.5
        players[2].position_seconds = None
        players[2].start_seconds = 30.25
        players[0].volume = 100
        players[1].volume = 135
        players[2].volume = 0

        self.assertEqual(
            multi_player.format_copyable_positions(players),
            "-ss1 10 -ss2 20.5 -ss3 30.25 --vol1 100 --vol2 135 --vol3 0 "
            "Access/one.mp4 'Access/two words.mp4' 'Access/three'\"'\"'s.mp4'",
        )

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
        player.osd_font_size = multi_player.PLAY_PAUSE_OSD_FONT_SIZE
        state = multi_player.ControllerState(display_enabled=True)

        multi_player.update_persistent_display(player, state, now=99.0)
        self.assertNotIn(("show_text", multi_player.persistent_osd_text(player, state), multi_player.PERSISTENT_OSD_MS), player.client.commands)

        multi_player.update_persistent_display(player, state, now=100.0)
        self.assertIn(("osd_font_size", multi_player.DEFAULT_OSD_FONT_SIZE), player.client.commands)
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
        self.assertIn(("seek_absolute", 20.5), players[1].client.commands)
        self.assertIn(("time_pos",), players[1].client.commands)
        self.assertIn(
            ("show_text", "time  00:00:20.500\nnudge +0.500s\n[V]    \ndelta +0.500s", multi_player.ACTION_OSD_MS),
            players[1].client.commands,
        )
        self.assertEqual(players[2].client.commands, [])
        self.assertEqual(state.last_action, "video 2 offset +0.500s")
        self.assertEqual(players[1].position_seconds, 20.5)

    def test_nudge_selected_clips_at_zero_and_records_actual_delta(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].offset_seconds = -1.0
        players[0].client.time_pos = 2.0

        multi_player.nudge_selected(players, state, -5.0)

        self.assertIn(("seek_absolute", 0.0), players[0].client.commands)
        self.assertEqual(players[0].offset_seconds, -3.0)
        self.assertIn(
            (
                "show_text",
                "time  00:00:00.000\nnudge -3.000s\n[V] [A]\ndelta -2.000s",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )
        self.assertEqual(state.last_action, "video 1 offset -3.000s")

    def test_nudge_selected_handles_unavailable_timestamp(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].client.time_pos = None

        multi_player.nudge_selected(players, state, 0.5)

        self.assertNotIn(("seek_absolute", 0.5), players[0].client.commands)
        self.assertEqual(players[0].offset_seconds, 0.0)
        self.assertEqual(state.last_action, "video 1 timestamp is unavailable")

    def test_refresh_frame_duration_uses_mpv_fps_or_fallback(self) -> None:
        players = make_players(2)
        players[0].client.frame_rate = 60000 / 1001

        multi_player.refresh_frame_duration(players[0], fallback_seconds=0.033)
        multi_player.refresh_frame_duration(players[1], fallback_seconds=0.033)

        self.assertAlmostEqual(players[0].frame_seconds or 0.0, 1001 / 60000)
        self.assertEqual(players[1].frame_seconds, 0.033)
        self.assertEqual(players[0].client.commands.count(("frame_rate",)), 1)
        self.assertEqual(players[1].client.commands.count(("frame_rate",)), 1)

    def test_nudge_selected_frame_uses_cached_frame_duration(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].frame_seconds = 1001 / 60000

        multi_player.nudge_selected_frame(players, state, 1, fallback_seconds=0.033)

        self.assertIn(("seek_absolute", 10.0 + 1001 / 60000), players[0].client.commands)
        self.assertNotIn(("frame_rate",), players[0].client.commands)

    def test_seek_all_after_nudge_uses_canonical_aligned_elapsed(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].client.time_positions = [10.0, 10.0, 10.0, 10.0]

        with patch.object(multi_player.time, "monotonic", return_value=100.0):
            multi_player.nudge_selected(players, state, 0.5)
            multi_player.seek_all(players, state, 5.0)

        self.assertIn(("seek_absolute", 10.5), players[0].client.commands)
        self.assertIn(("seek_absolute", 15.5), players[0].client.commands)
        self.assertIn(("seek_absolute", 15.0), players[1].client.commands)

    def test_seek_all_preserves_alignment_after_paused_nudges_on_each_player(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].client.pause = True
        players[1].client.pause = True
        players[0].client.time_pos = 10.0
        players[1].start_seconds = 100.0
        players[1].client.time_pos = 110.0

        multi_player.nudge_selected(players, state, 0.5)
        state.selected_index = 2
        multi_player.nudge_selected(players, state, -0.25)
        multi_player.seek_all(players, state, -0.5)

        self.assertEqual(players[0].offset_seconds, 0.5)
        self.assertEqual(players[1].offset_seconds, -0.25)
        self.assertIn(("seek_absolute", 10.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 109.25), players[1].client.commands)
        self.assertAlmostEqual(state.aligned_elapsed_seconds or 0.0, 9.5)

    def test_seek_all_moves_every_player(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()

        multi_player.seek_all(players, state, -5.0)

        expected_targets = {1: 5.0, 2: 5.0, 3: 5.0}
        for player in players:
            self.assertIn(("seek_absolute", expected_targets[player.index]), player.client.commands)
            self.assertIn(("time_pos",), player.client.commands)
            expected_state = "[V] [A]" if player.index == 1 else "       "
            self.assertIn(
                (
                    "show_text",
                    f"time  00:00:{expected_targets[player.index]:02.0f}.000\nnudge +0.000s\n{expected_state}\nseek  -5.000s",
                    multi_player.ACTION_OSD_MS,
                ),
                player.client.commands,
            )
        self.assertEqual(state.last_action, "seek all -5.000s")

    def test_seek_all_respects_start_offsets(self) -> None:
        players = make_players(4)
        state = multi_player.ControllerState(selected_index=4)
        for player, start_seconds in zip(players, [420.0, 420.0, 420.0, 0.0], strict=True):
            player.start_seconds = start_seconds
            player.client.time_pos = start_seconds + 10.0
        players[3].client.time_pos = 10.0

        multi_player.seek_all(players, state, 2.0)

        self.assertIn(("seek_absolute", 432.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 432.0), players[1].client.commands)
        self.assertIn(("seek_absolute", 432.0), players[2].client.commands)
        self.assertIn(("seek_absolute", 12.0), players[3].client.commands)

    def test_seek_all_preserves_nudged_offsets(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        for player in players:
            player.start_seconds = 100.0
        players[0].offset_seconds = 1.0
        players[0].client.time_pos = 111.0
        players[1].client.time_pos = 110.0

        multi_player.seek_all(players, state, 5.0)

        self.assertIn(("seek_absolute", 116.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 115.0), players[1].client.commands)

    def test_seek_all_can_seek_before_initial_start_offsets(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState()
        for player in players:
            player.start_seconds = 420.0
            player.client.time_pos = 420.0
        players[0].client.time_pos = 420.0

        multi_player.seek_all(players, state, -5.0)

        for player in players:
            self.assertIn(("seek_absolute", 415.0), player.client.commands)
        self.assertEqual(state.last_action, "seek all -5.000s")

    def test_seek_all_clips_to_keep_every_video_at_or_after_zero(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState()
        players[0].start_seconds = 3.0
        players[1].start_seconds = 10.0
        players[0].client.time_pos = 3.0
        players[1].client.time_pos = 10.0

        multi_player.seek_all(players, state, -5.0)

        self.assertIn(("seek_absolute", 0.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 7.0), players[1].client.commands)
        self.assertIn(
            (
                "show_text",
                "time  00:00:00.000\nnudge +0.000s\n[V] [A]\nseek  -3.000s",
                multi_player.ACTION_OSD_MS,
            ),
            players[0].client.commands,
        )
        self.assertEqual(state.last_action, "seek all -3.000s")

    def test_seek_all_clips_with_nudged_offsets(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState()
        players[0].start_seconds = 3.0
        players[1].start_seconds = 10.0
        players[0].offset_seconds = -1.0
        players[1].offset_seconds = 2.0
        players[0].client.time_pos = 2.0
        players[1].client.time_pos = 12.0

        multi_player.seek_all(players, state, -5.0)

        self.assertIn(("seek_absolute", 0.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 10.0), players[1].client.commands)
        self.assertEqual(state.last_action, "seek all -2.000s")

    def test_handle_key_maps_seek_and_nudge_levels_to_expected_sizes(self) -> None:
        args = argparse.Namespace(nudge_small=0.033, nudge_large=0.5, seek_medium=2.0, seek_small=5.0, seek_large=30.0)

        for key, expected_seconds in (
            ("seek_all_back_xs", -1001 / 60000),
            ("seek_all_forward_xs", 1001 / 60000),
            ("seek_all_back_s", -0.5),
            ("seek_all_forward_m", 2.0),
            ("seek_all_back_l", -5.0),
            ("seek_all_forward_xl", 30.0),
        ):
            with self.subTest(key=key):
                players = make_players(2)
                state = multi_player.ControllerState()
                players[0].frame_seconds = 1001 / 60000

                multi_player.handle_key(key, players, state, args)

                for player in players:
                    self.assertIn(("seek_absolute", 10.0 + expected_seconds), player.client.commands)

        for key, expected_seconds in (
            ("nudge_back_xs", -0.033),
            ("nudge_forward_s", 0.5),
            ("nudge_back_m", -2.0),
            ("nudge_forward_l", 5.0),
        ):
            with self.subTest(key=key):
                players = make_players(2)
                state = multi_player.ControllerState(selected_index=2)

                multi_player.handle_key(key, players, state, args)

                self.assertIn(("seek_absolute", 20.0 + expected_seconds), players[1].client.commands)
                self.assertEqual(players[0].client.commands, [])

        for key, expected_volume in (
            ("volume_down_s", 106),
            ("volume_up_s", 116),
            ("volume_down_l", 91),
            ("volume_up_l", 131),
            ("volume_reset", 100),
        ):
            with self.subTest(key=key):
                players = make_players(2)
                state = multi_player.ControllerState(selected_index=2, audio_index=1)
                players[0].volume = 111
                players[1].volume = 150
                if key == "volume_reset":
                    players[0].volume = 123

                multi_player.handle_key(key, players, state, args)

                self.assertEqual(players[0].volume, expected_volume)
                self.assertEqual(players[1].volume, 150)
                self.assertIn(("volume", expected_volume), players[0].client.commands)
                self.assertNotIn(("volume", expected_volume), players[1].client.commands)

        for key, expected_speed in (
            ("speed_down_s", 1.4),
            ("speed_up_s", 1.6),
            ("speed_down_l", 1.25),
            ("speed_up_l", 1.75),
            ("speed_reset", 1.0),
        ):
            with self.subTest(key=key):
                players = make_players(2)
                state = multi_player.ControllerState(speed=1.5)

                multi_player.handle_key(key, players, state, args)

                self.assertAlmostEqual(state.speed, expected_speed)
                for player in players:
                    self.assertIn(("speed", expected_speed), player.client.commands)

    def test_sync_to_selected_time_seeks_other_players_to_selected_timestamp(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=2)

        multi_player.sync_to_selected_time(players, state)

        self.assertNotIn(("seek_absolute", 20.0), players[1].client.commands)
        self.assertIn(("seek_absolute", 20.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 20.0), players[2].client.commands)
        for player in players:
            self.assertIn(("time_pos",), player.client.commands)
        self.assertNotIn(
            (
                "show_text",
                multi_player.format_osd_state(players[1], state, "sync 2 00:00:20.000"),
                multi_player.ACTION_OSD_MS,
            ),
            players[1].client.commands,
        )
        for player in (players[0], players[2]):
            self.assertIn(
                (
                    "show_text",
                    multi_player.format_osd_state(player, state, "sync 2 00:00:20.000"),
                    multi_player.ACTION_OSD_MS,
                ),
                player.client.commands,
            )
        self.assertEqual(state.last_action, "synced all to video 2 at 00:00:20.000")

    def test_sync_to_selected_time_respects_start_offsets(self) -> None:
        players = make_players(4)
        state = multi_player.ControllerState(selected_index=2)
        for player, start_seconds in zip(players, [420.0, 420.0, 420.0, 0.0], strict=True):
            player.start_seconds = start_seconds
        players[1].client.time_pos = 430.0

        multi_player.sync_to_selected_time(players, state)

        self.assertIn(("seek_absolute", 430.0), players[0].client.commands)
        self.assertNotIn(("seek_absolute", 430.0), players[1].client.commands)
        self.assertIn(("seek_absolute", 430.0), players[2].client.commands)
        self.assertIn(("seek_absolute", 10.0), players[3].client.commands)

    def test_sync_to_selected_time_respects_start_offsets_from_segment_selected(self) -> None:
        players = make_players(4)
        state = multi_player.ControllerState(selected_index=4)
        for player, start_seconds in zip(players, [420.0, 420.0, 420.0, 0.0], strict=True):
            player.start_seconds = start_seconds
        players[3].client.time_pos = 10.0

        multi_player.sync_to_selected_time(players, state)

        self.assertIn(("seek_absolute", 430.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 430.0), players[1].client.commands)
        self.assertIn(("seek_absolute", 430.0), players[2].client.commands)
        self.assertNotIn(("seek_absolute", 10.0), players[3].client.commands)

    def test_sync_to_selected_time_updates_other_offsets_to_selected_start_adjusted_timestamp(self) -> None:
        players = make_players(3)
        state = multi_player.ControllerState(selected_index=1)
        for player in players:
            player.start_seconds = 100.0
        players[0].offset_seconds = 1.0
        players[1].offset_seconds = -2.0
        players[2].offset_seconds = 3.0
        players[0].client.time_pos = 111.0
        players[1].client.time_pos = 108.0
        players[2].client.time_pos = 113.0

        multi_player.sync_to_selected_time(players, state)

        self.assertNotIn(("seek_absolute", 111.0), players[0].client.commands)
        self.assertIn(("seek_absolute", 111.0), players[1].client.commands)
        self.assertIn(("seek_absolute", 111.0), players[2].client.commands)
        self.assertEqual(players[0].offset_seconds, 1.0)
        self.assertEqual(players[1].offset_seconds, 1.0)
        self.assertEqual(players[2].offset_seconds, 1.0)

    def test_sync_to_selected_time_corrects_elapsed_drift_after_relative_seek(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].start_seconds = 0.0
        players[1].start_seconds = 100.0
        players[0].offset_seconds = 0.5
        players[1].offset_seconds = -0.25
        players[0].client.time_pos = 11.5
        players[1].client.time_pos = 112.0

        multi_player.sync_to_selected_time(players, state)

        self.assertNotIn(("seek_absolute", 11.5), players[0].client.commands)
        self.assertIn(("seek_absolute", 111.5), players[1].client.commands)

    def test_sync_to_selected_time_handles_unavailable_selected_timestamp(self) -> None:
        players = make_players(2)
        state = multi_player.ControllerState(selected_index=1)
        players[0].client.time_pos = None

        multi_player.sync_to_selected_time(players, state)

        self.assertNotIn(("seek_absolute", None), players[1].client.commands)
        self.assertEqual(state.last_action, "video 1 timestamp is unavailable")

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
