"""网易云音乐 -> Discord Rich Presence (Windows)

数据来源:
  * 窗口标题: cloudmusic.exe 主窗口标题为 "歌名 - 歌手" 时判定为播放中
  * 本地数据库: %LOCALAPPDATA%\\NetEase\\CloudMusic\\Library\\webdb.dat
    的 historyTracks 表，提供歌曲 ID / 专辑 / 封面 / 时长 / 开始播放时间
  * 调试端口 (可选): 网易云以 --remote-debugging-port 启动时读取真实进度与播放状态
  * 音频电平 (pycaw，后备): 网易云在混音器里的电平持续接近 0 即视为暂停
  * 歌词: music.163.com 公开歌词接口
"""

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import re
import sqlite3
import struct
import sys
import threading
import time
import urllib.request
import uuid

APP_NAME = "NeteaseDiscordRPC"
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "netease_rpc.log")
DB_PATH = os.path.expandvars(r"%LOCALAPPDATA%\NetEase\CloudMusic\Library\webdb.dat")
PROCESS_NAME = "cloudmusic.exe"

DEFAULT_CONFIG = {
    "client_id": "1552064189902888970",  # Discord 应用 "NeteaseMusic"，可换成自己的
    "poll_interval": 1,
    "show_buttons": True,
    "show_download_button": True,
    "show_when_paused": False,
    "show_lyrics": True,
    "pause_grace": 1.5,
    "cdp_port": 29222,  # 网易云的 --remote-debugging-port，0 表示不用
}
LYRIC_INTERVAL = 4  # 秒
DOWNLOAD_URL = "https://github.com/EdisonJun/Neteasediscord/releases/latest"

log = logging.getLogger("netease-rpc")


# --------------------------------------------------------------------------
# Windows: 枚举 cloudmusic.exe 的可见窗口标题
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

# 声明参数类型: 否则 ctypes 按 32 位 int 传句柄，64 位句柄会溢出，导致枚举窗口提前中断
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _process_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def netease_window_titles():
    """返回 None 表示网易云未运行，否则返回其所有非空窗口标题列表。"""
    titles, running = [], False
    name_cache = {}

    def callback(hwnd, _):
        nonlocal running
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in name_cache:
            name_cache[pid.value] = _process_name(pid.value)
        if name_cache[pid.value] != PROCESS_NAME:
            return True
        running = True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                titles.append(buf.value.strip())
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return titles if running else None


# --------------------------------------------------------------------------
# 网易云本地数据库
# --------------------------------------------------------------------------
def latest_track():
    return _query_track("SELECT playtime, jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 1")


def _query_track(sql, args=()):
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute(sql, args).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.debug("读取数据库失败: %s", e)
        return None
    if not row:
        return None
    return parse_track(json.loads(row[1]), row[0] / 1000)


def parse_track(data, started=0):
    album = data.get("album") or {}
    return {
        "id": str(data.get("id", "")),
        "name": data.get("name", ""),
        "artists": " / ".join(a.get("name", "") for a in data.get("artists") or []),
        "album": album.get("name", ""),
        "cover": album.get("picUrl") or album.get("cover") or "",
        "duration": (data.get("duration") or 0) / 1000,
        "started": started,
    }


