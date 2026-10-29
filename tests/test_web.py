"""UVIS 웹 자동조회 시험 (실제 UVIS 흐름을 흉내 낸 가짜 사이트 + 실제 브라우저). Playwright 가 없으면 건너뜀."""
import glob
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from uvis_monitor.models import KST

from mock_uvis_site import MockUvis

HAVE_PW = importlib.util.find_spec("playwright") is not None

CHROME = os.environ.get("UVIS_TEST_CHROMIUM") or next(iter(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome")), None)

# config.example.yaml 의 실제 UVIS 설정과 같은 값
REFRESH = "js:document.querySelector('#chkUseGroup').checked = false; getDataLoad('', '', '', '', 'N', 'Y')"
FIELDS = {"vehicle_id": "cmNumber", "date": "biDate", "timestamp": "biTime",
          "lat": "biXPosition", "lon": "biYPosition", "speed": "biGpsSpeed", "address": "dbAddress"}


@unittest.skipUnless(HAVE_PW, "playwright 미설치")
class WebSourceTest(unittest.TestCase):
    def _base(self, site):
        cfg = {"login_url": site.url + "/uvis/login.do", "monitor_url": site.url + "/uvis/mainFrame.do",
               "username": "tester", "password": "secret", "headless": True, "wait_sec": 10,
               "frame_url_contains": "carLocation.do", "refresh": REFRESH}
        if CHROME:
            cfg["executable_path"] = CHROME
        return cfg

    def test_uvis_login_flow_network_and_table_modes(self):
        from uvis_monitor.sources.uvis_web import UvisWebSource
        now = datetime(2026, 10, 29, 6, 31, tzinfo=KST)
        with MockUvis() as site, tempfile.TemporaryDirectory() as td:
            net = UvisWebSource({**self._base(site), "mode": "network",
                                 "network": {"url_contains": "carLocationVehicleList.ajax", "records_path": "carList",
                                             "fields": FIELDS}}, Path(td))
            try:
                ps = net.fetch(now)  # 로그인 → 비밀번호 변경 안내 → 간편 접속 → 메인 프레임
                self.assertEqual([p.vehicle_id for p in ps], ["경기70아1001", "경기70아1002", "경기70아1003"])  # 그룹별 해제
                self.assertEqual((ps[0].lat, ps[0].lon, ps[0].ts), (37.2664, 127.0001, datetime(2026, 10, 29, 6, 30, tzinfo=KST)))
                self.assertEqual(ps[2].ts, datetime(2026, 10, 27, 20, 36, 39, tzinfo=KST))  # 오래된 위치는 그 날짜 그대로
                site.vehicles[0]["biXPosition"] = "37.270000"
                self.assertEqual(net.fetch(now)[0].lat, 37.27)  # 새로고침마다 최신 데이터
            finally:
                net.close()
            table = UvisWebSource({**self._base(site), "mode": "table",
                                   "table": {"row_selector": "#carList tbody tr",
                                             "columns": {"vehicle_id": 0, "timestamp": 1, "lat": 2, "lon": 3}}},
                                  Path(td))
            try:
                self.assertEqual(len(table.fetch(now)), 3)  # 프레임 안의 표
            finally:
                table.close()
            self.assertEqual(site.login_count, 1)  # 저장된 로그인 쿠키 재사용

    def test_wrong_password_reports_clear_error(self):
        from uvis_monitor.sources.uvis_web import UvisWebSource
        with MockUvis() as site, tempfile.TemporaryDirectory() as td:
            src = UvisWebSource({**self._base(site), "password": "wrong", "mode": "network",
                                 "network": {"url_contains": "carLocationVehicleList"}}, Path(td))
            try:
                with self.assertRaisesRegex(RuntimeError, "로그인 실패.*아이디 또는 비밀번호"):
                    src.fetch(datetime.now(KST))
            finally:
                src.close()

    def test_quick_access_dialog_must_be_accepted(self):
        from uvis_monitor.sources.uvis_web import UvisWebSource
        with MockUvis() as site, tempfile.TemporaryDirectory() as td:
            src = UvisWebSource({**self._base(site), "after_login": [], "mode": "network",
                                 "network": {"url_contains": "carLocationVehicleList"}}, Path(td))
            try:
                with self.assertRaisesRegex(RuntimeError, "after_login"):
                    src.fetch(datetime.now(KST))
            finally:
                src.close()


if __name__ == "__main__":
    unittest.main()
