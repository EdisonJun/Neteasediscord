"""生成分享卡片 docs/og-image.png (1200x630)：python tools/make_og_image.py

链接分享到 Discord / 微信 / 社交媒体时显示的预览图。
"""

import io
import os
import re
import sys
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import make_icon  # noqa: E402

W, H = 1200, 630
TITLE = "网易云 Discord 状态"
TAGLINE = "让好友听见你在听什么。"
POINTS = "实时歌词 · 专辑封面 · 暂停与进度同步 · 免费开源"
URL = "edisonjun.github.io/Neteasediscord"


def google_ttf(family, weight, text):
    """只含 text 里字符的 TTF (用最简单的 UA 请求时 Google 返回 TTF，Pillow 能直接读)"""
    css_url = ("https://fonts.googleapis.com/css2?family=" + urllib.parse.quote(family)
               + f":wght@{weight}&text=" + urllib.parse.quote(text))
    css = urllib.request.urlopen(urllib.request.Request(css_url, headers={"User-Agent": "Mozilla/5.0"}),
                                 timeout=30).read().decode()
    font_url = re.search(r"url\((https://[^)]+)\)", css).group(1)
    return io.BytesIO(urllib.request.urlopen(font_url, timeout=30).read())


def glow(color, center, radius):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((center[0] - radius, center[1] - radius,
                                   center[0] + radius, center[1] + radius), fill=color)
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.55))


def main():
    img = Image.new("RGBA", (W, H), (10, 10, 12, 255))
    img = Image.alpha_composite(img, glow((255, 69, 69, 110), (1010, 140), 300))
    img = Image.alpha_composite(img, glow((88, 101, 242, 120), (180, 600), 320))

    icon = make_icon(220)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 170), (96, 222, 96 + 220, 222 + 220), icon.getchannel("A"))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(18)))
    img.alpha_composite(icon, (90, 205))

    serif = ImageFont.truetype(google_ttf("Noto Serif SC", 900, TITLE + TAGLINE), 76)
    tag_font = ImageFont.truetype(google_ttf("Noto Serif SC", 900, TAGLINE), 44)
    sans = ImageFont.truetype(google_ttf("Noto Sans SC", 500, POINTS + URL), 26)

    d = ImageDraw.Draw(img)
    x = 360
    d.text((x, 215), TITLE, font=serif, fill=(245, 245, 247))
    d.text((x, 322), TAGLINE, font=tag_font, fill=(255, 110, 110))
    d.text((x, 400), POINTS, font=sans, fill=(161, 161, 168))
    d.text((x, 520), URL, font=sans, fill=(107, 107, 115))

    out = os.path.join(ROOT, "docs", "og-image.png")
    img.convert("RGB").save(out, optimize=True)
    print("saved", out)


if __name__ == "__main__":
    main()
