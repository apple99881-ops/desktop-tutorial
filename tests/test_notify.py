import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from uvis_monitor.models import Alert, Level
from uvis_monitor.notify import NotifierHub
from uvis_monitor.notify.base import Notifier, map_link, split_text
from uvis_monitor.notify.kakao import KakaoNotifier
from uvis_monitor.notify.telegram import TelegramNotifier

from helpers import at


def alert(**kw):
    base = dict(key="k", level=Level.CRITICAL, title="[출발지연] 1호차 <수원>", lines=["계획 06:30 → 현재 06:35"],
                created_at=at(6, 35), lat=37.2664, lon=127.0001, place="수원역, 4번출구")
    base.update(kw)
    return Alert(**base)


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class SplitTest(unittest.TestCase):
    def test_split_respects_limit(self):
        text = "\n".join(f"{i}번째 줄 " + "가" * 30 for i in range(20))
        chunks = split_text(text, 200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertTrue(chunks[0].startswith("(1/"))
        self.assertEqual(split_text("짧음", 200), ["짧음"])

    def test_map_link(self):
        self.assertIn("37.266400,127.000100", map_link(alert()))
        self.assertIsNone(map_link(alert(lat=None, lon=None)))


class KakaoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / "kakao.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, expires_at):
        self.token.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R",
                                          "expires_at": expires_at, "refresh_token_expires_at": time.time() + 9e6}))

    def test_refreshes_expired_token_then_sends_chunks(self):
        self._write(time.time() - 10)
        k = KakaoNotifier("KEY", self.token)
        posts = []

        def fake_post(url, **kw):
            posts.append((url, kw))
            if "oauth/token" in url:
                return Resp(200, {"access_token": "NEW", "expires_in": 21599})
            return Resp(200, {"result_code": 0})

        k.session.post = fake_post
        self.assertTrue(k.send_text("가" * 450))
        self.assertIn("oauth/token", posts[0][0])
        sends = [p for p in posts if "memo" in p[0]]
        self.assertEqual(len(sends), 3)  # 200자 단위 분할
        self.assertEqual(sends[0][1]["headers"]["Authorization"], "Bearer NEW")
        tpl = json.loads(sends[0][1]["data"]["template_object"])
        self.assertEqual(tpl["object_type"], "text")
        self.assertLessEqual(len(tpl["text"]), 200)
        self.assertEqual(json.loads(self.token.read_text())["access_token"], "NEW")

    def test_retry_once_on_401(self):
        self._write(time.time() + 3600)
        k = KakaoNotifier("KEY", self.token)
        calls = {"memo": 0}

        def fake_post(url, **kw):
            if "oauth/token" in url:
                return Resp(200, {"access_token": "NEW", "expires_in": 21599})
            calls["memo"] += 1
            return Resp(401, {"msg": "expired"}) if calls["memo"] == 1 else Resp(200, {"result_code": 0})

        k.session.post = fake_post
        self.assertTrue(k.send(alert()))
        self.assertEqual(calls["memo"], 2)

    def test_missing_token_file_fails_softly(self):
        k = KakaoNotifier("KEY", self.token)
        self.assertFalse(k.send_text("hi"))


class TelegramTest(unittest.TestCase):
    def test_html_escape_and_command_filter(self):
        tg = TelegramNotifier("T", "42")
        sent = {}

        def fake_post(url, **kw):
            sent.update(kw.get("json") or {})
            return Resp(200, {"ok": True})

        tg.session.post = fake_post
        self.assertTrue(tg.send(alert()))
        self.assertIn("&lt;수원&gt;", sent["text"])
        self.assertIn("map.kakao.com", sent["text"])
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/현황"}},
            {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/현황"}},  # 다른 사람 → 무시
        ]}
        tg.session.get = mock.Mock(return_value=Resp(200, updates))
        self.assertEqual(tg.poll_commands(), ["/현황"])


class HubTest(unittest.TestCase):
    def test_failing_channel_does_not_raise_and_levels_filter(self):
        class Boom(Notifier):
            name = "boom"

            def send_text(self, text):
                raise RuntimeError("down")

        class Rec(Notifier):
            name = "rec"
            got: list = []

            def send_text(self, text):
                self.got.append(text)
                return True

        rec = Rec(levels=["critical"])
        hub = NotifierHub([Boom(), rec])
        self.assertEqual(hub.send(alert()), [("boom", False), ("rec", True)])
        self.assertEqual(hub.send(alert(level=Level.OK)), [("boom", False)])
        self.assertEqual(len(rec.got), 1)


if __name__ == "__main__":
    unittest.main()
