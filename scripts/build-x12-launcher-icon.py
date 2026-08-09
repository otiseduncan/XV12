"""Build the multi-resolution Windows launcher icon for XV12."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "assets" / "launcher"
PNG_PATH = OUTPUT_DIR / "x12-launcher.png"
ICO_PATH = OUTPUT_DIR / "x12-launcher.ico"
FONT_PATH = Path(r"C:\Windows\Fonts\bahnschrift.ttf")


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def build_icon(size: int = 1024) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = rounded_mask(size, int(size * 0.21))

    background = Image.new("RGBA", (size, size), (2, 11, 22, 255))
    pixels = background.load()
    for y in range(size):
        for x in range(size):
            dx = (x - size * 0.42) / size
            dy = (y - size * 0.34) / size
            glow = max(0.0, 1.0 - (dx * dx + dy * dy) ** 0.5 / 0.72)
            pixels[x, y] = (
                int(2 + 14 * glow),
                int(11 + 88 * glow),
                int(22 + 154 * glow),
                255,
            )
    canvas.paste(background, (0, 0), mask)

    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    inset = int(size * 0.075)
    glow_draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=int(size * 0.17),
        outline=(53, 232, 255, 185),
        width=int(size * 0.026),
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(size * 0.012))
    canvas.alpha_composite(glow_layer)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=int(size * 0.17),
        outline=(101, 234, 255, 255),
        width=int(size * 0.012),
    )

    font = ImageFont.truetype(str(FONT_PATH), int(size * 0.42))
    text = "X12"
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = (
        (size - text_width) / 2 - bbox[0],
        (size - text_height) / 2 - bbox[1] - size * 0.012,
    )

    text_glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(text_glow).text(
        position, text, font=font, fill=(53, 232, 255, 225), stroke_width=0
    )
    text_glow = text_glow.filter(ImageFilter.GaussianBlur(size * 0.025))
    canvas.alpha_composite(text_glow)
    draw.text(
        position,
        text,
        font=font,
        fill=(223, 252, 255, 255),
        stroke_width=max(1, int(size * 0.006)),
        stroke_fill=(7, 26, 45, 255),
    )

    canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", (size, size), 0), mask))
    return canvas


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    icon.resize((512, 512), Image.Resampling.LANCZOS).save(PNG_PATH)
    icon.save(
        ICO_PATH,
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
               (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(PNG_PATH)
    print(ICO_PATH)


if __name__ == "__main__":
    main()