# --------------------------------------------------------------------------
# Discord IPC (命名管道)
# --------------------------------------------------------------------------
class DiscordIPC:
    OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2

    def __init__(self, client_id):
        self.client_id = client_id
        self.pipe = None

    @property
    def connected(self):
        return self.pipe is not None

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\?\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        else:
            raise ConnectionError("找不到 Discord，请确认 Discord 客户端已打开")
        self._send(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if op == self.OP_CLOSE or data.get("evt") == "ERROR":
            self.close()
            raise ConnectionError(f"Discord 握手失败: {data}")
        user = (data.get("data") or {}).get("user") or {}
        log.info("已连接 Discord (用户: %s)", user.get("username", "?"))

    def _send(self, op, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.pipe.write(struct.pack("<II", op, len(body)) + body)

    def _recv(self):
        header = self.pipe.read(8)
        if len(header) < 8:
            raise ConnectionError("Discord 连接已断开")
        op, length = struct.unpack("<II", header)
        return op, json.loads(self.pipe.read(length).decode("utf-8"))

    def set_activity(self, activity):
        self._send(self.OP_FRAME, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        })
        op, data = self._recv()
        if data.get("evt") == "ERROR":
            log.warning("设置状态失败: %s", data.get("data"))

    def close(self):
        if self.pipe:
            try:
                self._send(self.OP_CLOSE, {})
                self.pipe.close()
            except OSError:
                pass
        self.pipe = None


# --------------------------------------------------------------------------
# 精确播放状态: 网易云以 --remote-debugging-port 启动时，通过 Chrome DevTools
# 协议只读地查询它的页面: redux store 里的播放状态与歌曲信息，以及进度条里的
# 当前秒数 (支持拖动进度条)。
# 注意不能注册 audioplayer.onPlayProgress 回调: 原生层每个事件只保留最后注册的
# 一个处理函数，注册了就会和网易云自己的进度条互相抢
# --------------------------------------------------------------------------
try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

CDP_PROBE = r"""(() => {
  if (!window.__ncmStore) {  // 从 React 根节点找到 redux store
    const el = document.getElementById("root") || document.body.firstElementChild;
    const k = el && Object.keys(el).find(k => k.startsWith("__reactContainer") || k.startsWith("_reactRootContainer"));
    let f = k && el[k]; if (f && f._internalRoot) f = f._internalRoot.current;
    const stack = [f];
    for (let n = 0; stack.length && n < 20000 && !window.__ncmStore; n++) {
      const x = stack.pop(); if (!x) continue;
      const st = x.memoizedProps && x.memoizedProps.store;
      if (st && st.getState && st.getState().playing) window.__ncmStore = st;
      if (x.sibling) stack.push(x.sibling); if (x.child) stack.push(x.child);
    }
  }
  const pl = window.__ncmStore && window.__ncmStore.getState().playing;
  if (!pl) return null;
  // 播放进度条里隐藏的 <input type=range>，value 就是当前秒数 (窗口最小化时也会更新)
  const range = document.querySelector('[aria-label="播放进度调节"] input[type=range]');
  return { playing: pl.playingState === 2, track: (pl.curPlaying || {}).track || null,
           position: range && range.value !== "" ? parseFloat(range.value) : null };
})()"""


class NeteaseCDP:
    RETRY = 10  # 秒，连接失败后多久重试

    def __init__(self, port):
        self.port = port
        self.ws = None
        self.msg_id = 0
        self.retry_at = 0
        self.no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _connect(self):
        with self.no_proxy.open(f"http://127.0.0.1:{self.port}/json", timeout=1) as r:
            pages = json.load(r)
        page = next(p for p in pages
                    if p.get("type") == "page" and p.get("url", "").startswith("orpheus://"))
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=2, suppress_origin=True,
            http_no_proxy=["127.0.0.1"])
        log.info("已通过调试端口连接网易云，使用精确进度")

    def _eval(self, expr):
        self.msg_id += 1
        self.ws.send(json.dumps({"id": self.msg_id, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.msg_id:
                return msg["result"]["result"].get("value")

    def state(self):
        """返回 {"track", "playing", "position"}；拿不到时返回 None (退回音频电平检测)"""
        if websocket is None or not self.port:
            return None
        if self.ws is None:
            if time.time() < self.retry_at:
                return None
            try:
                self._connect()
            except Exception as e:
                log.debug("调试端口不可用: %s", e)
                self.retry_at = time.time() + self.RETRY
                return None
        try:
            s = self._eval(CDP_PROBE)
        except Exception as e:
            log.info("与网易云调试端口的连接断开: %s", e)
            self.ws = None
            return None
        if not s or not s.get("track"):
            return None
        track = parse_track(s["track"])
        playing, position = s["playing"], s["position"]
        if position is None and not playing:
            position = 0  # 暂停时不显示进度条，位置无关紧要
        return {"track": track, "playing": playing, "position": position}


# --------------------------------------------------------------------------
# 暂停检测 (后备): 网易云暂停时窗口标题不变，也不接入系统媒体控件 (SMTC)，
# 所以读取它在 Windows 混音器里的音频电平 —— 暂停时电平接近 0
# --------------------------------------------------------------------------
try:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
except ImportError:  # 没装 pycaw 时退化为 "有窗口标题就算播放中"
    AudioUtilities = None


class AudioMeter:
    REFRESH = 10  # 秒，定期重新枚举音频会话
    THRESHOLD = 0.0005  # 暂停时电平约 6e-10 而不是 0；正常播放的安静段落也在 0.005 以上

    def __init__(self):
        self.meters = []
        self.refreshed = 0

    def _refresh(self):
        self.meters = []
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() == PROCESS_NAME:
                self.meters.append(s._ctl.QueryInterface(IAudioMeterInformation))
        self.refreshed = time.time()

    def sounding(self):
        """True: 有声音; False: 静音; None: 无法检测"""
        if AudioUtilities is None:
            return None
        try:
            if not self.meters or time.time() - self.refreshed > self.REFRESH:
                self._refresh()
            return any(m.GetPeakValue() > self.THRESHOLD for m in self.meters)
        except Exception as e:
            log.debug("读取音频电平失败: %s", e)
            self.meters = []
            return None


# --------------------------------------------------------------------------
# 歌词
# --------------------------------------------------------------------------
LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
LRC_CREDIT = re.compile(r"^[^:：]{1,24}[:：]")  # "作词 : xxx" / "Mix & Master: xxx" 这类署名行


def parse_lrc(text):
    lines = []
    for raw in (text or "").splitlines():
        stamps = LRC_TIME.findall(raw)
        content = LRC_TIME.sub("", raw).strip()
        if not stamps or LRC_CREDIT.match(content):
            continue
        for m, sec in stamps:
            lines.append((int(m) * 60 + float(sec), content))
    lines.sort(key=lambda x: x[0])
    return lines


class Lyrics:
    URL = "https://music.163.com/api/song/lyric?id={}&lv=1&tv=-1"

    def __init__(self):
        self.cache = {}  # song id -> [(秒, 歌词)]，None 表示正在获取
        self.lock = threading.Lock()

    def _fetch(self, song_id):
        lines = []
        try:
            req = urllib.request.Request(self.URL.format(song_id), headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
            lines = parse_lrc((data.get("lrc") or {}).get("lyric"))
            log.debug("获取歌词 %s: %d 行", song_id, len(lines))
        except Exception as e:
            log.debug("获取歌词失败 %s: %s", song_id, e)
        with self.lock:
            self.cache[song_id] = lines

    def line_at(self, song_id, position):
        if not song_id:
            return ""
        with self.lock:
            if song_id not in self.cache:
                self.cache[song_id] = None
                threading.Thread(target=self._fetch, args=(song_id,), daemon=True).start()
                return ""
            lines = self.cache[song_id]
        current = ""
        for t, text in lines or ():
            if t > position:
                break
            current = text
        return current


# --------------------------------------------------------------------------
# 播放状态跟踪
# --------------------------------------------------------------------------
def _clip(text, limit=128):
    text = (text or "").strip()
    if len(text) < 2:
        text = text.ljust(2, "​")  # Discord 要求字段至少 2 个字符
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_playing_title(titles, track):
    """在窗口标题中找到 "歌名 - 歌手" 形式的标题。"""
    for t in titles:
        if track and t.startswith(track["name"]) and " - " in t:
            return t
    for t in titles:
        if " - " in t and t != "网易云音乐":
            return t
    return None


class Tracker:
    def __init__(self, config):
        self.config = config
        self.meter = AudioMeter()
        self.cdp = NeteaseCDP(config["cdp_port"])
        self.lyrics = Lyrics()
        self.song_key = None  # 当前歌曲 (id 或标题)
        self.elapsed = 0.0  # 当前歌曲已播放秒数 (只在有声音时累加)
        self.silence = 0.0  # 连续静音的秒数 (尚未计入 elapsed)
        self.last_tick = None
        self.last_sent = None

    def build_activity(self):
        titles = netease_window_titles()
        if titles is None:
            self.song_key = None
            return None

        now = time.time()
        player = self.cdp.state()
        if player:
            self.song_key = None  # 切回后备模式时重新估算
            info, playing, position = player["track"], player["playing"], player["position"]
            if position is None:
                return self.last_sent
        else:
            result = self._estimate(titles, now)
            if result is None:
                return None
            info, playing, position = result

        if not playing and not self.config["show_when_paused"]:
            return None
        if info["duration"]:
            position = min(max(position, 0), info["duration"])
        return self._activity(info, playing, position, now)

    def _estimate(self, titles, now):
        """后备方案: 窗口标题 + 数据库开始时间 + 音频电平估算进度"""
        track = latest_track()
        title = find_playing_title(titles, track)

        if title and track and title.startswith(track["name"]):
            info = track
        elif title:  # 数据库还没更新，先用窗口标题
            name, _, artists = title.rpartition(" - ")
            info = {"id": "", "name": name, "artists": artists, "album": "",
                    "cover": "", "duration": 0, "started": now}
        elif track:
            info = track
        else:
            return None

        sounding = self.meter.sounding() if title else False
        if sounding is None:
            sounding = title is not None

        key = info["id"] or info["name"]
        if key != self.song_key:
            self.song_key = key
            self.elapsed = max(0.0, now - info["started"])
            if info["duration"]:
                self.elapsed = min(self.elapsed, info["duration"])
            self.silence = 0.0
        elif self.last_tick is not None:
            dt = now - self.last_tick
            if sounding:
                # 短暂静音 (歌曲里的安静段落) 其实一直在播放，补回进度；
                # 超过 pause_grace 的是真暂停，那段时间不计入进度
                if self.silence < self.config["pause_grace"]:
                    self.elapsed += self.silence
                self.elapsed += dt
                self.silence = 0.0
            else:
                self.silence += dt
        self.last_tick = now

        playing = self.silence < self.config["pause_grace"]
        return info, playing, self.elapsed + (self.silence if playing else 0)

    def _activity(self, info, playing, position, now):
        lyric = self.lyrics.line_at(info["id"], position) if self.config["show_lyrics"] else ""
        byline = " · ".join(x for x in (info["artists"], info["album"]) if x)

        activity = {
            "type": 2,  # Listening
            "status_display_type": 2,  # 成员列表里显示 "正在听 <歌名>"
            "details": _clip(info["name"]),
            # 有歌词时: 第二行 "歌手 · 专辑"，第三行 (封面文字) 显示歌词
            "state": _clip(byline if lyric else (info["artists"] or "未知歌手")),
            "assets": {
                "large_image": info["cover"] or "netease",
                "large_text": _clip(("♪ " + lyric) if lyric else (info["album"] or info["name"])),
            },
        }
        if playing and info["duration"]:
            start = now - position
            activity["timestamps"] = {
                "start": int(start * 1000),
                "end": int((start + info["duration"]) * 1000),
            }
        if not playing:
            activity["state"] = _clip("⏸ 已暂停 · " + (info["artists"] or ""))
            activity["assets"]["large_text"] = _clip(info["album"] or info["name"])
        buttons = []  # Discord 最多 2 个按钮
        if info["id"] and self.config["show_buttons"]:
            buttons.append({"label": "在网易云音乐中收听",
                            "url": f"https://music.163.com/song?id={info['id']}"})
        if self.config["show_download_button"]:
            buttons.append({"label": "下载插件", "url": DOWNLOAD_URL})
        if buttons:
            activity["buttons"] = buttons
        return activity

    def change_level(self, activity):
        """0: 无需推送; 1: 只有歌词变了 (可延后); 2: 歌曲/播放状态变了 (立即推送)"""
        prev = self.last_sent
        if not prev or not activity:
            return 0 if prev == activity else 2

        def core(a):
            a = {k: v for k, v in a.items() if k not in ("timestamps", "state")}
            a["assets"] = {k: v for k, v in a["assets"].items() if k != "large_text"}
            return a

        if core(prev) != core(activity) or ("timestamps" in prev) != ("timestamps" in activity):
            return 2
        if "timestamps" in activity and \
                abs(activity["timestamps"]["start"] - prev["timestamps"]["start"]) > 3000:
            return 2
        if activity["state"] != prev["state"] or activity["assets"] != prev["assets"]:
            return 1
        return 0


# --------------------------------------------------------------------------
def load_config():
    os.makedirs(APP_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("配置文件读取失败，使用默认配置: %s", e)
    cfg["client_id"] = str(cfg["client_id"]).strip()
    save_config(cfg)  # 补全新版本增加的字段
    return cfg


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def setup_logging(verbose=False):
    os.makedirs(APP_DIR, exist_ok=True)
    handlers = [logging.FileHandler(LOG_PATH, "w", "utf-8")]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", handlers=handlers)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


class Presence:
    """后台同步循环。run() 阻塞直到 stop() 被调用；状态供托盘菜单显示。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tracker = Tracker(cfg)
        self.ipc = DiscordIPC(cfg["client_id"])
        self._stop = threading.Event()
        self.discord_ok = False
        self.now_playing = ""  # 最近一次推送的 "歌名 - 歌手"，空表示没有状态

    @property
    def precise(self):
        """是否正在通过网易云调试端口读取精确进度"""
        return self.tracker.cdp.ws is not None

    def stop(self):
        self._stop.set()

    def refresh(self):
        """配置改动后强制下一轮重新推送"""
        self.tracker.last_sent = {}

    def run(self):
        ipc, tracker = self.ipc, self.tracker
        last_send = 0.0
        log.info("网易云 Discord 状态同步已启动")
        while not self._stop.is_set():
            try:
                if not ipc.connected:
                    ipc.connect()
                    tracker.last_sent = None
                self.discord_ok = True
                activity = tracker.build_activity()
                level = tracker.change_level(activity)
                since = time.time() - last_send
                # Discord 限制约 5 次 / 20 秒: 歌词更新至少间隔 LYRIC_INTERVAL 秒
                if (level == 2 and since >= 1) or (level == 1 and since >= LYRIC_INTERVAL):
                    ipc.set_activity(activity)
                    last_send = time.time()
                    tracker.last_sent = activity
                    self.now_playing = f"{activity['details']} - {activity['state']}" if activity else ""
                    if level == 1:
                        log.debug("  %s", activity["assets"]["large_text"])
                    elif activity and "timestamps" not in activity:
                        log.info("⏸ 已暂停: %s", activity["details"])
                    elif activity:
                        log.info("♪ %s - %s", activity["details"], activity["state"])
                    else:
                        log.info("已清除状态")
            except (ConnectionError, OSError) as e:
                if ipc.connected:
                    log.warning("Discord 连接断开: %s", e)
                else:
                    log.debug("%s", e)
                ipc.close()
                self.discord_ok = False
                self.now_playing = ""
                self._stop.wait(10)
                continue
            except Exception:
                log.exception("未知错误")
            self._stop.wait(self.cfg["poll_interval"])
        ipc.close()  # 断开后 Discord 会自动清除状态
        log.info("已退出")


def main():
    """命令行模式 (调试用)：python netease_rpc.py [-v]"""
    setup_logging("-v" in sys.argv)
    presence = Presence(load_config())
    try:
        presence.run()
    except KeyboardInterrupt:
        presence.stop()


if __name__ == "__main__":
    main()
