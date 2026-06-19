#!/usr/bin/env python3
"""Side-by-side synchronized multi-player video control using mpv."""

from __future__ import annotations

import argparse
import json
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
DEFAULT_GAP = 24
DEFAULT_X = 40
DEFAULT_Y = 80
DEFAULT_SEEK_SMALL = 5.0
DEFAULT_SEEK_LARGE = 30.0
DEFAULT_NUDGE_SMALL = 0.0333667
DEFAULT_NUDGE_LARGE = 0.5
DEFAULT_AUDIO_STEP = 0.05


@dataclass(frozen=True)
class WindowGeometry:
    width: int
    height: int
    x: int
    y: int

    def mpv_value(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


@dataclass(frozen=True)
class SideBySideGeometry:
    left: WindowGeometry
    right: WindowGeometry


@dataclass
class OffsetState:
    left_seconds: float = 0.0
    right_seconds: float = 0.0

    @property
    def relative_seconds(self) -> float:
        return self.right_seconds - self.left_seconds

    def nudge_left(self, seconds: float) -> None:
        self.left_seconds += seconds

    def nudge_right(self, seconds: float) -> None:
        self.right_seconds += seconds


@dataclass
class AudioMix:
    pan: float = 0.5
    step: float = DEFAULT_AUDIO_STEP

    def move_left(self) -> None:
        self.pan = clamp(self.pan - self.step, 0.0, 1.0)

    def move_right(self) -> None:
        self.pan = clamp(self.pan + self.step, 0.0, 1.0)

    @property
    def left_volume(self) -> int:
        return round((1.0 - self.pan) * 100)

    @property
    def right_volume(self) -> int:
        return round(self.pan * 100)


class MpvClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def command(self, *parts: Any) -> dict[str, Any]:
        payload = json.dumps({"command": list(parts)}).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(self.socket_path))
            sock.sendall(payload)
            response = sock.recv(65536)
        if not response:
            return {}
        return json.loads(response.decode("utf-8"))

    def seek(self, seconds: float) -> None:
        self.command("seek", seconds, "relative", "exact")

    def set_volume(self, volume: int) -> None:
        self.command("set_property", "volume", volume)

    def get_pause(self) -> bool:
        response = self.command("get_property", "pause")
        return bool(response.get("data"))

    def set_pause(self, pause: bool) -> None:
        self.command("set_property", "pause", pause)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def calculate_geometry(width: int, height: int, gap: int, x: int, y: int) -> SideBySideGeometry:
    left = WindowGeometry(width=width, height=height, x=x, y=y)
    right = WindowGeometry(width=width, height=height, x=x + width + gap, y=y)
    return SideBySideGeometry(left=left, right=right)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open two videos side by side in mpv and control them together.",
    )
    parser.add_argument("left_video", type=Path)
    parser.add_argument("right_video", type=Path)
    parser.add_argument("--width", type=positive_int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=positive_int, default=DEFAULT_HEIGHT)
    parser.add_argument("--gap", type=non_negative_int, default=DEFAULT_GAP)
    parser.add_argument("--x", type=non_negative_int, default=DEFAULT_X)
    parser.add_argument("--y", type=non_negative_int, default=DEFAULT_Y)
    parser.add_argument("--seek-small", type=positive_float, default=DEFAULT_SEEK_SMALL)
    parser.add_argument("--seek-large", type=positive_float, default=DEFAULT_SEEK_LARGE)
    parser.add_argument("--nudge-small", type=positive_float, default=DEFAULT_NUDGE_SMALL)
    parser.add_argument("--nudge-large", type=positive_float, default=DEFAULT_NUDGE_LARGE)
    parser.add_argument("--audio-step", type=positive_float, default=DEFAULT_AUDIO_STEP)
    return parser.parse_args(argv)


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


