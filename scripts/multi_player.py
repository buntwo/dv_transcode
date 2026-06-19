#!/usr/bin/env python3
"""Synchronized multi-player video control using mpv."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_WIDTH = 960
DEFAULT_HEIGHT = 540
DEFAULT_GAP = 0
DEFAULT_X = 40
DEFAULT_Y = 80
DEFAULT_SEEK_SMALL = 5.0
DEFAULT_SEEK_LARGE = 30.0
DEFAULT_NUDGE_SMALL = 0.0333667
DEFAULT_NUDGE_LARGE = 0.5
MIN_VIDEOS = 2
MAX_VIDEOS = 4
SELECTED_OSD_MS = 500
AUDIO_OSD_MS = 500
ACTION_OSD_MS = 500
PERSISTENT_OSD_MS = 3_600_000
STATUS_REFRESH_SECONDS = 0.5
MAX_SELECT_TIMEOUT_SECONDS = 0.1
PLAYER_READY_TIMEOUT_SECONDS = 10.0
IPC_CONNECT_ATTEMPTS = 10
IPC_CONNECT_RETRY_SECONDS = 0.02
SEEK_KEYS = frozenset(
    {
        "seek_all_back_xs",
        "seek_all_forward_xs",
        "seek_all_back_s",
        "seek_all_forward_s",
        "seek_all_back_m",
        "seek_all_forward_m",
        "seek_all_back_l",
        "seek_all_forward_l",
    }
)


@dataclass(frozen=True)
class WindowGeometry:
    width: int
    height: int
    x: int
    y: int

    def mpv_value(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


class MpvClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def command(self, *parts: Any) -> dict[str, Any]:
        payload = json.dumps({"command": list(parts)}).encode("utf-8") + b"\n"
        response = send_ipc_payload(self.socket_path, payload)
        return parse_mpv_response(response)

    def seek(self, seconds: float) -> None:
        self.command("seek", seconds, "relative", "exact")

    def seek_absolute(self, seconds: float) -> None:
        self.command("seek", seconds, "absolute", "exact")

    def set_volume(self, volume: int) -> None:
        self.command("set_property", "volume", volume)

    def set_title(self, title: str) -> None:
        self.command("set_property", "title", title)

    def get_pause(self) -> bool:
        response = self.command("get_property", "pause")
        return bool(response.get("data"))

    def get_time_pos(self) -> float | None:
        response = self.command("get_property", "time-pos")
        data = response.get("data")
        if isinstance(data, int | float):
            return float(data)
        return None

    def set_pause(self, pause: bool) -> None:
        self.command("set_property", "pause", pause)

    def show_text(self, text: str, duration_ms: int) -> None:
        self.command("show-text", text, duration_ms)


def send_ipc_payload(socket_path: Path, payload: bytes) -> bytes:
    for attempt in range(IPC_CONNECT_ATTEMPTS):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(socket_path))
                sock.sendall(payload)
                return sock.recv(65536)
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED or attempt == IPC_CONNECT_ATTEMPTS - 1:
                raise
            time.sleep(IPC_CONNECT_RETRY_SECONDS)
    return b""


def parse_mpv_response(response: bytes) -> dict[str, Any]:
    if not response:
        return {}

    fallback: dict[str, Any] | None = None
    for line in response.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            continue
        if fallback is None:
            fallback = parsed
        if "event" not in parsed:
            return parsed
    return fallback or {}


@dataclass
class Player:
    index: int
    video: Path
    geometry: WindowGeometry
    socket_path: Path
    process: subprocess.Popen[bytes] | None = None
    client: MpvClient | None = None
    offset_seconds: float = 0.0
    position_seconds: float | None = None
    osd_block_until: float = 0.0

    @property
    def title_name(self) -> str:
        return f"{self.index}: {self.video.name}"


@dataclass
class ControllerState:
    selected_index: int = 1
    audio_index: int = 1
    muted: bool = False
    display_enabled: bool = True
    last_action: str = "ready"


def calculate_geometry(count: int, width: int, height: int, gap: int, x: int, y: int) -> list[WindowGeometry]:
    return [
        WindowGeometry(
            width=width,
            height=height,
            x=x + ((index - 1) % 2) * (width + gap),
            y=y + ((index - 1) // 2) * (height + gap),
        )
        for index in range(1, count + 1)
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open 2 to 4 videos in mpv and control them together.",
    )
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--width", type=positive_int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=positive_int, default=DEFAULT_HEIGHT)
    parser.add_argument("--gap", type=non_negative_int, default=DEFAULT_GAP)
    parser.add_argument("-x", type=non_negative_int, default=DEFAULT_X)
    parser.add_argument("-y", type=non_negative_int, default=DEFAULT_Y)
    parser.add_argument("--monitor", type=positive_int)
    parser.add_argument("--seek-small", type=positive_float, default=DEFAULT_SEEK_SMALL)
    parser.add_argument("--seek-large", type=positive_float, default=DEFAULT_SEEK_LARGE)
    parser.add_argument("--nudge-small", type=positive_float, default=DEFAULT_NUDGE_SMALL)
    parser.add_argument("--nudge-large", type=positive_float, default=DEFAULT_NUDGE_LARGE)
    for index in range(1, MAX_VIDEOS + 1):
        parser.add_argument(
            f"-ss{index}",
            dest=f"ss{index}",
            type=parse_timestamp,
            metavar="TIMESTAMP",
            help=(
                f"Pre-seek video {index} before playback. "
                "Accepts seconds, MM:SS, or HH:MM:SS with optional decimals."
            ),
        )
    args = parser.parse_args(argv)
    if not MIN_VIDEOS <= len(args.videos) <= MAX_VIDEOS:
        parser.error(f"expected {MIN_VIDEOS} to {MAX_VIDEOS} videos")
    for index in range(len(args.videos) + 1, MAX_VIDEOS + 1):
        if getattr(args, f"ss{index}") is not None:
            parser.error(f"-ss{index} was supplied but video {index} is not loaded")
    return args


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def parse_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(parts) > 3:
        raise argparse.ArgumentTypeError("must be seconds, MM:SS, or HH:MM:SS")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        else:
            if any(part == "" for part in parts):
                raise ValueError
            seconds = float(parts[-1])
            for multiplier, part in zip((60, 3600), reversed(parts[:-1]), strict=False):
                if "." in part:
                    raise ValueError
                seconds += int(part) * multiplier
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be seconds, MM:SS, or HH:MM:SS") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return seconds


def collect_start_times(args: argparse.Namespace) -> list[float]:
    return [getattr(args, f"ss{index}") or 0.0 for index in range(1, len(args.videos) + 1)]


def validate_inputs(args: argparse.Namespace) -> None:
    for index, path in enumerate(args.videos, start=1):
        if not path.exists():
            raise ValueError(f"video {index} does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"video {index} is not a file: {path}")
    if shutil.which("mpv") is None:
        raise ValueError("mpv is required; install with: brew install mpv")


def launch_mpv(
    video: Path,
    title: str,
    geometry: WindowGeometry,
    ipc_socket: Path,
    volume: int,
    screen: int | None = None,
) -> subprocess.Popen[bytes]:
    cmd = [
        "mpv",
        "--no-terminal",
        "--pause",
        "--osd-align-x=left",
        "--osd-align-y=top",
        "--osd-font=monospace",
        f"--input-ipc-server={ipc_socket}",
        f"--geometry={geometry.mpv_value()}",
        f"--title={title}",
        f"--volume={volume}",
    ]
    if screen is not None:
        cmd.append(f"--screen={screen}")
    cmd.append(str(video))
    return subprocess.Popen(cmd)


def wait_for_socket(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for mpv IPC socket: {path}")


def normalize_key(sequence: bytes) -> str | None:
    if sequence == b" ":
        return "space"
    if sequence in (b"\r", b"\n"):
        return "selected_pause"
    if sequence in (b"q", b"\x03"):
        return "quit"
    if sequence == b"m":
        return "mute"
    if sequence == b"d":
        return "display"
    if sequence == b"z":
        return "seek_all_back_xs"
    if sequence == b"x":
        return "seek_all_forward_xs"
    if sequence == b"Z":
        return "seek_all_back_s"
    if sequence == b"X":
        return "seek_all_forward_s"
    if sequence == b"a":
        return "seek_all_back_m"
    if sequence == b"s":
        return "seek_all_forward_m"
    if sequence == b"A":
        return "seek_all_back_l"
    if sequence == b"S":
        return "seek_all_forward_l"
    if sequence == b",":
        return "nudge_back_xs"
    if sequence == b".":
        return "nudge_forward_xs"
    if sequence == b"<":
        return "nudge_back_s"
    if sequence == b">":
        return "nudge_forward_s"
    if sequence == b"k":
        return "nudge_back_m"
    if sequence == b"l":
        return "nudge_forward_m"
    if sequence == b"K":
        return "nudge_back_l"
    if sequence == b"L":
        return "nudge_forward_l"
    if sequence in (b"1", b"2", b"3", b"4"):
        return f"select_{sequence.decode('ascii')}"
    if sequence == b"!":
        return "audio_1"
    if sequence == b"@":
        return "audio_2"
    if sequence == b"#":
        return "audio_3"
    if sequence == b"$":
        return "audio_4"
    return None


def read_key() -> str | None:
    first = os.read(sys.stdin.fileno(), 1)
    if first != b"\x1b":
        return normalize_key(first)

    sequence = bytearray(first)
    while True:
        readable, _, _ = select.select([sys.stdin], [], [], 0.01)
        if not readable:
            break
        sequence.extend(os.read(sys.stdin.fileno(), 1))
        if sequence.endswith((b"A", b"B", b"C", b"D", b"u", b" ")):
            break
    return normalize_key(bytes(sequence))


def flush_pending_input() -> None:
    termios.tcflush(sys.stdin, termios.TCIFLUSH)


def print_help() -> None:
    print(
        "\n".join(
            [
                "Controls:",
                "  space             play/pause all",
                "  enter             play/pause selected video",
                "  z/x               seek all backward/forward by xs step",
                "  Z/X               seek all backward/forward by s step",
                "  a/s               seek all backward/forward by m step",
                "  A/S               seek all backward/forward by l step",
                "  ,/.               nudge selected video backward/forward by xs step",
                "  </>               nudge selected video backward/forward by s step",
                "  k/l               nudge selected video backward/forward by m step",
                "  K/L               nudge selected video backward/forward by l step",
                "  1/2/3/4           select video for nudging",
                "  !/@/#/$           activate audio for video 1/2/3/4",
                "  m                 toggle mute for all",
                "  d                 toggle persistent timestamp/nudge display",
                "  q                 quit",
                "",
            ]
        )
    )


def print_offset_summary(players: list[Player]) -> None:
    print("Final offset:")
    for player in players:
        print(f"  {player.index}: {player.offset_seconds:+.3f}s")


def print_status(message: str) -> None:
    sys.stdout.write(f"\r\033[K{message}")
    sys.stdout.flush()


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--.---"

    sign = "-" if seconds < 0 else ""
    total_ms = round(abs(seconds) * 1000)
    total_seconds, milliseconds = divmod(total_ms, 1000)
    minutes_total, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{sign}{hours:02}:{minutes:02}:{seconds_part:02}.{milliseconds:03}"


def format_status(players: list[Player], state: ControllerState) -> str:
    mute_state = "on" if state.muted else "off"
    positions = " ".join(f"{player.index}:{format_timestamp(player.position_seconds)}" for player in players)
    offsets = " ".join(f"{player.index}:{player.offset_seconds:+.3f}s" for player in players)
    return (
        f"selected {state.selected_index} | audio {state.audio_index} | mute {mute_state} | "
        f"pos {positions} | offsets {offsets} | {state.last_action}"
    )


def render_status(players: list[Player], state: ControllerState, message: str) -> None:
    state.last_action = message
    print_status(format_status(players, state))


def player_title(player: Player, state: ControllerState) -> str:
    selected_marker = "*" if player.index == state.selected_index else " "
    audio_marker = "[A]" if player.index == state.audio_index else "   "
    return f"{selected_marker} {audio_marker} {player.title_name}"


def live_client(player: Player) -> MpvClient:
    if player.client is None:
        raise RuntimeError(f"video {player.index} is not connected")
    return player.client


def refresh_position(player: Player) -> None:
    player.position_seconds = live_client(player).get_time_pos()


def refresh_positions(players: list[Player]) -> None:
    for player in players:
        refresh_position(player)


def wait_for_player_ready(player: Player, timeout: float = PLAYER_READY_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if player.process is not None and player.process.poll() is not None:
            raise RuntimeError(f"video {player.index} exited before it was ready")
        refresh_position(player)
        if player.position_seconds is not None:
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for video {player.index} to become ready")


def persistent_osd_text(player: Player, state: ControllerState) -> str:
    return format_osd_state(player, state)


def format_osd_state(player: Player, state: ControllerState, action: str | None = None) -> str:
    selected_marker = "[V]" if player.index == state.selected_index else "   "
    audio_marker = "[A]" if player.index == state.audio_index else "   "
    lines = [
        f"time  {format_timestamp(player.position_seconds)}",
        f"nudge {player.offset_seconds:+.3f}s",
        f"{selected_marker} {audio_marker}",
    ]
    if action is not None:
        lines.append(action)
    return "\n".join(lines)


def show_temporary_osd(player: Player, text: str, duration_ms: int) -> None:
    player.osd_block_until = time.monotonic() + duration_ms / 1000
    live_client(player).show_text(text, duration_ms)


def update_persistent_display(player: Player, state: ControllerState, now: float | None = None, force: bool = False) -> None:
    if not state.display_enabled:
        return
    current_time = time.monotonic() if now is None else now
    if force or current_time >= player.osd_block_until:
        live_client(player).show_text(persistent_osd_text(player, state), PERSISTENT_OSD_MS)
        player.osd_block_until = 0.0


def update_persistent_displays(players: list[Player], state: ControllerState, force: bool = False) -> None:
    for player in players:
        update_persistent_display(player, state, force=force)


def restore_due_persistent_displays(players: list[Player], state: ControllerState, now: float) -> None:
    if not state.display_enabled:
        return
    for player in players:
        if player.osd_block_until and now >= player.osd_block_until:
            update_persistent_display(player, state, now=now)


def next_persistent_restore_time(players: list[Player], state: ControllerState) -> float | None:
    if not state.display_enabled:
        return None
    pending = [player.osd_block_until for player in players if player.osd_block_until]
    if not pending:
        return None
    return min(pending)


def next_loop_timeout(now: float, next_status_refresh: float, next_osd_restore: float | None) -> float:
    next_deadline = next_status_refresh
    if next_osd_restore is not None:
        next_deadline = min(next_deadline, next_osd_restore)
    return max(0.0, min(MAX_SELECT_TIMEOUT_SECONDS, next_deadline - now))


def clear_persistent_displays(players: list[Player]) -> None:
    for player in players:
        player.osd_block_until = 0.0
        live_client(player).show_text("", 1)


def update_titles(players: list[Player], state: ControllerState) -> None:
    for player in players:
        live_client(player).set_title(player_title(player, state))


def get_player(players: list[Player], index: int) -> Player | None:
    for player in players:
        if player.index == index:
            return player
    return None


def select_player(players: list[Player], state: ControllerState, index: int) -> None:
    player = get_player(players, index)
    if player is None:
        render_status(players, state, f"video {index} is not loaded")
        return
    state.selected_index = index
    update_titles(players, state)
    if state.display_enabled:
        update_persistent_displays(players, state, force=True)
    else:
        show_temporary_osd(player, f"SELECTED {index}", SELECTED_OSD_MS)
    render_status(players, state, f"selected {player.title_name}")


def activate_audio(players: list[Player], state: ControllerState, index: int, flash: bool = True) -> None:
    player = get_player(players, index)
    if player is None:
        render_status(players, state, f"video {index} is not loaded")
        return
    state.audio_index = index
    for current in players:
        live_client(current).set_volume(100 if current.index == index else 0)
    update_titles(players, state)
    if flash:
        if state.display_enabled:
            update_persistent_displays(players, state, force=True)
        else:
            show_temporary_osd(player, f"AUDIO {index}", AUDIO_OSD_MS)
        render_status(players, state, f"audio {player.title_name}")


def set_all_pause(players: list[Player], state: ControllerState) -> None:
    next_pause = not live_client(players[0]).get_pause()
    for player in players:
        live_client(player).set_pause(next_pause)
        show_temporary_osd(player, "PAUSED" if next_pause else "PLAYING", ACTION_OSD_MS)
    render_status(players, state, "paused" if next_pause else "playing")


def toggle_selected_pause(players: list[Player], state: ControllerState) -> None:
    player = get_player(players, state.selected_index)
    if player is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return
    next_pause = not live_client(player).get_pause()
    live_client(player).set_pause(next_pause)
    show_temporary_osd(player, "PAUSED" if next_pause else "PLAYING", ACTION_OSD_MS)
    render_status(players, state, f"{'paused' if next_pause else 'playing'} {player.title_name}")


def start_all_playback(players: list[Player], state: ControllerState) -> None:
    for player in players:
        live_client(player).set_pause(False)
    render_status(players, state, "started")


def preseek_players(players: list[Player], start_times: list[float], state: ControllerState) -> None:
    for player, start_time in zip(players, start_times, strict=True):
        if start_time > 0:
            live_client(player).seek_absolute(start_time)
        refresh_position(player)
    if any(start_time > 0 for start_time in start_times):
        render_status(players, state, "pre-seeked")


def seek_all(players: list[Player], state: ControllerState, seconds: float) -> None:
    for player in players:
        live_client(player).seek(seconds)
        refresh_position(player)
        show_temporary_osd(player, format_osd_state(player, state, f"seek  {seconds:+.3f}s"), ACTION_OSD_MS)
    render_status(players, state, f"seek all {seconds:+.3f}s")


def nudge_selected(players: list[Player], state: ControllerState, seconds: float) -> None:
    player = get_player(players, state.selected_index)
    if player is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return
    live_client(player).seek(seconds)
    player.offset_seconds += seconds
    refresh_position(player)
    show_temporary_osd(
        player,
        format_osd_state(player, state, f"delta {seconds:+.3f}s"),
        ACTION_OSD_MS,
    )
    render_status(players, state, f"video {player.index} offset {player.offset_seconds:+.3f}s")


def toggle_mute(players: list[Player], state: ControllerState) -> None:
    state.muted = not state.muted
    for player in players:
        live_client(player).command("cycle", "mute")
        show_temporary_osd(player, "MUTED" if state.muted else "UNMUTED", ACTION_OSD_MS)
    render_status(players, state, "muted" if state.muted else "unmuted")


def toggle_display(players: list[Player], state: ControllerState) -> None:
    state.display_enabled = not state.display_enabled
    if state.display_enabled:
        refresh_positions(players)
        update_persistent_displays(players, state, force=True)
    else:
        clear_persistent_displays(players)
    render_status(players, state, state.last_action)


def handle_key(
    key: str,
    players: list[Player],
    state: ControllerState,
    args: argparse.Namespace,
) -> bool:
    if key == "quit":
        return False
    if key == "space":
        set_all_pause(players, state)
    elif key == "selected_pause":
        toggle_selected_pause(players, state)
    elif key == "seek_all_back_xs":
        seek_all(players, state, -args.nudge_small)
    elif key == "seek_all_forward_xs":
        seek_all(players, state, args.nudge_small)
    elif key == "seek_all_back_s":
        seek_all(players, state, -args.nudge_large)
    elif key == "seek_all_forward_s":
        seek_all(players, state, args.nudge_large)
    elif key == "seek_all_back_m":
        seek_all(players, state, -args.seek_small)
    elif key == "seek_all_forward_m":
        seek_all(players, state, args.seek_small)
    elif key == "seek_all_back_l":
        seek_all(players, state, -args.seek_large)
    elif key == "seek_all_forward_l":
        seek_all(players, state, args.seek_large)
    elif key == "nudge_back_xs":
        nudge_selected(players, state, -args.nudge_small)
    elif key == "nudge_forward_xs":
        nudge_selected(players, state, args.nudge_small)
    elif key == "nudge_back_s":
        nudge_selected(players, state, -args.nudge_large)
    elif key == "nudge_forward_s":
        nudge_selected(players, state, args.nudge_large)
    elif key == "nudge_back_m":
        nudge_selected(players, state, -args.seek_small)
    elif key == "nudge_forward_m":
        nudge_selected(players, state, args.seek_small)
    elif key == "nudge_back_l":
        nudge_selected(players, state, -args.seek_large)
    elif key == "nudge_forward_l":
        nudge_selected(players, state, args.seek_large)
    elif key.startswith("select_"):
        select_player(players, state, int(key[-1]))
    elif key.startswith("audio_"):
        activate_audio(players, state, int(key[-1]))
    elif key == "mute":
        toggle_mute(players, state)
    elif key == "display":
        toggle_display(players, state)
    return True


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run(args: argparse.Namespace) -> int:
    validate_inputs(args)
    geometries = calculate_geometry(len(args.videos), args.width, args.height, args.gap, args.x, args.y)
    start_times = collect_start_times(args)
    screen = args.monitor - 1 if args.monitor is not None else None
    state = ControllerState()
    players: list[Player] = []

    with tempfile.TemporaryDirectory(prefix="multi_player_") as tmp:
        tmp_path = Path(tmp)
        players = [
            Player(
                index=index,
                video=video,
                geometry=geometry,
                socket_path=tmp_path / f"player-{index}.sock",
            )
            for index, (video, geometry) in enumerate(zip(args.videos, geometries, strict=True), start=1)
        ]

        for player in players:
            player.process = launch_mpv(
                player.video,
                player_title(player, state),
                player.geometry,
                player.socket_path,
                100 if player.index == state.audio_index else 0,
                screen,
            )

        try:
            for player in players:
                wait_for_socket(player.socket_path)
                player.client = MpvClient(player.socket_path)
            for player in players:
                wait_for_player_ready(player)
            preseek_players(players, start_times, state)
            update_titles(players, state)
            activate_audio(players, state, state.audio_index, flash=False)
            print_help()
            start_all_playback(players, state)

            old_term = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                next_status_refresh = time.monotonic() + STATUS_REFRESH_SECONDS
                while all(player.process is not None and player.process.poll() is None for player in players):
                    now = time.monotonic()
                    timeout = next_loop_timeout(now, next_status_refresh, next_persistent_restore_time(players, state))
                    readable, _, _ = select.select([sys.stdin], [], [], timeout)
                    now = time.monotonic()
                    restore_due_persistent_displays(players, state, now)
                    if now >= next_status_refresh:
                        refresh_positions(players)
                        update_persistent_displays(players, state)
                        print_status(format_status(players, state))
                        next_status_refresh = now + STATUS_REFRESH_SECONDS
                    if not readable:
                        continue
                    key = read_key()
                    if key is None:
                        continue
                    if not handle_key(key, players, state, args):
                        break
                    if key in SEEK_KEYS:
                        flush_pending_input()
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)
                print()
        finally:
            for player in players:
                if player.process is not None:
                    terminate_process(player.process)
            print_offset_summary(players)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print()
        return 130
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
