"""python -m unittest discover -s tests -v"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import netease_rpc as core  # noqa: E402
import system  # noqa: E402


class ParseLrcTest(unittest.TestCase):
    def test_credits_are_skipped(self):
        lrc = ("[00:00.00] 作词 : 某人\n"
               "[00:01.00]Mix & Master: Someone\n"
               "[00:02.00]制作人：某人\n"
               "[00:12.50]第一句\n")
        self.assertEqual(core.parse_lrc(lrc), [(12.5, "第一句")])

    def test_multiple_timestamps_and_sorting(self):
        lrc = "[00:30.00][00:10.00]副歌\n[00:20.00]主歌\n"
        self.assertEqual(core.parse_lrc(lrc), [(10.0, "副歌"), (20.0, "主歌"), (30.0, "副歌")])

    def test_empty(self):
        self.assertEqual(core.parse_lrc(None), [])
        self.assertEqual(core.parse_lrc("没有时间戳的一行"), [])


class ClipTest(unittest.TestCase):
    def test_min_length(self):
        self.assertEqual(len(core._clip("a")), 2)  # Discord 要求至少 2 个字符

    def test_max_length(self):
        clipped = core._clip("x" * 300)
        self.assertEqual(len(clipped), 128)
        self.assertTrue(clipped.endswith("…"))


def _activity(state="歌手", lyric="", start=1000):
    return {"type": 2, "details": "歌名", "state": state,
            "assets": {"large_image": "c", "large_text": lyric or "专辑"},
            "timestamps": {"start": start, "end": start + 200_000}}


class ChangeLevelTest(unittest.TestCase):
    def setUp(self):
        self.t = core.Tracker(dict(core.DEFAULT_CONFIG, cdp_port=0))

    def test_first_send_and_clear(self):
        self.assertEqual(self.t.change_level(_activity()), 2)
        self.t.last_sent = _activity()
        self.assertEqual(self.t.change_level(None), 2)
        self.t.last_sent = None
        self.assertEqual(self.t.change_level(None), 0)

    def test_lyric_only_is_level_1(self):
        self.t.last_sent = _activity(lyric="♪ 第一句")
        self.assertEqual(self.t.change_level(_activity(lyric="♪ 第二句")), 1)

    def test_small_drift_ignored_but_seek_is_level_2(self):
        self.t.last_sent = _activity(start=1000)
        self.assertEqual(self.t.change_level(_activity(start=2500)), 0)
        self.assertEqual(self.t.change_level(_activity(start=9000)), 2)

    def test_pause_is_level_2(self):
        self.t.last_sent = _activity()
        paused = _activity()
        del paused["timestamps"]
        self.assertEqual(self.t.change_level(paused), 2)


class QQPickSongTest(unittest.TestCase):
    def setUp(self):
        import qqmusic
        self.pick = qqmusic.pick_song
        self.candidates = [
            {"name": "示例歌曲", "singer": [{"name": "别的歌手"}], "interval": 200, "mid": "other"},
            {"name": "示例歌曲", "singer": [{"name": "歌手甲"}, {"name": "歌手乙"}], "interval": 218, "mid": "right"},
            {"name": "完全不同", "singer": [{"name": "歌手甲"}], "interval": 218, "mid": "wrong"},
        ]

    def test_prefers_matching_artist_and_duration(self):
        self.assertEqual(self.pick(self.candidates, "示例歌曲", "歌手甲/歌手乙", 218)["mid"], "right")

    def test_title_must_match(self):
        self.assertIsNone(self.pick(self.candidates, "没有这首歌", "歌手甲", 218))

    def test_case_and_spaces_ignored(self):
        songs = [{"name": "Some Song", "singer": [{"name": "Band"}], "interval": 100, "mid": "x"}]
        self.assertEqual(self.pick(songs, "some  song", "band", 100)["mid"], "x")


class FakeSource:
    def __init__(self, name):
        self.name, self.playing, self.next = name, False, None

    def build_activity(self):
        return self.next


def _song(title):
    return {"details": title, "state": "x", "assets": {}}


class PickActivityTest(unittest.TestCase):
    def setUp(self):
        self.p = core.Presence.__new__(core.Presence)  # 不连接 Discord、不创建真实播放器
        self.netease, self.qq = FakeSource("Netease Music"), FakeSource("QQ Music")
        self.p.sources, self.p._seen = [self.netease, self.qq], {}

    def _set(self, src, title, playing):
        src.next, src.playing = (_song(title) if title else None), playing

    def test_playing_beats_paused(self):
        self._set(self.netease, "A", False)
        self._set(self.qq, "B", True)
        self.assertIs(self.p.pick_activity()[0], self.qq)

    def test_both_playing_most_recent_start_wins(self):
        self._set(self.netease, "A", True)
        self._set(self.qq, None, False)
        self.p.pick_activity()
        time.sleep(0.01)
        self._set(self.qq, "B", True)  # QQ 音乐后开始播放
        self.assertIs(self.p.pick_activity()[0], self.qq)
        time.sleep(0.01)
        self._set(self.netease, "C", True)  # 网易云切歌，又成了最近开始的
        self.assertIs(self.p.pick_activity()[0], self.netease)

    def test_nothing_playing_shows_last_played(self):
        self._set(self.qq, "B", True)
        self.p.pick_activity()
        self._set(self.qq, "B", False)
        self._set(self.netease, "A", False)
        self.assertIs(self.p.pick_activity()[0], self.qq)

    def test_nothing_at_all(self):
        self.assertEqual(self.p.pick_activity(), (None, None))


class LyricsRetryTest(unittest.TestCase):
    def _wait(self, lyrics, song_id):
        for _ in range(100):
            with lyrics.lock:
                if lyrics.cache.get(song_id) is not None or song_id not in lyrics.cache:
                    return
            time.sleep(0.01)

    def test_retry_after_failure_then_success(self):
        lyrics = core.Lyrics()
        lyrics.RETRY_AFTER = 0
        responses = [OSError("network down"),
                     {"lrc": {"lyric": "[00:01.00]你好\n[00:05.00]世界"}}]

        def fake_download(song_id):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(lyrics, "_download", side_effect=fake_download):
            self.assertEqual(lyrics.line_at("1", 6), "")   # 第一次: 失败
            self._wait(lyrics, "1")
            self.assertNotIn("1", lyrics.cache)             # 失败后不缓存，稍后重试
            self.assertEqual(lyrics.line_at("1", 6), "")   # 第二次: 成功
            self._wait(lyrics, "1")
            self.assertEqual(lyrics.line_at("1", 6), "世界")

    def test_gives_up_after_max_tries(self):
        lyrics = core.Lyrics()
        lyrics.RETRY_AFTER = 0
        with mock.patch.object(lyrics, "_download", side_effect=OSError("down")):
            for _ in range(lyrics.MAX_TRIES):
                lyrics.line_at("2", 0)
                self._wait(lyrics, "2")
        self.assertEqual(lyrics.cache.get("2"), [])


class VersionTest(unittest.TestCase):
    def test_compare(self):
        self.assertGreater(app.version_tuple("v1.10.0"), app.version_tuple("1.9.9"))
        self.assertEqual(app.version_tuple("v1.2.0"), (1, 2, 0))
        self.assertEqual(app.version_tuple("v1.2.0"), app.version_tuple("1.2.0"))  # 有没有 v 前缀都一样
        self.assertEqual(app.version_tuple("dev"), (0,))


class RepairAutostartTest(unittest.TestCase):
    def test_registered_exe(self):
        self.assertEqual(system._registered_exe('"C:\\A B\\x.exe" --flag'), "C:\\A B\\x.exe")
        self.assertEqual(system._registered_exe("C:\\x.exe --flag"), "C:\\x.exe")

    def _run(self, registered, exists):
        with mock.patch.object(system, "_autostart_value", return_value=registered), \
                mock.patch.object(system, "_autostart_command", return_value='"C:\\new\\app.exe"'), \
                mock.patch.object(system.os.path, "exists", return_value=exists), \
                mock.patch.object(system, "set_autostart") as set_autostart:
            return system.repair_autostart(), set_autostart.called

    def test_missing_exe_is_repaired(self):
        self.assertEqual(self._run('"C:\\old\\app.exe"', exists=False), (True, True))

    def test_other_existing_copy_is_left_alone(self):
        # 同时存在好几份程序时，不能被另一份抢走开机自启
        self.assertEqual(self._run('"C:\\other\\app.exe"', exists=True), (False, False))

    def test_disabled_or_same_path_does_nothing(self):
        self.assertEqual(self._run(None, exists=False), (False, False))
        self.assertEqual(self._run('"C:\\new\\app.exe"', exists=False), (False, False))


class DebugFlagTest(unittest.TestCase):
    FLAG = "--remote-debugging-port=29222"

    def test_add(self):
        self.assertEqual(system._with_flag("--orpheus-startup=autorun", self.FLAG),
                         "--orpheus-startup=autorun " + self.FLAG)

    def test_replace_old_port(self):
        self.assertEqual(system._with_flag("--remote-debugging-port=1234 -x", self.FLAG), "-x " + self.FLAG)

    def test_remove_keeps_quoted_path(self):
        cmd = '"C:\\Program Files\\NetEase\\CloudMusic\\cloudmusic.exe" --orpheus-startup=autorun ' + self.FLAG
        self.assertEqual(system._with_flag(cmd, None),
                         '"C:\\Program Files\\NetEase\\CloudMusic\\cloudmusic.exe" --orpheus-startup=autorun')

    def test_remove_from_empty(self):
        self.assertEqual(system._with_flag(self.FLAG, None), "")


if __name__ == "__main__":
    unittest.main()
