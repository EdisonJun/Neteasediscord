"""QQ 音乐支持。

数据来源:
  * Windows 系统媒体控件 (SMTC): QQ 音乐会把正在播放的歌名、歌手、专辑、播放/暂停、
    进度和时长报告给系统，读取它不需要任何设置
  * QQ 音乐网页接口: 按 歌名 + 歌手 + 时长 搜索到歌曲，拿到封面、歌曲页链接和歌词
"""

import asyncio
import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request

import netease_rpc as core

try:
    from winrt.windows.media.control import \
        GlobalSystemMediaTransportControlsSessionManager as SessionManager
except ImportError:  # 没装 winrt 时不支持 QQ 音乐
    SessionManager = None

log = logging.getLogger("netease-rpc")

APP_ID = "qqmusic.exe"  # SMTC 会话的 source_app_user_model_id
PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}
SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
LYRIC_URL = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
             "?songmid={}&format=json&nobase64=1&g_tk=5381")
COVER_URL = "https://y.qq.com/music/photo_new/T002R300x300M000{}.jpg"
SONG_URL = "https://y.qq.com/n/ryqq/songDetail/{}"


# --------------------------------------------------------------------------
# 系统媒体控件
# --------------------------------------------------------------------------
class SmtcReader:
    RETRY = 10  # 秒，出错后多久重试

    def __init__(self):
        self.loop = None
        self.manager = None
        self.retry_at = 0

    def read(self):
        """返回 {"title", "artist", "album", "playing", "position", "duration"}，QQ 音乐没在放时返回 None"""
        if SessionManager is None or time.time() < self.retry_at:
            return None
        try:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
            if self.manager is None:
                self.manager = self.loop.run_until_complete(SessionManager.request_async())
            for session in self.manager.get_sessions():
                if APP_ID not in (session.source_app_user_model_id or "").lower():
                    continue
                props = self.loop.run_until_complete(session.try_get_media_properties_async())
                playing = session.get_playback_info().playback_status == PLAYING
                timeline = session.get_timeline_properties()
                position = timeline.position.total_seconds()
                if playing:  # 进度是 last_updated_time 那一刻的值，补上之后经过的时间
                    position += max(0.0, time.time() - timeline.last_updated_time.timestamp())
                return {"title": props.title or "", "artist": props.artist or "",
                        "album": props.album_title or "", "playing": playing,
                        "position": position, "duration": timeline.end_time.total_seconds()}
            return None
        except Exception as e:
            log.debug("读取系统媒体控件失败: %s", e)
            self.manager = None
            self.retry_at = time.time() + self.RETRY
            return None


# --------------------------------------------------------------------------
# 在 QQ 音乐里找到这首歌 (封面 / 链接 / 歌词需要歌曲 mid)
# --------------------------------------------------------------------------
def _norm(text):
    return re.sub(r"\s+", "", (text or "").lower())


def pick_song(candidates, title, artist, duration):
    """从搜索结果里挑出最匹配的一首；歌名对不上就返回 None"""
    best, best_score = None, 0
    artists = {_norm(a) for a in re.split(r"[/,，&、]", artist or "") if a.strip()}
    for song in candidates:
        if _norm(song.get("name")) != _norm(title):
            continue
        score = 3
        singers = {_norm(s.get("name")) for s in song.get("singer") or []}
        if artists & singers:
            score += 2
        if duration and abs((song.get("interval") or 0) - duration) <= 3:
            score += 2
        if score > best_score:
            best, best_score = song, score
    return best


class SongLookup:
    """按 (歌名, 歌手) 缓存搜索结果，在后台线程里查询，不阻塞主循环"""

    RETRY_AFTER = 30

    def __init__(self):
        self.cache = {}  # key -> dict (找到) / {} (没找到) / None (查询中)
        self.failed_at = {}
        self.lock = threading.Lock()

    def _search(self, title, artist):
        body = {"req": {"module": "music.search.SearchCgiService", "method": "DoSearchForQQMusicDesktop",
                        "param": {"query": f"{title} {artist}".strip(), "search_type": 0,
                                  "num_per_page": 10, "page_num": 1}}}
        req = urllib.request.Request(SEARCH_URL, data=json.dumps(body).encode(),
                                     headers=dict(HEADERS, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        return data["req"]["data"]["body"]["song"]["list"]

    def _fetch(self, key, title, artist, duration):
        try:
            song = pick_song(self._search(title, artist), title, artist, duration)
        except Exception as e:
            log.debug("QQ 音乐搜索失败 %s: %s", key, e)
            with self.lock:
                self.cache.pop(key, None)
                self.failed_at[key] = time.time()
            return
        info = {}
        if song:
            album_mid = (song.get("album") or {}).get("mid") or ""
            info = {"mid": song.get("mid", ""), "album": (song.get("album") or {}).get("name", ""),
                    "cover": COVER_URL.format(album_mid) if album_mid else ""}
        log.debug("QQ 音乐搜索 %s -> %s", key, info.get("mid") or "未找到")
        with self.lock:
            self.cache[key] = info

    def get(self, title, artist, duration):
        """返回 {"mid", "album", "cover"}；还没查到或没找到时返回 {}"""
        key = (title, artist)
        with self.lock:
            if key not in self.cache:
                if time.time() - self.failed_at.get(key, 0) >= self.RETRY_AFTER:
                    self.cache[key] = None
                    threading.Thread(target=self._fetch, args=(key, title, artist, duration),
                                     daemon=True).start()
                return {}
            return self.cache[key] or {}


class QQLyrics(core.Lyrics):
    def _lrc_text(self, song_mid):
        req = urllib.request.Request(LYRIC_URL.format(song_mid), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        return html.unescape(data.get("lyric") or "")


# --------------------------------------------------------------------------
class QQTracker:
    """和 netease_rpc.Tracker 一样提供 build_activity() 和 playing"""

    def __init__(self, config):
        self.config = config
        self.reader = SmtcReader()
        self.songs = SongLookup()
        self.lyrics = QQLyrics()
        self.playing = False

    @property
    def name(self):
        return self.config["qqmusic_name"]

    def build_activity(self):
        s = self.reader.read()
        if not s or not s["title"]:
            self.playing = False
            return None
        self.playing = s["playing"]
        if not self.playing and not self.config["show_when_paused"]:
            return None
        meta = self.songs.get(s["title"], s["artist"], s["duration"])
        mid = meta.get("mid", "")
        info = {"id": mid, "name": s["title"], "artists": s["artist"],
                "album": s["album"] or meta.get("album", ""), "cover": meta.get("cover", ""),
                "duration": s["duration"],
                "url": SONG_URL.format(mid) if mid else "", "listen_label": "在QQ音乐中收听"}
        position = min(max(s["position"], 0), s["duration"] or s["position"])
        return core.compose_activity(self.config, self.lyrics, self.name, info,
                                     self.playing, position, time.time())
