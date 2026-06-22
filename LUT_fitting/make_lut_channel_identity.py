#!/usr/bin/env python3
import argparse
from pathlib import Path


CHANNEL_INDEX = {
    "red": 0,
    "green": 1,
    "blue": 2,
}


def parse_cube(path):
    header = []
    values = []
    size = None
    domain_min = [0.0, 0.0, 0.0]
    domain_max = [1.0, 1.0, 1.0]

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            header.append(raw_line)
            continue

        parts = line.split()
        keyword = parts[0].upper()
        if keyword == "LUT_3D_SIZE":
            size = int(parts[1])
            header.append(raw_line)
        elif keyword == "DOMAIN_MIN":
            domain_min = [float(value) for value in parts[1:4]]
            header.append(raw_line)
        elif keyword == "DOMAIN_MAX":
            domain_max = [float(value) for value in parts[1:4]]
            header.append(raw_line)
        elif keyword in {"TITLE", "LUT_1D_SIZE"} or line.startswith("#"):
            header.append(raw_line)
        else:
            if len(parts) != 3:
                raise ValueError(f"Unexpected LUT row in {path}: {raw_line}")
            values.append([float(value) for value in parts])

    if size is None:
        raise ValueError(f"Missing LUT_3D_SIZE in {path}")
    expected = size ** 3
    if len(values) != expected:
        raise ValueError(f"Expected {expected} LUT rows, found {len(values)}")

    return header, values, size, domain_min, domain_max


def input_value_for_row(row_index, size, channel, domain_min, domain_max):
    r_index = row_index % size
    g_index = (row_index // size) % size
    b_index = row_index // (size * size)
    grid_index = [r_index, g_index, b_index][channel]
    if size == 1:
        t = 0.0
    else:
        t = grid_index / (size - 1)
    return domain_min[channel] + t * (domain_max[channel] - domain_min[channel])


def write_cube(path, header, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for line in header:
            f.write(f"{line}\n")
        for value in values:
            f.write(f"{value[0]:.8f} {value[1]:.8f} {value[2]:.8f}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Replace one output channel in a 3D .cube LUT with identity."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel", choices=sorted(CHANNEL_INDEX), required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.output.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite existing output: {args.output}")

    header, values, size, domain_min, domain_max = parse_cube(args.input)
    channel = CHANNEL_INDEX[args.channel]
    for row_index, value in enumerate(values):
        value[channel] = input_value_for_row(row_index, size, channel, domain_min, domain_max)

    write_cube(args.output, header, values)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
