"""为 landing page 生成自托管的字体子集：python tools/subset_fonts.py

只包含 docs/ 页面里实际用到的字符，存到 docs/fonts/，并生成 docs/fonts.css。
这样访客不需要访问 Google Fonts (中国大陆无法访问)，中文字体也小得多。
修改了页面文字之后重新运行一次即可。
"""

import os
import re
import urllib.parse
import urllib.request
from html import unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "fonts")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")  # 让 Google 返回 woff2

# (family, css2 的 axis 参数, 本地文件名前缀)
FAMILIES = [
    ("Noto Serif SC", "wght@900", "noto-serif-sc"),  # 页面只用到 900
    ("Noto Sans SC", "wght@400;500;700", "noto-sans-sc"),
    ("Instrument Serif", "ital@0;1", "instrument-serif"),
]


def page_text():
    chars = set()
    for name in ("index.html", "main.js"):
        with open(os.path.join(DOCS, name), encoding="utf-8") as f:
            src = f.read()
        if name.endswith(".html"):
            src = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", src)
            src = unescape(re.sub(r"<[^>]+>", " ", src))
        chars.update(src)
    chars.update("0123456789:：·—–…♪⏸◉›→ ")  # 动态显示的时间、符号
    return "".join(sorted(c for c in chars if c.isprintable()))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        os.remove(os.path.join(OUT, old))
    text = page_text()
    css_out = ["/* 由 tools/subset_fonts.py 生成，不要手动修改 */"]
    total = 0
    for family, axes, prefix in FAMILIES:
        url = ("https://fonts.googleapis.com/css2?family=" + urllib.parse.quote(family) + ":" + axes
               + "&display=swap&text=" + urllib.parse.quote(text))
        css = fetch(url).decode("utf-8")
        for i, block in enumerate(re.findall(r"@font-face\s*{[^}]*}", css)):
            src_url = re.search(r"url\((https://[^)]+)\)", block).group(1)
            style = re.search(r"font-style:\s*(\w+)", block).group(1)
            weight = re.search(r"font-weight:\s*(\d+)", block).group(1)
            fname = f"{prefix}-{weight}{'-italic' if style == 'italic' else ''}.woff2"
            data = fetch(src_url)
            with open(os.path.join(OUT, fname), "wb") as f:
                f.write(data)
            total += len(data)
            css_out.append(
                "@font-face {\n"
                f'  font-family: "{family}"; font-style: {style}; font-weight: {weight};\n'
                f'  font-display: swap; src: url("fonts/{fname}") format("woff2");\n'
                "}")
            print(f"{fname}: {len(data) // 1024} KB")
    with open(os.path.join(DOCS, "fonts.css"), "w", encoding="utf-8") as f:
        f.write("\n".join(css_out) + "\n")
    print(f"{len(text)} chars, total {total // 1024} KB -> docs/fonts/, docs/fonts.css")


if __name__ == "__main__":
    main()