def validate_inputs(args: argparse.Namespace) -> None:
    for label, path in (("left", args.left_video), ("right", args.right_video)):
        if not path.exists():
            raise ValueError(f"{label} video does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"{label} video is not a file: {path}")
    if shutil.which("mpv") is None:
        raise ValueError("mpv is required; install with: brew install mpv")


def launch_mpv(video: Path, title: str, geometry: WindowGeometry, ipc_socket: Path, volume: int) -> subprocess.Popen[bytes]:
    cmd = [
        "mpv",
        "--no-terminal",
        f"--input-ipc-server={ipc_socket}",
        f"--geometry={geometry.mpv_value()}",
        f"--title={title}",
        f"--volume={volume}",
        str(video),
    ]
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
    if sequence in (b"q", b"\x03"):
        return "quit"
    if sequence == b"c":
        return "left_nudge_back"
    if sequence == b"v":
        return "left_nudge_forward"
    if sequence == b"C":
        return "left_nudge_back_large"
    if sequence == b"V":
        return "left_nudge_forward_large"
    if sequence == b"m":
        return "right_nudge_back"
    if sequence == b",":
        return "right_nudge_forward"
    if sequence == b"M":
        return "right_nudge_back_large"
    if sequence == b"<":
        return "right_nudge_forward_large"
    if sequence == b"g":
        return "audio_left"
    if sequence == b"h":
        return "audio_right"
    if sequence == b"0":
        return "mute"
    if sequence in (b"\x1b[D", b"\x1bOD"):
        return "seek_back"
    if sequence in (b"\x1b[C", b"\x1bOC"):
        return "seek_forward"
    if sequence in (b"\x1b[1;2D", b"\x1b[2D"):
        return "seek_back_large"
    if sequence in (b"\x1b[1;2C", b"\x1b[2C"):
        return "seek_forward_large"
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
        if sequence.endswith((b"C", b"D")):
            break
    return normalize_key(bytes(sequence))


def print_help() -> None:
    print(
        "\n".join(
            [
                "Controls:",
                "  space        play/pause both",
                "  arrows       seek both backward/forward",
                "  shift+arrows seek both backward/forward by larger step",
                "  c/v          nudge left backward/forward",
                "  C/V          nudge left backward/forward by larger amount",
                "  m/,          nudge right backward/forward",
                "  M/<          nudge right backward/forward by larger amount",
                "  g/h          pan audio mix left/right",
                "  0            toggle mute for both",
                "  q            quit",
                "",
            ]
        )
    )


def print_offset_summary(offsets: OffsetState) -> None:
    print("Final offset:")
    print(f"  left nudged:     {offsets.left_seconds:+.3f}s")
    print(f"  right nudged:    {offsets.right_seconds:+.3f}s")
    print(f"  right - left:    {offsets.relative_seconds:+.3f}s")


def set_both_pause(left: MpvClient, right: MpvClient) -> None:
    next_pause = not left.get_pause()
    left.set_pause(next_pause)
    right.set_pause(next_pause)
    print("paused" if next_pause else "playing")


def seek_both(left: MpvClient, right: MpvClient, seconds: float) -> None:
    left.seek(seconds)
    right.seek(seconds)
    print(f"seek both {seconds:+.3f}s")


def set_mix(left: MpvClient, right: MpvClient, mix: AudioMix) -> None:
    left.set_volume(mix.left_volume)
    right.set_volume(mix.right_volume)
    print(f"audio mix left {mix.left_volume}% / right {mix.right_volume}%")


def toggle_mute(left: MpvClient, right: MpvClient) -> None:
    left.command("cycle", "mute")
    right.command("cycle", "mute")
    print("toggled mute")


def handle_key(
    key: str,
    left: MpvClient,
    right: MpvClient,
    offsets: OffsetState,
    mix: AudioMix,
    args: argparse.Namespace,
) -> bool:
    if key == "quit":
        return False
    if key == "space":
        set_both_pause(left, right)
    elif key == "seek_back":
        seek_both(left, right, -args.seek_small)
    elif key == "seek_forward":
        seek_both(left, right, args.seek_small)
    elif key == "seek_back_large":
        seek_both(left, right, -args.seek_large)
    elif key == "seek_forward_large":
        seek_both(left, right, args.seek_large)
    elif key == "left_nudge_back":
        left.seek(-args.nudge_small)
        offsets.nudge_left(-args.nudge_small)
        print(f"left offset {offsets.left_seconds:+.3f}s")
    elif key == "left_nudge_forward":
        left.seek(args.nudge_small)
        offsets.nudge_left(args.nudge_small)
        print(f"left offset {offsets.left_seconds:+.3f}s")
    elif key == "left_nudge_back_large":
        left.seek(-args.nudge_large)
        offsets.nudge_left(-args.nudge_large)
        print(f"left offset {offsets.left_seconds:+.3f}s")
    elif key == "left_nudge_forward_large":
        left.seek(args.nudge_large)
        offsets.nudge_left(args.nudge_large)
        print(f"left offset {offsets.left_seconds:+.3f}s")
    elif key == "right_nudge_back":
        right.seek(-args.nudge_small)
        offsets.nudge_right(-args.nudge_small)
        print(f"right offset {offsets.right_seconds:+.3f}s")
    elif key == "right_nudge_forward":
        right.seek(args.nudge_small)
        offsets.nudge_right(args.nudge_small)
        print(f"right offset {offsets.right_seconds:+.3f}s")
    elif key == "right_nudge_back_large":
        right.seek(-args.nudge_large)
        offsets.nudge_right(-args.nudge_large)
        print(f"right offset {offsets.right_seconds:+.3f}s")
    elif key == "right_nudge_forward_large":
        right.seek(args.nudge_large)
        offsets.nudge_right(args.nudge_large)
        print(f"right offset {offsets.right_seconds:+.3f}s")
    elif key == "audio_left":
        mix.move_left()
        set_mix(left, right, mix)
    elif key == "audio_right":
        mix.move_right()
        set_mix(left, right, mix)
    elif key == "mute":
        toggle_mute(left, right)
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
    geometry = calculate_geometry(args.width, args.height, args.gap, args.x, args.y)
    offsets = OffsetState()
    mix = AudioMix(step=args.audio_step)
    children: list[subprocess.Popen[bytes]] = []

    with tempfile.TemporaryDirectory(prefix="multi_player_") as tmp:
        tmp_path = Path(tmp)
        left_socket = tmp_path / "left.sock"
        right_socket = tmp_path / "right.sock"
        children = [
            launch_mpv(args.left_video, f"LEFT: {args.left_video.name}", geometry.left, left_socket, mix.left_volume),
            launch_mpv(args.right_video, f"RIGHT: {args.right_video.name}", geometry.right, right_socket, mix.right_volume),
        ]

        try:
            wait_for_socket(left_socket)
            wait_for_socket(right_socket)
            left = MpvClient(left_socket)
            right = MpvClient(right_socket)
            print_help()

            old_term = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                while all(child.poll() is None for child in children):
                    readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if not readable:
                        continue
                    key = read_key()
                    if key is not None and not handle_key(key, left, right, offsets, mix, args):
                        break
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)
                print()
        finally:
            for child in children:
                terminate_process(child)
            print_offset_summary(offsets)

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
