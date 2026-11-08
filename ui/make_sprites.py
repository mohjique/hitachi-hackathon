#!/usr/bin/env python3
"""
ui/make_sprites.py — cut the agent sprite sheet into one transparent PNG per agent/state.

The sheet is a 4 x 5 grid on a white background:
    rows    : coordinator (red diamond), planner (green box),
              dispatcher (yellow cube), inspector (blue blob)   <- order set in ROWS below
    columns : asleep, drowsy, awake, happy, working (with its tool)

Output: ui/sprites/<agent>_<pose>.png for the two poses the page uses (see POSES):
"idle" (column 2; column 1 for the dispatcher) and "working" (column 5).
Run once after changing the sheet:
    python ui/make_sprites.py                      # uses ui/assets/agent_sprite_sheet.jpeg
    python ui/make_sprites.py path/to/other_sheet.png
Needs Pillow (pip install pillow).
"""

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sprites")

# Sheet rows from top to bottom, and the column names from left to right.
ROWS = ["planner", "inspector", "dispatcher", "coordinator"]
COLS = ["asleep", "drowsy", "awake", "happy", "working"]
# Poses the page uses, and which sheet column each comes from:
#   idle    = not needed / standing by   -> column 2 ("drowsy"), except the dispatcher uses column 1
#   working = took part, holding its tool -> column 5
POSES = {"idle": "drowsy", "working": "working"}
IDLE_OVERRIDE = {"dispatcher": "asleep"}
HEIGHT = 180            # output height in px (shown at ~90 px, so sharp on retina screens)
SENTINEL = (255, 0, 255)


def runs(flags, min_gap):
    """Index ranges where flags is True, merging ranges separated by < min_gap."""
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append([start, i])
            start = None
    merged = []
    for r in out:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return merged


def main(sheet_path):
    sheet = Image.open(sheet_path).convert("RGB")
    W, H = sheet.size

    # 1) Make the white background transparent: flood-fill it from the corners.
    filled = sheet.copy()
    for corner in [(0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)]:
        if filled.getpixel(corner) != SENTINEL:
            ImageDraw.floodfill(filled, corner, SENTINEL, thresh=22)
    px = filled.load()
    src = sheet.load()
    rgba = Image.new("RGBA", (W, H))
    out = rgba.load()
    for y in range(H):
        for x in range(W):
            r, g, b = src[x, y]
            # also clear enclosed background (e.g. between legs): near-pure, colourless white
            enclosed_white = min(r, g, b) >= 238 and max(r, g, b) - min(r, g, b) <= 10
            out[x, y] = (0, 0, 0, 0) if px[x, y] == SENTINEL or enclosed_white else (r, g, b, 255)

    # 2) Find the rows and columns of sprites from where the opaque pixels are.
    alpha = rgba.getchannel("A")
    a = alpha.load()
    row_has = [any(a[x, y] for x in range(0, W, 3)) for y in range(H)]
    # rows are ~30 px apart at the closest; sprite rows are ~300 px tall, so anything much
    # shorter (e.g. handwritten column numbers above the grid) is ignored
    row_bands = [r for r in runs(row_has, 15) if r[1] - r[0] > 200]
    if len(row_bands) != len(ROWS):
        sys.exit(f"expected {len(ROWS)} rows of sprites, found {len(row_bands)}")

    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):                 # drop poses we no longer use
        if old.endswith(".png"):
            os.remove(os.path.join(OUT, old))
    for agent, (y0, y1) in zip(ROWS, row_bands):
        col_has = [any(a[x, y] for y in range(y0, y1, 3)) for x in range(W)]
        bands = [c for c in runs(col_has, 60) if c[1] - c[0] > 40]
        if len(bands) != len(COLS):
            sys.exit(f"{agent}: expected {len(COLS)} sprites, found {len(bands)}")
        columns = dict(zip(COLS, bands))
        for pose, column in POSES.items():
            if pose == "idle":
                column = IDLE_OVERRIDE.get(agent, column)
            x0, x1 = columns[column]
            crop = rgba.crop((x0, y0, x1, y1))
            crop = crop.crop(crop.getbbox())
            scale = HEIGHT / crop.height
            crop = crop.resize((max(1, round(crop.width * scale)), HEIGHT), Image.LANCZOS)
            path = os.path.join(OUT, f"{agent}_{pose}.png")
            crop.save(path, optimize=True)
            print(f"{path}  (column {COLS.index(column) + 1}, {column})  {crop.size[0]}x{crop.size[1]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "assets", "agent_sprite_sheet.jpeg"))
