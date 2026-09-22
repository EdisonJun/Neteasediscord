"""打包成单个 exe：python build.py  ->  dist/NeteaseDiscordRPC.exe"""

import os
import subprocess
import sys

from app import make_icon

ROOT = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(ROOT, "build", "icon.ico")


def main():
    os.makedirs(os.path.dirname(ICON), exist_ok=True)
    make_icon(256).save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "app.py",
        "--name", "NeteaseDiscordRPC",
        "--onefile", "--noconsole", "--clean", "--noconfirm",
        "--icon", ICON,
        "--hidden-import", "pystray._win32",
        "--collect-submodules", "comtypes",
        "--collect-submodules", "pycaw",
        # 用不到的大依赖 (Pillow / comtypes 会顺带拉进来)
        "--exclude-module", "numpy", "--exclude-module", "setuptools",
        "--exclude-module", "tkinter", "--exclude-module", "pkg_resources",
        "--exclude-module", "comtypes.test",
    ], cwd=ROOT)
    # 只输出 ASCII: GitHub Actions 的 Windows runner 默认代码页是 cp1252
    print("\nDone:", os.path.join(ROOT, "dist", "NeteaseDiscordRPC.exe"))


if __name__ == "__main__":
    main()
