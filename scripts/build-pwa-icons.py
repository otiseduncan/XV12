from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "static" / "icons"


def icon(size: int) -> Image.Image:
    scale = 4
    dimension = size * scale
    ratio = dimension / 512
    gradient = Image.new("RGBA", (dimension, dimension), "#020b16")
    pixels = gradient.load()
    cx, cy, radius = 256 * ratio, 215 * ratio, 317 * ratio
    stops = ((0.0, (53, 232, 255)), (0.38, (22, 119, 210)), (1.0, (2, 11, 22)))
    for y in range(dimension):
        for x in range(dimension):
            value = min(1.0, math.hypot(x - cx, y - cy) / radius)
            left, right = stops[0], stops[-1]
            for index in range(1, len(stops)):
                if value <= stops[index][0]:
                    left, right = stops[index - 1], stops[index]
                    break
            amount = (value - left[0]) / max(0.0001, right[0] - left[0])
            pixels[x, y] = tuple(round(left[1][channel] + (right[1][channel] - left[1][channel]) * amount) for channel in range(3)) + (255,)
    circle_mask = Image.new("L", (dimension, dimension), 0)
    ImageDraw.Draw(circle_mask).ellipse(tuple(value * ratio for value in (82, 82, 430, 430)), fill=255)
    background = Image.new("RGBA", (dimension, dimension), "#020b16")
    image = Image.composite(gradient, background, circle_mask)
    draw = ImageDraw.Draw(image)
    def box(center: int, radius_value: int) -> tuple[float, float, float, float]:
        return tuple(value * ratio for value in (center - radius_value, center - radius_value, center + radius_value, center + radius_value))
    draw.ellipse(box(256, 174), outline="#65eaff", width=round(18 * ratio))
    draw.ellipse(box(256, 105), fill="#071a2d", outline="#8ff3ff", width=round(12 * ratio))
    points = [(156,151),(256,256),(156,361),(219,361),(287,289),(355,361),(418,361),(318,256),(418,151),(355,151),(287,223),(219,151)]
    draw.polygon([(x * ratio, y * ratio) for x, y in points], fill="#dffcff")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        icon(size).save(OUTPUT / f"xoduz-{size}.png", optimize=True)


if __name__ == "__main__":
    main()
