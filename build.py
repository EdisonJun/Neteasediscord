"""打包成单个 exe：python build.py  ->  dist/NeteaseDiscordRPC.exe

设置环境变量 APP_VERSION (例如 GitHub Actions 里的 tag "v1.2.0") 时，
会先把版本号写进 _version.py，保证 exe 里显示的版本和 Release 一致。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
ICON = os.path.join(BUILD, "icon.ico")
VERSION_INFO = os.path.join(BUILD, "version_info.txt")


def resolve_version():
    tag = os.environ.get("APP_VERSION", "").strip()
    if tag:
        version = tag.lstrip("vV")
        with open(os.path.join(ROOT, "_version.py"), "w", encoding="utf-8") as f:
            f.write("# 由 build.py 根据 git tag 生成\n")
            f.write(f'VERSION = "{version}"\n')
        return version
    from _version import VERSION
    return VERSION


def write_version_info(version):
    """exe 的 "属性 -> 详细信息" 里显示的内容"""
    nums = (list(map(int, re.findall(r"\d+", version)[:4])) + [0, 0, 0, 0])[:4]
    t = tuple(nums)
    with open(VERSION_INFO, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'EdisonJun'),
      StringStruct('FileDescription', '网易云音乐 Discord 状态'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'NeteaseDiscordRPC'),
      StringStruct('LegalCopyright', 'GPL-3.0 · github.com/EdisonJun/Neteasediscord'),
      StringStruct('OriginalFilename', 'NeteaseDiscordRPC.exe'),
      StringStruct('ProductName', 'NeteaseDiscordRPC'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""")


def main():
    os.makedirs(BUILD, exist_ok=True)
    version = resolve_version()
    write_version_info(version)

    from app import make_icon
    make_icon(256).save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "app.py",
        "--name", "NeteaseDiscordRPC",
        "--onefile", "--noconsole", "--clean", "--noconfirm",
        "--icon", ICON,
        "--version-file", VERSION_INFO,
        "--add-data", os.path.join(ROOT, "assets", "icon.png") + os.pathsep + "assets",
        "--hidden-import", "pystray._win32",
        "--collect-submodules", "comtypes",
        "--collect-submodules", "pycaw",
        # 用不到的大依赖 (Pillow / comtypes 会顺带拉进来)
        "--exclude-module", "numpy", "--exclude-module", "setuptools",
        "--exclude-module", "tkinter", "--exclude-module", "pkg_resources",
        "--exclude-module", "comtypes.test",
    ], cwd=ROOT)
    # 只输出 ASCII: GitHub Actions 的 Windows runner 默认代码页是 cp1252
    print(f"\nDone: v{version} ->", os.path.join(ROOT, "dist", "NeteaseDiscordRPC.exe"))


if __name__ == "__main__":
    main()
