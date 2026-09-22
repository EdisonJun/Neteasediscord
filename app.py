"""托盘程序入口。

    app.py                       正常启动 (托盘图标)
    app.py --enable-debug-port   (内部使用，由托盘以管理员身份调用) 修改网易云快捷方式
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading

import netease_rpc as core
import system

VERSION = "1.0.0"
HOMEPAGE = "https://github.com/EdisonJun/Neteasediscord"
log = core.log


# --------------------------------------------------------------------------
# 图标 (运行时用 Pillow 画，打包时 build.py 也用它生成 .ico)
# --------------------------------------------------------------------------
def make_icon(size=64, active=True):
    from PIL import Image, ImageDraw
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (233, 70, 70, 255) if active else (130, 130, 140, 255)
    d.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=bg)
    white = (255, 255, 255, 255)
    # 八分音符: 两个符头 + 符干 + 横梁
    d.ellipse((16 * s, 38 * s, 28 * s, 48 * s), fill=white)
    d.ellipse((36 * s, 34 * s, 48 * s, 44 * s), fill=white)
    d.rectangle((25 * s, 16 * s, 28 * s, 44 * s), fill=white)
    d.rectangle((45 * s, 12 * s, 48 * s, 40 * s), fill=white)
    d.polygon([(25 * s, 16 * s), (48 * s, 12 * s), (48 * s, 19 * s), (25 * s, 23 * s)], fill=white)
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


def elevated_enable_debug_port(port):
    ok, failed = system.enable_debug_port(port)
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
            Item(lambda item: self._status_discord(), None, enabled=False),
            Item(lambda item: self._status_song(), None, enabled=False),
            Item(lambda item: self._status_mode(), None, enabled=False),
            Menu.SEPARATOR,
            toggle("show_lyrics"),
            toggle("show_when_paused"),
            toggle("show_buttons"),
            Menu.SEPARATOR,
            Item("开机自动启动", self._toggle_autostart, checked=lambda item: system.autostart_enabled()),
            Item("开启精确进度 (网易云调试端口)…", self._enable_debug_port),
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
                "show_buttons": "显示「在网易云音乐中收听」按钮"}[key]

    def _status_discord(self):
        return "Discord：已连接" if self.presence.discord_ok else "Discord：未连接 (请打开 Discord 客户端)"

    def _status_song(self):
        return "正在显示：" + (self.presence.now_playing[:40] or "无")

    def _status_mode(self):
        return "进度：精确 (调试端口)" if self.presence.precise else "进度：估算 (未开启调试端口)"

    # ---- 动作 ----
    def _toggle_autostart(self, icon, item):
        system.set_autostart(not system.autostart_enabled())

    def _enable_debug_port(self, icon, item):
        threading.Thread(target=self._enable_debug_port_worker, daemon=True).start()

    def _enable_debug_port_worker(self):
        port = self.cfg["cdp_port"]
        if not port:
            system.message_box("config.json 里的 cdp_port 为 0，精确进度已被禁用。")
            return
        code = run_elevated_and_wait("--enable-debug-port", str(port))
        if code is None:
            # 没有管理员权限: 至少把不需要权限的部分改掉
            ok, _ = system.enable_debug_port(port)
            text = ("没有获得管理员权限，只修改了当前用户可以修改的快捷方式 "
                    f"({len(ok)} 个)。\n开始菜单里的网易云快捷方式可能仍然没有调试端口。\n\n")
        elif code != 0:
            text = "部分快捷方式修改失败 (见上一个提示)。\n\n"
        else:
            text = "已为网易云的快捷方式和开机自启加上调试端口。\n\n"
        if system.netease_running():
            if system.message_box(text + "需要重启网易云才能生效，现在重启吗？\n(会中断正在播放的歌曲)",
                                  flags=system.MB_YESNO | system.MB_ICONINFO) == system.IDYES:
                system.restart_netease(port)
        else:
            system.message_box(text + "下次打开网易云后就会使用精确进度。")

    def _quit(self, icon, item):
        self._stop.set()
        self.presence.stop()
        icon.stop()

    # ---- 刷新图标状态 ----
    def _watch(self):
        while not self._stop.wait(2):
            active = self.presence.discord_ok and bool(self.presence.now_playing)
            self.icon.icon = self.icons[active]
            title = self.presence.now_playing or "网易云 Discord 状态"
            self.icon.title = title[:120]

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
            self._first_run_tip()
        self.icon.run(setup=setup)

    def _run_presence(self):
        import comtypes
        comtypes.CoInitialize()  # 音频电平检测在这个线程里用 COM
        self.presence.run()


def main():
    args = sys.argv[1:]
    if args[:1] == ["--enable-debug-port"]:
        sys.exit(elevated_enable_debug_port(int(args[1]) if len(args) > 1 else 29222))

    if not system.acquire_single_instance():
        system.message_box("网易云 Discord 状态已经在运行了，请查看右下角托盘图标。")
        return
    core.setup_logging("-v" in args)
    log.info("版本 %s", VERSION)
    try:
        TrayApp().run()
    except Exception:
        log.exception("托盘程序异常退出")
        raise


if __name__ == "__main__":
    main()
