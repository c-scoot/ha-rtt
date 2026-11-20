"""Rebuild the original integration icon (development dependency: Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / "custom_components" / "realtime_trains" / "brand"
    target.mkdir(exist_ok=True)
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    blue = "#087EA4"
    draw.rounded_rectangle((192, 92, 832, 784), radius=128, fill=blue)
    draw.rounded_rectangle((264, 220, 760, 512), radius=48, fill="#F4FBFF")
    draw.line((512, 240, 512, 492), fill=blue, width=24)
    for x in (328, 696):
        draw.ellipse((x - 44, 608, x + 44, 696), fill="#F4FBFF")
    draw.line((368, 756, 216, 936), fill=blue, width=52)
    draw.line((656, 756, 808, 936), fill=blue, width=52)
    draw.line((324, 848, 700, 848), fill=blue, width=36)
    draw.rounded_rectangle((416, 132, 608, 168), radius=16, fill="#F4FBFF")
    for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
        image.resize((size, size), Image.Resampling.LANCZOS).save(target / name)


if __name__ == "__main__":
    main()
