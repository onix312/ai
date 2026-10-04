"""Build the zone sign wordmark pre-composited onto its exact lavender rail."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "site" / "assets" / "brand"
SPEC = ROOT / "site" / "assets" / "print-layouts-v2.json"
PAGE = ROOT / "site" / "materials" / "таблички-зон.html"
SOURCE = BRAND / "nozza-selected-print-source.png"
DEST = BRAND / "nozza-zone-logo-lavender.png"
WORDMARK = (335, 253, 910, 326)
PADDING = 24


def _background_rgb() -> tuple[int, int, int]:
    css = PAGE.read_text(encoding="utf-8")
    match = re.search(r"\.zone \.side\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})", css)
    if not match:
        raise RuntimeError("Не найден точный цвет полосы таблички зоны")
    value = match.group(1).lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _estimate_edge_alpha(color: tuple[int, int, int], neighbor: tuple[int, int, int],
                         matte: tuple[int, int, int]) -> float | None:
    vector = [color[i] - matte[i] for i in range(3)]
    foreground = [neighbor[i] - matte[i] for i in range(3)]
    norm = sum(channel * channel for channel in foreground)
    if not norm:
        return None
    alpha = sum(vector[i] * foreground[i] for i in range(3)) / norm
    if not 0.04 < alpha < 0.96:
        return None
    residual = [vector[i] - alpha * foreground[i] for i in range(3)]
    if max(abs(channel) for channel in residual) > 12:
        return None
    return alpha


def build() -> Path:
    matte = (253, 248, 241)
    target = _background_rgb()
    crop_x, crop_y, width, height = WORDMARK
    with Image.open(SOURCE) as source_image:
        source = source_image.convert("RGB").crop(
            (crop_x, crop_y, crop_x + width, crop_y + height))
    mask_path = BRAND / (
        "nozza-print-crop-" + "-".join(str(value) for value in WORDMARK) + ".png")
    with Image.open(mask_path) as mask_image:
        alpha = mask_image.convert("RGBA").getchannel("A")
    src_pixels = list(source.get_flattened_data())
    mask_pixels = list(alpha.get_flattened_data())
    output = []

    for y in range(height):
        for x in range(width):
            index = y * width + x
            color = src_pixels[index]
            opacity = mask_pixels[index] / 255
            matte_distance = max(abs(color[c] - matte[c]) for c in range(3))

            # The selected source crop touches the nozzle at its left/top edge.
            # Recover partial pixel coverage where the binary mask kept a matte
            # antialias pixel fully opaque; this removes the visible cut line.
            if opacity == 1 and (x == 0 or y == 0 or x == width - 1 or y == height - 1):
                if 12 < matte_distance < 150:
                    neighbors = []
                    for ny in range(max(0, y - 1), min(height, y + 2)):
                        for nx in range(max(0, x - 1), min(width, x + 2)):
                            if nx == x and ny == y:
                                continue
                            candidate = src_pixels[ny * width + nx]
                            if max(abs(candidate[c] - matte[c]) for c in range(3)) > 150:
                                neighbors.append(candidate)
                    for neighbor in neighbors:
                        estimate = _estimate_edge_alpha(color, neighbor, matte)
                        if estimate is not None:
                            opacity = estimate
                            break

            # The pale ribbon separator is negative space in this logo. Match
            # it to the rail instead of leaving a cream seam against lavender.
            if opacity == 1 and matte_distance <= 10:
                output.append(target)
                continue
            if opacity <= 0.01:
                output.append(target)
                continue
            output.append(tuple(
                max(0, min(255, round(color[c] + (1 - opacity) * (target[c] - matte[c]))))
                for c in range(3)
            ))

    result = Image.new("RGB", (width + PADDING * 2, height + PADDING * 2), target)
    logo = Image.new("RGB", (width, height))
    logo.putdata(output)
    result.paste(logo, (PADDING, PADDING))
    result.save(DEST, optimize=True)
    return DEST


if __name__ == "__main__":
    print(build())
