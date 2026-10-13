"""Generate app.ico for the memo app (kept for future rebuilds)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
OUT = BASE / "app.ico"

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",  # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",    # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",  # 黑体
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError("找不到中文字体")


def make_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(1, size // 32)
    draw.rounded_rectangle(
        [margin, margin, size - 1 - margin, size - 1 - margin],
        radius=int(size * 0.22), fill=(47, 111, 237, 255),
    )
    font = load_font(int(size * 0.60))
    text = "记"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top),
        text, font=font, fill=(255, 255, 255, 255),
    )
    return image


def main() -> None:
    master = make_icon(256)
    master.save(OUT, format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("saved", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
