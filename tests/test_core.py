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
