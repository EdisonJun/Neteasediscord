"""Windows 相关的系统操作：开机自启、给网易云开启调试端口、提权、单实例、消息框。"""

import ctypes
import glob
import os
import subprocess
import sys
import winreg

APP_NAME = "NeteaseDiscordRPC"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NETEASE_EXE = "cloudmusic.exe"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MB_OK, MB_YESNO, MB_ICONINFO, MB_ICONWARNING, IDYES = 0x0, 0x4, 0x40, 0x30, 6


def message_box(text, title=APP_NAME, flags=MB_OK | MB_ICONINFO):
    return user32.MessageBoxW(None, text, title, flags)


# --------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------
_mutex = None


def acquire_single_instance():
    """已有实例在运行时返回 False"""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Local\\" + APP_NAME)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


# --------------------------------------------------------------------------
# 当前程序的启动命令 (打包成 exe 与直接跑 python 两种情况)
# --------------------------------------------------------------------------
def self_command(*args):
    """返回 (可执行文件, 参数字符串)"""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, []
    else:
        # 用 pythonw 启动，避免出现控制台窗口
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        params = [os.path.abspath(sys.argv[0])]
    return exe, subprocess.list2cmdline(params + list(args))


# --------------------------------------------------------------------------
# 开机自启 (HKCU\...\Run，不需要管理员权限)
# --------------------------------------------------------------------------
def _autostart_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except OSError:
        return None


def _autostart_command():
    exe, params = self_command()
    return f'"{exe}" {params}'.strip()


def autostart_enabled():
    return _autostart_value() is not None


def _registered_exe(value):
    """从 '"C:\\path\\app.exe" args' 里取出 exe 路径"""
    value = value.strip()
    if value.startswith('"'):
        return value[1:].split('"', 1)[0]
    return value.split(" ", 1)[0]


def repair_autostart():
    """开机自启登记的 exe 已经不存在 (被移动或删除) 时，改成当前位置。返回是否修复了。
    登记的文件还在时不动它：用户可能同时有好几份 (例如下载的正式版和本地开发版)"""
    value = _autostart_value()
    if value is None or value == _autostart_command():
        return False
    if os.path.exists(_registered_exe(value)):
        return False
    set_autostart(True)
    return True


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------
# 给网易云的快捷方式 / 开机自启项加上 --remote-debugging-port
# --------------------------------------------------------------------------
def _debug_flag(port):
    return f"--remote-debugging-port={port}"


def _shortcut_dirs():
    env = os.environ.get
    return [os.path.join(env("USERPROFILE", ""), "Desktop"),
            os.path.join(env("PUBLIC", ""), "Desktop"),
            os.path.join(env("APPDATA", ""), r"Microsoft\Windows\Start Menu"),
            os.path.join(env("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu"),
            os.path.join(env("APPDATA", ""), r"Microsoft\Internet Explorer\Quick Launch")]


def netease_shortcuts():
    import comtypes.client  # pycaw 的依赖，已随程序打包
    shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
    for d in _shortcut_dirs():
        for path in glob.glob(os.path.join(glob.escape(d), "**", "*.lnk"), recursive=True):
            try:
                lnk = shell.CreateShortcut(path)
                if os.path.basename(lnk.TargetPath).lower() == NETEASE_EXE:
                    yield path, lnk
            except Exception:
                continue


def debug_port_status(port):
    """返回 (已设置的数量, 总数)，统计快捷方式和网易云自己的开机自启项"""
    done = total = 0
    try:
        for _, lnk in netease_shortcuts():
            total += 1
            done += _debug_flag(port) in lnk.Arguments
    except Exception:
        pass
    cmd = _netease_run_value()
    if cmd is not None:
        total += 1
        done += _debug_flag(port) in cmd
    return done, total


def _netease_run_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "cloudmusic")[0]
    except OSError:
        return None


def _with_flag(args, flag):
    """去掉已有的 --remote-debugging-port=...，flag 不为空时再加上新的"""
    kept = [a for a in args.split(" ") if a and not a.startswith("--remote-debugging-port=")]
    return " ".join(kept + ([flag] if flag else []))


def _set_debug_port(flag):
    ok, failed = [], []
    for path, lnk in netease_shortcuts():
        try:
            new = _with_flag(lnk.Arguments, flag)
            if new != lnk.Arguments:
                lnk.Arguments = new
                lnk.Save()
            ok.append(path)
        except Exception as e:
            failed.append(f"{path}: {e}")
    cmd = _netease_run_value()
    if cmd is not None:
        new = _with_flag(cmd, flag)
        if new != cmd:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "cloudmusic", 0, winreg.REG_SZ, new)
        ok.append("网易云开机自启")
    return ok, failed


def enable_debug_port(port):
    """需要管理员权限 (开始菜单里的快捷方式在 ProgramData 下)。返回 (成功列表, 失败列表)"""
    return _set_debug_port(_debug_flag(port))


def disable_debug_port():
    """把网易云的快捷方式和开机自启还原成没有调试端口的样子"""
    return _set_debug_port(None)


# --------------------------------------------------------------------------
# 网易云进程
# --------------------------------------------------------------------------
def netease_running():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {NETEASE_EXE}", "/NH"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return NETEASE_EXE in out.stdout.lower()


def netease_exe_path():
    for key in (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\网易云音乐",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\网易云音乐"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                path = winreg.QueryValueEx(k, "DisplayIcon")[0].split(",")[0].strip('"')
                if os.path.basename(path).lower() == NETEASE_EXE and os.path.exists(path):
                    return path
        except OSError:
            continue
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        path = os.path.join(base, r"NetEase\CloudMusic", NETEASE_EXE)
        if os.path.exists(path):
            return path
    return None


def restart_netease(port=None):
    """结束网易云并重新启动；port 不为空时带上调试端口"""
    exe = netease_exe_path()
    subprocess.run(["taskkill", "/IM", NETEASE_EXE, "/F"], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    if exe:
        import time
        time.sleep(1.5)
        subprocess.Popen([exe] + ([_debug_flag(port)] if port else []), cwd=os.path.dirname(exe),
                         creationflags=subprocess.DETACHED_PROCESS)
    return exe is not None
