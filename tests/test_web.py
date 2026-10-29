"""UVIS 웹 자동조회 시험 (가짜 UVIS 사이트 + 실제 브라우저). Playwright 가 없으면 건너뜀."""
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


@unittest.skipUnless(HAVE_PW, "playwright 미설치")
class WebSourceTest(unittest.TestCase):
    def _base(self, site):
        cfg = {"login_url": site.url + "/login", "monitor_url": site.url + "/monitor", "username": "tester",
               "password": "secret", "headless": True, "wait_sec": 10}
        if CHROME:
            cfg["executable_path"] = CHROME
        return cfg

    def test_network_and_table_modes_with_cookie_reuse(self):
        from uvis_monitor.sources.uvis_web import UvisWebSource
        now = datetime(2026, 10, 29, 6, 31, tzinfo=KST)
        with MockUvis() as site, tempfile.TemporaryDirectory() as td:
            net = UvisWebSource({**self._base(site), "mode": "network", "refresh": "click:#btnRefresh",
                                 "network": {"url_contains": "/api/vehicle/location", "records_path": "data.list",
                                             "fields": {"vehicle_id": "carNo", "timestamp": "gpsDt",
                                                        "lat": "lat", "lon": "lng", "speed": "spd"}}}, Path(td))
            try:
                ps = net.fetch(now)
                self.assertEqual([p.vehicle_id for p in ps], ["경기70아1001", "경기70아1002"])
                self.assertEqual(ps[0].lat, 37.2664)
                site.vehicles[0]["lat"] = "37270000"
                self.assertEqual(net.fetch(now)[0].lat, 37.27)  # 새로고침마다 최신 데이터
            finally:
                net.close()
            table = UvisWebSource({**self._base(site), "mode": "table", "refresh": "reload",
                                   "table": {"row_selector": "#carList tbody tr",
                                             "columns": {"vehicle_id": 0, "timestamp": 1, "lat": 2, "lon": 3}}},
                                  Path(td))
            try:
                self.assertEqual(len(table.fetch(now)), 2)
            finally:
                table.close()
            self.assertEqual(site.login_count, 1)  # 저장된 로그인 쿠키 재사용

    def test_wrong_password_reports_clear_error(self):
        from uvis_monitor.sources.uvis_web import UvisWebSource
        with MockUvis() as site, tempfile.TemporaryDirectory() as td:
            src = UvisWebSource({**self._base(site), "password": "wrong", "mode": "network",
                                 "network": {"url_contains": "/api/"}}, Path(td))
            with self.assertRaisesRegex(RuntimeError, "로그인 실패"):
                src.fetch(datetime.now(KST))


if __name__ == "__main__":
    unittest.main()
