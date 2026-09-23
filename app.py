"""托盘程序入口。

    app.py                       正常启动 (托盘图标)
    app.py --enable-debug-port   (内部使用，由托盘以管理员身份调用) 修改网易云快捷方式
    app.py --disable-debug-port  (内部使用) 还原网易云快捷方式
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import threading
import urllib.request

import netease_rpc as core
import system
from _version import VERSION

HOMEPAGE = "https://github.com/EdisonJun/Neteasediscord"
LATEST_RELEASE_API = "https://api.github.com/repos/EdisonJun/Neteasediscord/releases/latest"
log = core.log


# --------------------------------------------------------------------------
# 更新检查
# --------------------------------------------------------------------------
def version_tuple(v):
    """'v1.2.10' -> (1, 2, 10)；无法解析的部分当作 0"""
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def latest_release():
    """返回 (tag, 网页地址)，失败返回 None"""
    try:
        req = urllib.request.Request(LATEST_RELEASE_API, headers={
            "User-Agent": f"NeteaseDiscordRPC/{VERSION}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        return data["tag_name"], data["html_url"]
    except Exception as e:
        log.debug("检查更新失败: %s", e)
        return None


# --------------------------------------------------------------------------
# 图标: assets/icon.png (1024px 透明底)。打包后位于 PyInstaller 的解压目录
# --------------------------------------------------------------------------
ICON_PATH = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                         "assets", "icon.png")


def make_icon(size=64, active=True):
    """active=False (没有在播放) 时返回灰度版本"""
    from PIL import Image, ImageEnhance
    img = Image.open(ICON_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
    if not active:
        alpha = img.getchannel("A")
        img = ImageEnhance.Brightness(img.convert("L")).enhance(0.8).convert("RGBA")
        img.putalpha(alpha)
    return img


# --------------------------------------------------------------------------
# 以管理员身份执行并等待结束
# --------------------------------------------------------------------------
class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


def run_elevated_and_wait(*args):
    """返回子进程退出码；用户在 UAC 里点了"否"返回 None"""
    exe, params = system.self_command(*args)
    info = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=0x40,  # NOCLOSEPROCESS
                             lpVerb="runas", lpFile=exe, lpParameters=params, nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wt.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value


def elevated_set_debug_port(port):
    """port 为 None 时还原 (去掉调试端口)"""
    ok, failed = system.enable_debug_port(port) if port else system.disable_debug_port()
    if failed:
        system.message_box("以下快捷方式修改失败：\n\n" + "\n".join(failed), flags=system.MB_ICONWARNING)
        return 1
    return 0


# --------------------------------------------------------------------------
# 托盘
# --------------------------------------------------------------------------
class TrayApp:
    def __init__(self):
        import pystray
        self.pystray = pystray
        self.cfg = core.load_config()
        self.presence = core.Presence(self.cfg)
        self.icons = {True: make_icon(64, True), False: make_icon(64, False)}
        self.icon = pystray.Icon(system.APP_NAME, self.icons[False], "网易云 Discord 状态",
                                 menu=self._menu())
        self._stop = threading.Event()
        self.update = None  # (tag, url)：发现的新版本

    # ---- 菜单 ----
    def _menu(self):
        Item, Menu = self.pystray.MenuItem, self.pystray.Menu

        def toggle(key):
            def action(icon, item):
                self.cfg[key] = not self.cfg[key]
                core.save_config(self.cfg)
                self.presence.refresh()
            return Item(self._label(key), action, checked=lambda item: bool(self.cfg[key]))

        return Menu(
            Item(lambda item: f"⬆ 发现新版本 {self.update[0]}，点击下载" if self.update else "",
                 lambda: os.startfile(self.update[1]), visible=lambda item: bool(self.update)),
            Item(lambda item: self._status_discord(), None, enabled=False),
            Item(lambda item: self._status_song(), None, enabled=False),
            Item(lambda item: self._status_mode(), None, enabled=False),
            Menu.SEPARATOR,
            toggle("show_lyrics"),
            toggle("show_when_paused"),
            toggle("show_buttons"),
            toggle("show_download_button"),
            Menu.SEPARATOR,
            Item("开机自动启动", self._toggle_autostart, checked=lambda item: system.autostart_enabled()),
            Item("开启精确进度 (网易云调试端口)…", self._set_debug_port(True)),
            Item("关闭精确进度 (还原网易云快捷方式)…", self._set_debug_port(False)),
            Menu.SEPARATOR,
            Item("打开配置文件", lambda: os.startfile(core.CONFIG_PATH)),
            Item("打开日志", lambda: os.startfile(core.LOG_PATH)),
            Item(f"项目主页 (v{VERSION})", lambda: os.startfile(HOMEPAGE), visible=bool(HOMEPAGE)),
            Menu.SEPARATOR,
            Item("退出", self._quit),
        )

    @staticmethod
    def _label(key):
        return {"show_lyrics": "显示歌词",
                "show_when_paused": "暂停时保留状态",
                "show_buttons": "显示「在网易云音乐中收听」按钮",
                "show_download_button": "显示「下载插件」按钮"}[key]

    def _status_discord(self):
        return "Discord：已连接" if self.presence.discord_ok else "Discord：未连接 (请打开 Discord 客户端)"

    def _status_song(self):
        return "正在显示：" + (self.presence.now_playing[:40] or "无")

    def _status_mode(self):
        return "进度：精确 (调试端口)" if self.presence.precise else "进度：估算 (未开启调试端口)"

    # ---- 动作 ----
    def _toggle_autostart(self, icon, item):
        system.set_autostart(not system.autostart_enabled())

    def _set_debug_port(self, enable):
        def action(icon, item):
            threading.Thread(target=self._debug_port_worker, args=(enable,), daemon=True).start()
        return action

    def _debug_port_worker(self, enable):
        port = self.cfg["cdp_port"] if enable else None
        if enable and not port:
            system.message_box("config.json 里的 cdp_port 为 0，精确进度已被禁用。")
            return
        code = run_elevated_and_wait(*(["--enable-debug-port", str(port)] if enable else ["--disable-debug-port"]))
        what = "加上调试端口" if enable else "去掉调试端口"
        if code is None:
            # 没有管理员权限: 至少把不需要权限的部分改掉
            ok, _ = system.enable_debug_port(port) if enable else system.disable_debug_port()
            text = (f"没有获得管理员权限，只为当前用户可以修改的快捷方式{what} ({len(ok)} 个)。\n"
                    "开始菜单里的网易云快捷方式可能没有改到。\n\n")
        elif code != 0:
            text = "部分快捷方式修改失败 (见上一个提示)。\n\n"
        else:
            text = f"已为网易云的快捷方式和开机自启{what}。\n\n"
        if system.netease_running():
            if system.message_box(text + "需要重启网易云才能生效，现在重启吗？\n(会中断正在播放的歌曲)",
                                  flags=system.MB_YESNO | system.MB_ICONINFO) == system.IDYES:
                system.restart_netease(port)
        else:
            system.message_box(text + "下次打开网易云时生效。")

    def _quit(self, icon, item):
        self._stop.set()
        self.presence.stop()
        icon.stop()

    # ---- 刷新图标状态 ----
    def _watch(self):
        shown = (None, None)
        while not self._stop.wait(2):
            active = self.presence.discord_ok and bool(self.presence.now_playing)
            title = (self.presence.now_playing or "网易云 Discord 状态")[:120]
            if (active, title) != shown:  # 只在变化时更新，避免反复刷新托盘
                self.icon.icon = self.icons[active]
                self.icon.title = title
                shown = (active, title)

    def _wait_takeover(self):
        """另一个副本 (通常是刚下载的新版本) 请求接替时，正常退出"""
        event = system.create_quit_event()
        while not self._stop.is_set():
            if system.wait_quit_event(event, 1000):
                log.info("新启动的副本接替了运行，本副本退出")
                self._quit(self.icon, None)
                return

    def _check_update(self):
        found = latest_release()
        if found and version_tuple(found[0]) > version_tuple(VERSION):
            self.update = found
            log.info("发现新版本 %s", found[0])
            self.icon.update_menu()
            try:
                self.icon.notify(f"网易云 Discord 状态有新版本 {found[0]}，右键托盘图标即可下载。",
                                 "发现新版本")
            except Exception:
                pass

    def _first_run_tip(self):
        if self.cfg.get("first_run_done"):
            return
        self.cfg["first_run_done"] = True
        core.save_config(self.cfg)
        done, total = system.debug_port_status(self.cfg["cdp_port"])
        tip = "已在后台运行，右键托盘图标可以设置。"
        if total and done < total:
            tip += "\n建议点「开启精确进度」，拖动进度条后歌词也能同步。"
        try:
            self.icon.notify(tip, "网易云 Discord 状态")
        except Exception:
            pass

    def run(self):
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._run_presence, daemon=True).start()
            threading.Thread(target=self._watch, daemon=True).start()
            threading.Thread(target=self._wait_takeover, daemon=True).start()
            if self.cfg.get("check_updates", True):
                threading.Thread(target=self._check_update, daemon=True).start()
            self._first_run_tip()
        self.icon.run(setup=setup)

    def _run_presence(self):
        import comtypes
        comtypes.CoInitialize()  # 音频电平检测在这个线程里用 COM
        self.presence.run()


def main():
    args = sys.argv[1:]
    if args[:1] == ["--enable-debug-port"]:
        sys.exit(elevated_set_debug_port(int(args[1]) if len(args) > 1 else 29222))
    if args[:1] == ["--disable-debug-port"]:
        sys.exit(elevated_set_debug_port(None))

    if not system.acquire_single_instance():
        # 常见情况：下载了新版本直接双击，而旧版本还在托盘里运行
        answer = system.message_box(
            "网易云 Discord 状态已经在运行了 (右下角托盘图标)。\n\n"
            f"要关闭正在运行的那个，改用现在打开的这个 (v{VERSION}) 吗？",
            flags=system.MB_YESNO | system.MB_ICONINFO)
        if answer != system.IDYES:
            return
        if not system.take_over_running_instance():
            system.message_box("没能关闭正在运行的程序。请右键托盘图标选择「退出」，再重新打开。",
                               flags=system.MB_ICONWARNING)
            return
    core.setup_logging("-v" in args)
    log.info("版本 %s", VERSION)
    try:
        if system.repair_autostart():
            log.info("程序位置变了，已更新开机自启的路径")
    except OSError as e:
        log.warning("更新开机自启路径失败: %s", e)
    try:
        TrayApp().run()
    except Exception:
        log.exception("托盘程序异常退出")
        raise


if __name__ == "__main__":
    main()
