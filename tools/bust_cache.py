"""给介绍页引用的 CSS / JS / 字体加上内容哈希：python tools/bust_cache.py

GitHub Pages 允许浏览器缓存文件 10 分钟。只改了 style.css 却没改网址时，
访客可能拿到新的 index.html + 旧的 style.css，页面就会错乱。
给网址加上 ?v=<内容哈希> 后，文件一变网址就变，不会再用到旧缓存。
修改 docs/ 里的 CSS / JS 后运行一次 (tools/subset_fonts.py 结束时也会自动运行)。
"""

import hashlib
import os
import re

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def short_hash(path):
    with open(os.path.join(DOCS, path), "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]


def stamp(text, pattern, root=""):
    """把 pattern 匹配到的文件引用改成 文件?v=哈希"""
    def sub(m):
        name = m.group(2)
        return f"{m.group(1)}{name}?v={short_hash(os.path.join(root, name))}{m.group(3)}"
    return re.sub(pattern, sub, text)


def main():
    # 1. fonts.css 里的 woff2 (先做，因为 fonts.css 自身的哈希会因此改变)
    fonts_css = os.path.join(DOCS, "fonts.css")
    if os.path.exists(fonts_css):
        with open(fonts_css, encoding="utf-8") as f:
            css = f.read()
        css = stamp(css, r'(url\(")(fonts/[\w.-]+\.woff2)(?:\?v=\w+)?(")')
        with open(fonts_css, "w", encoding="utf-8") as f:
            f.write(css)

    # 2. index.html 里的 css / js
    index = os.path.join(DOCS, "index.html")
    with open(index, encoding="utf-8") as f:
        html = f.read()
    html = stamp(html, r'((?:href|src)=")((?:style|fonts)\.css|main\.js)(?:\?v=\w+)?(")')
    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    print("cache-busted:", re.findall(r'(?:style|fonts)\.css\?v=\w+|main\.js\?v=\w+', html))


if __name__ == "__main__":
    main()
