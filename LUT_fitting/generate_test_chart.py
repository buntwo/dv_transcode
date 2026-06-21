#!/usr/bin/env python3
from PIL import Image, ImageDraw
import numpy as np

w, h = 1200, 700
img = Image.new("RGB", (w, h), "black")
draw = ImageDraw.Draw(img)

# Neutral gray ramp
for x in range(w):
    v = int(255 * x / (w - 1))
    draw.line([(x, 0), (x, 100)], fill=(v, v, v))

# RGB ramps
for x in range(w):
    v = int(255 * x / (w - 1))
    draw.line([(x, 120), (x, 190)], fill=(v, 0, 0))
    draw.line([(x, 200), (x, 270)], fill=(0, v, 0))
    draw.line([(x, 280), (x, 350)], fill=(0, 0, v))

# Color patches
patches = [
    ("black", (0, 0, 0)),
    ("dark gray", (32, 32, 32)),
    ("mid gray", (128, 128, 128)),
    ("white", (255, 255, 255)),
    ("red", (220, 40, 40)),
    ("green", (40, 180, 40)),
    ("blue", (40, 70, 220)),
    ("yellow", (220, 200, 40)),
    ("cyan", (40, 200, 220)),
    ("magenta", (220, 40, 200)),
    ("skin-ish 1", (190, 130, 95)),
    ("skin-ish 2", (150, 95, 70)),
]

x0, y0 = 40, 390
pw, ph = 90, 90
gap = 20

for i, (name, color) in enumerate(patches):
    x = x0 + i * (pw + gap)
    if x + pw > w:
        x = x0 + (i - 8) * (pw + gap)
        y = y0 + 140
    else:
        y = y0
    draw.rectangle([x, y, x + pw, y + ph], fill=color)
    draw.text((x, y + ph + 8), name, fill="white")

img.save("lut_sanity_chart.png")
