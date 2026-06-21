#!/usr/bin/env python3
"""Generate the home-screen / PWA icons for cc-hub.

Pure stdlib (zlib only) — no PIL/cairo needed. Renders a dark, rounded
"network hub" mark on a slate background: a central green node linked by
spokes to a ring of satellite nodes — the cc-hub motif (sessions gathering
at a hub). Writes PNGs at the sizes Android and iOS want plus a maskable
variant with safe-zone padding.

Run from the repo root:  python3 gen_icons.py
Outputs land in web/icons/.
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "web" / "icons"

# --- color helpers --------------------------------------------------------- #


def hexc(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


BG_TOP = hexc("#1b2330")
BG_BOT = hexc("#0d1117")
HUB = hexc("#3fb950")     # central node core (brand green)
NODE = hexc("#3fb950")    # satellite nodes
RINGC = hexc("#eef2f6")   # bright ring around the central node
SPOKE = hexc("#5b7088")   # connecting spokes (muted steel)


# --- geometry (all coords in the unit square, y points down) --------------- #

CENTER = (0.5, 0.5)
NODE_R_OUT = 0.33   # distance of satellite nodes from the center
NODES = [
    (
        CENTER[0] + NODE_R_OUT * math.cos(math.radians(-90 + 60 * k)),
        CENTER[1] + NODE_R_OUT * math.sin(math.radians(-90 + 60 * k)),
    )
    for k in range(6)
]


def in_circle(px, py, cx, cy, r):
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def in_rrect(px, py, x0, y0, x1, y1, r):
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def on_seg(px, py, ax, ay, bx, by, w):
    """True if (px,py) lies within w/2 of the segment a->b (rounded caps)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= (w * 0.5) ** 2


def sample(nx, ny, pad):
    """Return (r,g,b,a) for a point in the unit square. pad shrinks the art
    toward the center so the maskable variant survives a circular crop."""
    # Map into a padded sub-square so content sits in the safe zone.
    s = 1.0 - 2 * pad
    bx = (nx - pad) / s
    by = (ny - pad) / s

    # Background fills the whole tile (edge-to-edge), rounded corners.
    bg_on = in_rrect(nx, ny, 0.0, 0.0, 1.0, 1.0, 0.18)
    bg = lerp(BG_TOP, BG_BOT, ny)

    if bx < 0 or bx > 1 or by < 0 or by > 1:
        return (*bg, 255) if bg_on else (0, 0, 0, 0)

    px, py = bx, by
    cx, cy = CENTER

    # central hub node: green core wrapped in a bright ring — highest priority
    if in_circle(px, py, cx, cy, 0.150):
        if in_circle(px, py, cx, cy, 0.118):
            return (*HUB, 255)
        return (*RINGC, 255)
    # satellite nodes
    for ox, oy in NODES:
        if in_circle(px, py, ox, oy, 0.072):
            return (*NODE, 255)
    # spokes (center -> each satellite), drawn under the nodes
    for ox, oy in NODES:
        if on_seg(px, py, cx, cy, ox, oy, 0.030):
            return (*SPOKE, 255)

    return (*bg, 255) if bg_on else (0, 0, 0, 0)


# --- render + PNG encode --------------------------------------------------- #


def render(size, pad=0.0, ss=3):
    """Supersample at size*ss then box-downsample for anti-aliasing."""
    big = size * ss
    inv = 1.0 / big
    rows = []
    for Y in range(big):
        ny = (Y + 0.5) * inv
        row = []
        for X in range(big):
            nx = (X + 0.5) * inv
            row.append(sample(nx, ny, pad))
        rows.append(row)

    out = bytearray()
    area = ss * ss
    for y in range(size):
        out.append(0)  # filter type 0
        for x in range(size):
            r = g = b = a = 0
            for dy in range(ss):
                src = rows[y * ss + dy]
                base = x * ss
                for dx in range(ss):
                    pr, pg, pb, pa = src[base + dx]
                    # premultiply so transparent edges don't darken
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a == 0:
                out += bytes((0, 0, 0, 0))
            else:
                out += bytes((r // a, g // a, b // a, a // area))
    return bytes(out)


def write_png(path, size, raw):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, 0.0),
        ("icon-512.png", 512, 0.0),
        ("apple-touch-icon.png", 180, 0.0),
        # maskable: extra padding so a circular Android mask keeps the hub whole
        ("maskable-512.png", 512, 0.12),
    ]
    for name, size, pad in targets:
        print(f"rendering {name} ({size}px, pad={pad}) ...", flush=True)
        raw = render(size, pad=pad, ss=3)
        write_png(OUT / name, size, raw)
        print(f"  -> {OUT / name}")
    print("done")


if __name__ == "__main__":
    main()
