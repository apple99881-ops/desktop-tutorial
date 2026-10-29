import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from uvis_monitor.config import load_config, load_routes, parse_time, ConfigError
from uvis_monitor.models import KST
from uvis_monitor.monitor import Monitor, SimClock, run_replay
from uvis_monitor.notify import NotifierHub
from uvis_monitor.notify.base import Notifier
from uvis_monitor.report import build_monthly_report
from uvis_monitor.sources.simulator import SimulatorSource
from uvis_monitor.storage import Store

ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 10, 29)


class Recorder(Notifier):
    name = "rec"

    def __init__(self):
        super().__init__()
        self.texts = []
        self.files = []

    def send_text(self, text):
        self.texts.append(text)
        return True

    def send_file(self, path, caption=""):
        self.files.append(path)
        return True


def setup_cfg(tmp: Path):
    cfg = load_config(ROOT / "config" / "config.example.yaml")
    cfg.database = tmp / "t.db"
    cfg.report_dir = tmp / "reports"
    cfg.routes_file = ROOT / "config" / "routes.example.yaml"
    routes = load_routes(cfg.routes_file, cfg.rules)
    return cfg, routes


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg, self.routes = setup_cfg(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_demo_detects_every_scenario_and_writes_report(self):
        rec = Recorder()
        store = Store(self.cfg.database)
        result = run_replay(self.cfg, self.routes, SimulatorSource(self.routes, D), NotifierHub([rec]), store, D,
                            notify=True)
        found = {(a["route_id"], a["kind"]) for a in store.anomalies(D)}
        expected = {("R02", "출발지연"), ("R03", "정차지연"), ("R04", "조기출발"), ("R05", "미경유"),
                    ("R06", "위치미수신"), ("R07", "출발지연")}
        self.assertTrue(expected <= found, found)
        self.assertFalse({r for r, _ in found} & {"R01", "R08"})  # 정상 노선은 이상 없음
        self.assertTrue(result.report_path.is_file())
        wb = load_workbook(result.report_path)
        self.assertEqual(wb.sheetnames, ["요약", "이상발생내역", "노선별상세", "알림이력", "판정기준"])
        self.assertIn("일일점검", rec.texts[-1])
        self.assertEqual(rec.files, [result.report_path])
        keys = [a["alert_key"] for a in store.alerts(D) if a["alert_key"]]
        self.assertEqual(len(keys), len(set(keys)))  # 같은 알림은 한 번만

    def test_restart_restores_state_without_resending(self):
        source = SimulatorSource(self.routes, D)
        store = Store(self.cfg.database)
        start = datetime(2026, 10, 29, 6, 0, tzinfo=KST)
        crash = datetime(2026, 10, 29, 6, 50, tzinfo=KST)
        rec1 = Recorder()
        mon = Monitor(self.cfg, self.routes, source, NotifierHub([rec1]), store, SimClock(start), restore=False)
        mon.opts = {"start_message": False}
        mon.run_day(D, end=crash)  # 06:50 에 프로그램이 죽었다고 가정
        sent_before = len(rec1.texts)

        rec2 = Recorder()
        clock = SimClock(crash)
        mon2 = Monitor(self.cfg, self.routes, SimulatorSource(self.routes, D), NotifierHub([rec2]), store, clock)
        mon2.opts = {"start_message": False, "send_report_file": False}
        mon2.run_day(D)
        alert_texts = [t for t in rec2.texts if "일일점검" not in t]
        self.assertFalse(any("[출발지연] 2호차" in t for t in alert_texts))  # 06:40 알림 재발송 안 함
        self.assertTrue(any("[지연출발 확인] 2호차" in t for t in rec1.texts + rec2.texts))
        self.assertGreater(sent_before, 0)
        kinds = {(a["route_id"], a["kind"]) for a in store.anomalies(D)}
        self.assertIn(("R03", "정차지연"), kinds)

    def test_monthly_report(self):
        store = Store(self.cfg.database)
        hub = NotifierHub([])
        for d in (date(2026, 10, 28), date(2026, 10, 29)):
            run_replay(self.cfg, self.routes, SimulatorSource(self.routes, d), hub, store, d, notify=False)
        path = build_monthly_report(store, "2026-10", self.cfg.rules, self.cfg.report_dir)
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["월간요약", "일별매트릭스", "월간이상내역"])
        ws = wb["월간요약"]
        self.assertEqual(ws["A5"].value, "R01")
        self.assertEqual(ws["C5"].value, 2)
        self.assertTrue(str(ws["E5"].value).startswith("=COUNTIFS("))


class ConfigTest(unittest.TestCase):
    def test_time_parsing(self):
        self.assertEqual(parse_time(390).strftime("%H:%M"), "06:30")  # YAML 60진수
        self.assertEqual(parse_time("6:05").strftime("%H:%M"), "06:05")
        with self.assertRaises(ConfigError):
            parse_time("25:99")

    def test_reversed_times_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "routes.yaml"
            p.write_text("""routes:
  - id: X
    vehicle_id: A
    origin: {name: a, lat: 37.1, lon: 127.1, time: "07:00"}
    destination: {name: b, lat: 37.2, lon: 127.2, time: "06:50"}
""", encoding="utf-8")
            from uvis_monitor.models import Rules
            with self.assertRaises(ConfigError):
                load_routes(p, Rules())

    def test_env_expansion(self):
        import os
        from uvis_monitor.config import expand_env
        os.environ["UVIS_TEST_X"] = "abc"
        self.assertEqual(expand_env({"a": ["${UVIS_TEST_X}", "${NOPE:-def}"]}), {"a": ["abc", "def"]})


if __name__ == "__main__":
    unittest.main()
