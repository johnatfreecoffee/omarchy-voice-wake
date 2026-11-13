#!/usr/bin/env python3
"""Rebuild plugin/head.png from the official Grok Bot app icon.

The 512x512 grok-bot icon is a cropped close-up of the head (chin and left
side are clipped). This fills the missing disc from a least-squares fit of the
visible outer edge, inverts it to a black head on a transparent ground, and
pads it so the overlay can sit the whole head inside the white disc.
"""
from __future__ import annotations

import subprocess
import sys

# Algebraic least-squares fit of the visible outer edge of grok-bot.png
# (512 canvas): center (217.64, 380.15), radius 291.61. The circle extends
# 74 px left and 160 px below the canvas.
CX, CY, R = 217.64, 380.15, 291.61
OUT = 1024
MARGIN = 20


def read_rgba(path: str) -> tuple[int, int, bytes]:
    ident = subprocess.check_output(["magick", "identify", "-format", "%w %h", path], text=True)
    w, h = map(int, ident.split())
    raw = subprocess.check_output(["magick", path, "-alpha", "on", "-depth", "8", "rgba:-"])
    if len(raw) != w * h * 4:
        raise SystemExit(f"{path}: expected {w*h*4} rgba bytes, got {len(raw)}")
    return w, h, raw


def write_png(path: str, w: int, h: int, raw: bytes) -> None:
    subprocess.run(
        ["magick", "-depth", "8", "-size", f"{w}x{h}", "rgba:-", path],
        input=raw,
        check=True,
    )


def build(src_path: str, dest_path: str) -> None:
    sw, sh, src = read_rgba(src_path)
    if sw != 512 or sh != 512:
        # Still place by the same fitted circle, assuming the canonical 512 art
        # was just scaled. Map into 512 space.
        scale_in = 512.0 / sw
    else:
        scale_in = 1.0

    ss = 2
    ss_out = OUT * ss
    d = OUT - 2 * MARGIN
    ss_c = (ss_out - 1) / 2.0
    ss_scale = R / ((d * ss) / 2.0)
    half = (d * ss) / 2.0
    aa = float(ss)
    src_buf = memoryview(src)
    out_ss = bytearray(ss_out * ss_out * 4)

    def sample(sx: float, sy: float) -> tuple[float, float, float]:
        # Source is in 512-space. sx,sy already in 512-space.
        if scale_in != 1.0:
            px = sx / scale_in
            py = sy / scale_in
            pw, ph = sw, sh
        else:
            px, py, pw, ph = sx, sy, sw, sh
        if px < 0 or py < 0 or px > pw - 1 or py > ph - 1:
            return (255.0, 255.0, 255.0)  # missing disc = head fill (white, pre-invert)
        x0 = int(px)
        y0 = int(py)
        x1 = x0 + 1 if x0 + 1 < pw else x0
        y1 = y0 + 1 if y0 + 1 < ph else y0
        fx, fy = px - x0, py - y0

        def pix(x: int, y: int) -> tuple[int, int, int]:
            i = (y * pw + x) * 4
            return src_buf[i], src_buf[i + 1], src_buf[i + 2]

        r00, g00, b00 = pix(x0, y0)
        r10, g10, b10 = pix(x1, y0)
        r01, g01, b01 = pix(x0, y1)
        r11, g11, b11 = pix(x1, y1)
        r = r00 * (1 - fx) * (1 - fy) + r10 * fx * (1 - fy) + r01 * (1 - fx) * fy + r11 * fx * fy
        g = g00 * (1 - fx) * (1 - fy) + g10 * fx * (1 - fy) + g01 * (1 - fx) * fy + g11 * fx * fy
        b = b00 * (1 - fx) * (1 - fy) + b10 * fx * (1 - fy) + b01 * (1 - fx) * fy + b11 * fx * fy
        return r, g, b

    for y in range(ss_out):
        row = y * ss_out * 4
        for x in range(ss_out):
            sx = CX + (x - ss_c) * ss_scale
            sy = CY + (y - ss_c) * ss_scale
            dist = ((x - ss_c) ** 2 + (y - ss_c) ** 2) ** 0.5
            cover = (half + aa / 2.0 - dist) / aa
            if cover < 0:
                cover = 0.0
            elif cover > 1:
                cover = 1.0
            r, g, b = sample(sx, sy)
            # Invert RGB so the white head becomes black.
            i = row + x * 4
            out_ss[i] = int(255.0 - r + 0.5)
            out_ss[i + 1] = int(255.0 - g + 0.5)
            out_ss[i + 2] = int(255.0 - b + 0.5)
            out_ss[i + 3] = int(cover * 255.0 + 0.5)

    # Box-downsample 2x
    out = bytearray(OUT * OUT * 4)
    for y in range(OUT):
        for x in range(OUT):
            acc = [0, 0, 0, 0]
            for dy in range(ss):
                for dx in range(ss):
                    i = ((y * ss + dy) * ss_out + (x * ss + dx)) * 4
                    acc[0] += out_ss[i]
                    acc[1] += out_ss[i + 1]
                    acc[2] += out_ss[i + 2]
                    acc[3] += out_ss[i + 3]
            j = (y * OUT + x) * 4
            out[j] = (acc[0] + 2) // 4
            out[j + 1] = (acc[1] + 2) // 4
            out[j + 2] = (acc[2] + 2) // 4
            out[j + 3] = (acc[3] + 2) // 4

    write_png(dest_path, OUT, OUT, bytes(out))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.stderr.write(f"usage: {sys.argv[0]} grok-bot.png head.png\n")
        sys.exit(2)
    build(sys.argv[1], sys.argv[2])
