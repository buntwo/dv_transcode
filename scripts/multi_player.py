#!/usr/bin/env python3
"""Synchronized multi-player video control using mpv."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import select
import shlex
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
DEFAULT_X = 0
DEFAULT_Y = 0
DEFAULT_SEEK_MEDIUM = 2.0
DEFAULT_SEEK_SMALL = 5.0
DEFAULT_SEEK_LARGE = 30.0
DEFAULT_NUDGE_SMALL = 0.0333667
DEFAULT_NUDGE_LARGE = 0.5
DEFAULT_VOLUME = 100
MAX_VOLUME = 200
VOLUME_STEP_SMALL = 5
VOLUME_STEP_LARGE = 20
DEFAULT_SPEED = 1.0
MIN_SPEED = 0.1
MAX_SPEED = 4.0
SPEED_STEP_SMALL = 0.1
SPEED_STEP_LARGE = 0.25
MIN_VIDEOS = 2
MAX_VIDEOS = 4
SELECTED_OSD_MS = 500
AUDIO_OSD_MS = 500
ACTION_OSD_MS = 500
PERSISTENT_OSD_MS = 3_600_000
PAUSE_OSD_TEXT = "▌▌"
PLAY_OSD_TEXT = "▶"
OSD_FONT = "monospace"
DEFAULT_OSD_FONT_SIZE = 30
PLAY_PAUSE_OSD_FONT_SIZE = 72
STATUS_REFRESH_SECONDS = 0.5
MAX_SELECT_TIMEOUT_SECONDS = 0.1
PLAYER_READY_TIMEOUT_SECONDS = 10.0
IPC_CONNECT_ATTEMPTS = 10
IPC_CONNECT_RETRY_SECONDS = 0.02
SEEK_SETTLE_SECONDS = 0.25
SEEK_SETTLE_TOLERANCE_SECONDS = 0.005
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
        "seek_all_back_xl",
        "seek_all_forward_xl",
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

    def set_speed(self, speed: float) -> None:
        self.command("set_property", "speed", speed)

    def set_title(self, title: str) -> None:
        self.command("set_property", "title", title)

    def set_osd_font_size(self, size: int) -> None:
        self.command("set_property", "osd-font-size", size)

    def get_pause(self) -> bool:
        response = self.command("get_property", "pause")
        return bool(response.get("data"))

    def get_paused_for_cache(self) -> bool:
        response = self.command("get_property", "paused-for-cache")
        return bool(response.get("data"))

    def get_time_pos(self) -> float | None:
        response = self.command("get_property", "time-pos")
        data = response.get("data")
        if isinstance(data, int | float):
            return float(data)
        return None

    def get_frame_rate(self) -> float | None:
        for property_name in ("estimated-vf-fps", "container-fps"):
            response = self.command("get_property", property_name)
            data = response.get("data")
            if isinstance(data, int | float) and math.isfinite(data) and data > 0:
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
    start_seconds: float = 0.0
    volume: int = DEFAULT_VOLUME
    frame_seconds: float | None = None
    pending_seek_source_seconds: float | None = None
    pending_seek_target_seconds: float | None = None
    pending_seek_until: float = 0.0
    osd_block_until: float = 0.0
    osd_font_size: int = DEFAULT_OSD_FONT_SIZE

    @property
    def title_name(self) -> str:
        return f"{self.index}: {self.video.name}"

    @property
    def timeline_base_seconds(self) -> float:
        return self.start_seconds + self.offset_seconds

    def shift_timeline_base(self, seconds: float) -> None:
        self.offset_seconds += seconds


@dataclass
class ControllerState:
    selected_index: int = 1
    audio_index: int = 1
    muted: bool = False
    display_enabled: bool = True
    speed: float = DEFAULT_SPEED
    aligned_elapsed_seconds: float | None = None
    aligned_elapsed_updated_at: float = 0.0
    cache_pause_index: int | None = None
    auto_paused_for_cache: bool = False
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
    parser.add_argument("--seek-medium", type=positive_float, default=DEFAULT_SEEK_MEDIUM)
    parser.add_argument("--seek-large", type=positive_float, default=DEFAULT_SEEK_LARGE)
    parser.add_argument("--nudge-small", type=positive_float, default=DEFAULT_NUDGE_SMALL)
    parser.add_argument("--nudge-large", type=positive_float, default=DEFAULT_NUDGE_LARGE)
    parser.add_argument(
        "-ss",
        dest="ss",
        type=parse_timestamp,
        metavar="TIMESTAMP",
        help="Pre-seek all videos before playback. Numbered -ssN values override this per video.",
    )
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
        parser.add_argument(
            f"--vol{index}",
            dest=f"vol{index}",
            type=volume_int,
            metavar="VOLUME",
            help=f"Initial volume for video {index}, from 0 to {MAX_VOLUME}.",
        )
    args = parser.parse_args(argv)
    if not MIN_VIDEOS <= len(args.videos) <= MAX_VIDEOS:
        parser.error(f"expected {MIN_VIDEOS} to {MAX_VIDEOS} videos")
    for index in range(len(args.videos) + 1, MAX_VIDEOS + 1):
        if getattr(args, f"ss{index}") is not None:
            parser.error(f"-ss{index} was supplied but video {index} is not loaded")
        if getattr(args, f"vol{index}") is not None:
            parser.error(f"--vol{index} was supplied but video {index} is not loaded")
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


def volume_int(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= MAX_VOLUME:
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_VOLUME}")
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
    default_start_time = args.ss or 0.0
    return [
        specific_start_time if (specific_start_time := getattr(args, f"ss{index}")) is not None else default_start_time
        for index in range(1, len(args.videos) + 1)
    ]


def collect_volumes(args: argparse.Namespace) -> list[int]:
    return [
        volume if (volume := getattr(args, f"vol{index}")) is not None else DEFAULT_VOLUME
        for index in range(1, len(args.videos) + 1)
    ]


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
        "--ontop",
        "--osd-align-x=left",
        "--osd-align-y=top",
        f"--osd-font={OSD_FONT}",
        f"--osd-font-size={DEFAULT_OSD_FONT_SIZE}",
        f"--input-ipc-server={ipc_socket}",
        f"--geometry={geometry.mpv_value()}",
        f"--title={title}",
        f"--volume-max={MAX_VOLUME}",
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
    if sequence == b"\x1a":
        return "seek_all_back_xs"
    if sequence == b"\x18":
        return "seek_all_forward_xs"
    if sequence == b"m":
        return "mute"
    if sequence == b"d":
        return "display"
    if sequence == b"t":
        return "sync_to_selected_time"
    if sequence == b"z":
        return "seek_all_back_s"
    if sequence == b"x":
        return "seek_all_forward_s"
    if sequence == b"Z":
        return "seek_all_back_m"
    if sequence == b"X":
        return "seek_all_forward_m"
    if sequence == b"a":
        return "seek_all_back_l"
    if sequence == b"s":
        return "seek_all_forward_l"
    if sequence == b"A":
        return "seek_all_back_xl"
    if sequence == b"S":
        return "seek_all_forward_xl"
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
    if sequence == b"[":
        return "volume_down_s"
    if sequence == b"]":
        return "volume_up_s"
    if sequence == b"{":
        return "volume_down_l"
    if sequence == b"}":
        return "volume_up_l"
    if sequence == b"\\":
        return "volume_reset"
    if sequence == b"y":
        return "speed_down_s"
    if sequence == b"u":
        return "speed_up_s"
    if sequence == b"Y":
        return "speed_down_l"
    if sequence == b"U":
        return "speed_up_l"
    if sequence == b"i":
        return "speed_reset"
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
                "Controls",
                "",
                "Audio / Select",
                "  !     @     #     $        audio 1-4",
                "  1     2     3     4        select 1-4",
                "",
                "",
                "Seek all videos                  Nudge selected video",
                "  A     S    xl  30s  back/fwd     K     L    l   5s     back/fwd",
                "  a     s    l   5s   back/fwd     k     l    m   2s     back/fwd",
                "  Z     X    m   2s   back/fwd     <     >    s   0.5s   back/fwd",
                "  z     x    s   0.5s back/fwd     ,     .    xs  frame  back/fwd",
                "",
                "",
                "Playback                          Other",
                "  Space      play/pause all       t     sync to selected timestamp",
                "  Enter      play/pause selected  m     mute",
                "  [ ]        volume -/+ 5         \\     volume 100",
                "  { }        volume -/+ 20",
                "  y u        speed -/+ 0.10x      i     speed 1.00x",
                "  Y U        speed -/+ 0.25x",
                "                                  d     display",
                "                                  q     quit",
                "",
            ]
        )
    )


def print_offset_summary(players: list[Player]) -> None:
    print("Final offset:")
    for player in players:
        print(f"  {player.index}: {player.offset_seconds:+.3f}s")
    print()
    print("Copyable positions:")
    print(format_copyable_positions(players))


def format_seconds_argument(seconds: float) -> str:
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def format_copyable_positions(players: list[Player]) -> str:
    start_args: list[str] = []
    volume_args: list[str] = []
    video_args: list[str] = []
    for player in players:
        position_seconds = player.position_seconds if player.position_seconds is not None else player.start_seconds
        start_args.extend([f"-ss{player.index}", format_seconds_argument(position_seconds)])
        volume_args.extend([f"--vol{player.index}", str(player.volume)])
        video_args.append(shlex.quote(str(player.video)))
    return " ".join([*start_args, *volume_args, *video_args])


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


def seek_player_absolute(player: Player, seconds: float) -> None:
    player.pending_seek_source_seconds = player.position_seconds
    live_client(player).seek_absolute(seconds)
    player.position_seconds = seconds
    player.pending_seek_target_seconds = seconds
    player.pending_seek_until = time.monotonic() + SEEK_SETTLE_SECONDS


def refresh_position(player: Player) -> None:
    position_seconds = live_client(player).get_time_pos()
    pending_target_seconds = player.pending_seek_target_seconds
    if pending_target_seconds is None:
        player.position_seconds = position_seconds
        return

    if position_seconds is None:
        return

    now = time.monotonic()
    if abs(position_seconds - pending_target_seconds) <= SEEK_SETTLE_TOLERANCE_SECONDS:
        player.pending_seek_source_seconds = None
        player.pending_seek_target_seconds = None
        player.position_seconds = position_seconds
        return

    if now < player.pending_seek_until:
        player.position_seconds = pending_target_seconds
        return

    pending_source_seconds = player.pending_seek_source_seconds
    if pending_source_seconds is not None and pending_target_seconds > pending_source_seconds:
        if position_seconds < pending_target_seconds - SEEK_SETTLE_TOLERANCE_SECONDS:
            player.position_seconds = pending_target_seconds
            return

    if live_client(player).get_pause():
        player.position_seconds = pending_target_seconds
        return

    player.pending_seek_source_seconds = None
    player.pending_seek_target_seconds = None
    player.position_seconds = position_seconds


def refresh_positions(players: list[Player]) -> None:
    for player in players:
        refresh_position(player)


def refresh_frame_duration(player: Player, fallback_seconds: float) -> None:
    frame_rate = live_client(player).get_frame_rate()
    player.frame_seconds = 1 / frame_rate if frame_rate is not None else fallback_seconds


def refresh_frame_durations(players: list[Player], fallback_seconds: float) -> None:
    for player in players:
        refresh_frame_duration(player, fallback_seconds)


def nudge_selected_frame(players: list[Player], state: ControllerState, direction: int, fallback_seconds: float) -> None:
    player = get_player(players, state.selected_index)
    if player is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return
    nudge_selected(players, state, direction * (player.frame_seconds or fallback_seconds))


def invalidate_aligned_elapsed(state: ControllerState) -> None:
    state.aligned_elapsed_seconds = None
    state.aligned_elapsed_updated_at = 0.0


def set_aligned_elapsed(state: ControllerState, elapsed_seconds: float) -> None:
    state.aligned_elapsed_seconds = elapsed_seconds
    state.aligned_elapsed_updated_at = time.monotonic()


def set_aligned_elapsed_from_player(player: Player, state: ControllerState) -> None:
    if player.position_seconds is not None:
        set_aligned_elapsed(state, player.position_seconds - player.timeline_base_seconds)


def get_aligned_elapsed(players: list[Player], state: ControllerState) -> float | None:
    selected = get_player(players, state.selected_index)
    if selected is None:
        return None
    initialized = state.aligned_elapsed_seconds is None
    if state.aligned_elapsed_seconds is None:
        refresh_position(selected)
        if selected.position_seconds is None:
            return None
        set_aligned_elapsed_from_player(selected, state)
    if state.aligned_elapsed_seconds is None:
        return None
    if initialized:
        return state.aligned_elapsed_seconds
    if not live_client(selected).get_pause():
        now = time.monotonic()
        state.aligned_elapsed_seconds += (now - state.aligned_elapsed_updated_at) * state.speed
        state.aligned_elapsed_updated_at = now
    return state.aligned_elapsed_seconds


def pause_all_if_any_player_is_buffering(players: list[Player], state: ControllerState) -> bool:
    buffering_player = next((player for player in players if live_client(player).get_paused_for_cache()), None)
    if buffering_player is None:
        if state.auto_paused_for_cache:
            state.auto_paused_for_cache = False
            state.cache_pause_index = None
            for player in players:
                live_client(player).set_pause(False)
            refresh_positions(players)
            for player in players:
                show_play_pause_osd(player, False)
            render_status(players, state, "resumed after buffering")
            return True
        state.cache_pause_index = None
        return False
    if state.cache_pause_index == buffering_player.index:
        return True

    state.cache_pause_index = buffering_player.index
    state.auto_paused_for_cache = True
    for player in players:
        live_client(player).set_pause(True)
    refresh_positions(players)
    for player in players:
        show_temporary_osd(player, f"{PAUSE_OSD_TEXT} video {buffering_player.index} buffering", ACTION_OSD_MS)
    render_status(players, state, f"paused: video {buffering_player.index} buffering")
    return True


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


def set_osd_font_size(player: Player, size: int) -> None:
    if player.osd_font_size == size:
        return
    live_client(player).set_osd_font_size(size)
    player.osd_font_size = size


def show_temporary_osd(
    player: Player,
    text: str,
    duration_ms: int,
    font_size: int = DEFAULT_OSD_FONT_SIZE,
) -> None:
    player.osd_block_until = time.monotonic() + duration_ms / 1000
    set_osd_font_size(player, font_size)
    live_client(player).show_text(text, duration_ms)


def play_pause_osd_text(pause: bool) -> str:
    return PAUSE_OSD_TEXT if pause else PLAY_OSD_TEXT


def show_play_pause_osd(player: Player, pause: bool) -> None:
    show_temporary_osd(player, play_pause_osd_text(pause), ACTION_OSD_MS, font_size=PLAY_PAUSE_OSD_FONT_SIZE)


def update_persistent_display(player: Player, state: ControllerState, now: float | None = None, force: bool = False) -> None:
    if not state.display_enabled:
        return
    current_time = time.monotonic() if now is None else now
    if force or current_time >= player.osd_block_until:
        set_osd_font_size(player, DEFAULT_OSD_FONT_SIZE)
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
        live_client(current).set_volume(current.volume if current.index == index else 0)
    update_titles(players, state)
    if flash:
        if state.display_enabled:
            update_persistent_displays(players, state, force=True)
        else:
            show_temporary_osd(player, f"AUDIO {index}", AUDIO_OSD_MS)
        render_status(players, state, f"audio {player.title_name}")


def change_audio_volume(players: list[Player], state: ControllerState, delta: int | None) -> None:
    player = get_player(players, state.audio_index)
    if player is None:
        render_status(players, state, f"video {state.audio_index} is not loaded")
        return
    previous_volume = player.volume
    player.volume = DEFAULT_VOLUME if delta is None else max(0, min(MAX_VOLUME, player.volume + delta))
    actual_delta = player.volume - previous_volume
    live_client(player).set_volume(player.volume)
    refresh_position(player)
    show_temporary_osd(player, format_osd_state(player, state, f"vol {actual_delta:+d} -> {player.volume}"), ACTION_OSD_MS)
    render_status(players, state, f"video {player.index} volume {player.volume}")


def format_speed(speed: float) -> str:
    return f"{speed:.2f}x"


def change_speed(players: list[Player], state: ControllerState, delta: float | None) -> None:
    previous_speed = state.speed
    state.speed = DEFAULT_SPEED if delta is None else max(MIN_SPEED, min(MAX_SPEED, state.speed + delta))
    actual_delta = state.speed - previous_speed
    for player in players:
        live_client(player).set_speed(state.speed)
        refresh_position(player)
        show_temporary_osd(
            player,
            format_osd_state(player, state, f"speed {actual_delta:+.2f} -> {format_speed(state.speed)}"),
            ACTION_OSD_MS,
        )
    render_status(players, state, f"speed {format_speed(state.speed)}")


def set_all_pause(players: list[Player], state: ControllerState) -> None:
    state.auto_paused_for_cache = False
    state.cache_pause_index = None
    next_pause = not live_client(players[0]).get_pause()
    for player in players:
        live_client(player).set_pause(next_pause)
    refresh_positions(players)
    selected = get_player(players, state.selected_index)
    if next_pause and selected is not None:
        set_aligned_elapsed_from_player(selected, state)
    else:
        invalidate_aligned_elapsed(state)
    for player in players:
        show_play_pause_osd(player, next_pause)
    render_status(players, state, "paused" if next_pause else "playing")


def toggle_selected_pause(players: list[Player], state: ControllerState) -> None:
    state.auto_paused_for_cache = False
    state.cache_pause_index = None
    player = get_player(players, state.selected_index)
    if player is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return
    next_pause = not live_client(player).get_pause()
    live_client(player).set_pause(next_pause)
    refresh_position(player)
    if next_pause:
        set_aligned_elapsed_from_player(player, state)
    else:
        invalidate_aligned_elapsed(state)
    show_play_pause_osd(player, next_pause)
    render_status(players, state, f"{'paused' if next_pause else 'playing'} {player.title_name}")


def start_all_playback(players: list[Player], state: ControllerState) -> None:
    for player in players:
        live_client(player).set_pause(False)
    invalidate_aligned_elapsed(state)
    render_status(players, state, "started")


def preseek_players(players: list[Player], start_times: list[float], state: ControllerState) -> None:
    for player, start_time in zip(players, start_times, strict=True):
        player.start_seconds = start_time
        if start_time > 0:
            seek_player_absolute(player, start_time)
        refresh_position(player)
    if any(start_time > 0 for start_time in start_times):
        render_status(players, state, "pre-seeked")


def seek_all(players: list[Player], state: ControllerState, seconds: float) -> None:
    selected = get_player(players, state.selected_index)
    if selected is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return

    aligned_elapsed_seconds = get_aligned_elapsed(players, state)
    if aligned_elapsed_seconds is None:
        render_status(players, state, f"video {selected.index} timestamp is unavailable")
        return

    requested_elapsed_seconds = aligned_elapsed_seconds + seconds
    min_elapsed_seconds = max(-player.timeline_base_seconds for player in players)
    target_elapsed_seconds = max(min_elapsed_seconds, requested_elapsed_seconds)
    actual_seconds = target_elapsed_seconds - aligned_elapsed_seconds
    set_aligned_elapsed(state, target_elapsed_seconds)
    for player in players:
        seek_player_absolute(player, player.timeline_base_seconds + target_elapsed_seconds)
        refresh_position(player)
        show_temporary_osd(player, format_osd_state(player, state, f"seek  {actual_seconds:+.3f}s"), ACTION_OSD_MS)
    render_status(players, state, f"seek all {actual_seconds:+.3f}s")


def sync_to_selected_time(players: list[Player], state: ControllerState) -> None:
    selected = get_player(players, state.selected_index)
    if selected is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return

    refresh_positions(players)
    if selected.position_seconds is None:
        render_status(players, state, f"video {selected.index} timestamp is unavailable")
        return

    selected_start_elapsed_seconds = selected.position_seconds - selected.start_seconds
    selected_base_elapsed_seconds = selected.position_seconds - selected.timeline_base_seconds
    for player in players:
        if player.index != selected.index:
            target_position_seconds = max(0.0, player.start_seconds + selected_start_elapsed_seconds)
            player.offset_seconds = target_position_seconds - player.start_seconds - selected_base_elapsed_seconds
            seek_player_absolute(player, target_position_seconds)
            refresh_position(player)
            show_temporary_osd(
                player,
                format_osd_state(player, state, f"sync {selected.index} {format_timestamp(selected.position_seconds)}"),
                ACTION_OSD_MS,
            )
        else:
            refresh_position(player)
    set_aligned_elapsed(state, selected_base_elapsed_seconds)
    render_status(players, state, f"synced all to video {selected.index} at {format_timestamp(selected.position_seconds)}")


def nudge_selected(players: list[Player], state: ControllerState, seconds: float) -> None:
    player = get_player(players, state.selected_index)
    if player is None:
        render_status(players, state, f"video {state.selected_index} is not loaded")
        return
    refresh_position(player)
    if player.position_seconds is None:
        render_status(players, state, f"video {player.index} timestamp is unavailable")
        return
    target_position_seconds = max(0.0, player.position_seconds + seconds)
    actual_seconds = target_position_seconds - player.position_seconds
    seek_player_absolute(player, target_position_seconds)
    player.shift_timeline_base(actual_seconds)
    refresh_position(player)
    set_aligned_elapsed_from_player(player, state)
    show_temporary_osd(
        player,
        format_osd_state(player, state, f"delta {actual_seconds:+.3f}s"),
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
        selected = get_player(players, state.selected_index)
        seek_all(players, state, -(selected.frame_seconds if selected and selected.frame_seconds else args.nudge_small))
    elif key == "seek_all_forward_xs":
        selected = get_player(players, state.selected_index)
        seek_all(players, state, selected.frame_seconds if selected and selected.frame_seconds else args.nudge_small)
    elif key == "seek_all_back_s":
        seek_all(players, state, -args.nudge_large)
    elif key == "seek_all_forward_s":
        seek_all(players, state, args.nudge_large)
    elif key == "seek_all_back_m":
        seek_all(players, state, -args.seek_medium)
    elif key == "seek_all_forward_m":
        seek_all(players, state, args.seek_medium)
    elif key == "seek_all_back_l":
        seek_all(players, state, -args.seek_small)
    elif key == "seek_all_forward_l":
        seek_all(players, state, args.seek_small)
    elif key == "seek_all_back_xl":
        seek_all(players, state, -args.seek_large)
    elif key == "seek_all_forward_xl":
        seek_all(players, state, args.seek_large)
    elif key == "sync_to_selected_time":
        sync_to_selected_time(players, state)
    elif key == "nudge_back_xs":
        nudge_selected_frame(players, state, -1, args.nudge_small)
    elif key == "nudge_forward_xs":
        nudge_selected_frame(players, state, 1, args.nudge_small)
    elif key == "nudge_back_s":
        nudge_selected(players, state, -args.nudge_large)
    elif key == "nudge_forward_s":
        nudge_selected(players, state, args.nudge_large)
    elif key == "nudge_back_m":
        nudge_selected(players, state, -args.seek_medium)
    elif key == "nudge_forward_m":
        nudge_selected(players, state, args.seek_medium)
    elif key == "nudge_back_l":
        nudge_selected(players, state, -args.seek_small)
    elif key == "nudge_forward_l":
        nudge_selected(players, state, args.seek_small)
    elif key == "volume_down_s":
        change_audio_volume(players, state, -VOLUME_STEP_SMALL)
    elif key == "volume_up_s":
        change_audio_volume(players, state, VOLUME_STEP_SMALL)
    elif key == "volume_down_l":
        change_audio_volume(players, state, -VOLUME_STEP_LARGE)
    elif key == "volume_up_l":
        change_audio_volume(players, state, VOLUME_STEP_LARGE)
    elif key == "volume_reset":
        change_audio_volume(players, state, None)
    elif key == "speed_down_s":
        change_speed(players, state, -SPEED_STEP_SMALL)
    elif key == "speed_up_s":
        change_speed(players, state, SPEED_STEP_SMALL)
    elif key == "speed_down_l":
        change_speed(players, state, -SPEED_STEP_LARGE)
    elif key == "speed_up_l":
        change_speed(players, state, SPEED_STEP_LARGE)
    elif key == "speed_reset":
        change_speed(players, state, None)
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
    volumes = collect_volumes(args)
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
                volume=volume,
            )
            for index, (video, geometry, volume) in enumerate(zip(args.videos, geometries, volumes, strict=True), start=1)
        ]

        for player in players:
            player.process = launch_mpv(
                player.video,
                player_title(player, state),
                player.geometry,
                player.socket_path,
                player.volume if player.index == state.audio_index else 0,
                screen,
            )

        try:
            for player in players:
                wait_for_socket(player.socket_path)
                player.client = MpvClient(player.socket_path)
            for player in players:
                wait_for_player_ready(player)
            refresh_frame_durations(players, args.nudge_small)
            preseek_players(players, start_times, state)
            update_titles(players, state)
            activate_audio(players, state, state.audio_index, flash=False)
            print_help()
            start_all_playback(players, state)
            pause_all_if_any_player_is_buffering(players, state)

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
                        if not pause_all_if_any_player_is_buffering(players, state):
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
            refresh_positions(players)
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
