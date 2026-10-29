import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

from uvis_monitor.models import KST
from uvis_monitor.sources.excel_export import ExcelSource, inspect, load_positions
from uvis_monitor.sources.mapping import get_path, normalize_coord, parse_timestamp, records_to_positions


class MappingTest(unittest.TestCase):
    def test_timestamp_formats(self):
        d = date(2026, 10, 29)
        expect = datetime(2026, 10, 29, 6, 31, 5, tzinfo=KST)
        for v in ["2026-10-29 06:31:05", "2026.10.29 06:31:05", "20261029063105", "2026-10-29 오전 6:31:05",
                  "2026-10-29T06:31:05+09:00", "06:31:05", "2026/10/29 06:31:05"]:
            self.assertEqual(parse_timestamp(v, "auto", d), expect, v)
        self.assertEqual(parse_timestamp(int(expect.timestamp() * 1000)), expect)
        self.assertEqual(parse_timestamp("29/10/2026 06:31:05", "%d/%m/%Y %H:%M:%S"), expect)
        self.assertIsNone(parse_timestamp("엉망"))

    def test_coordinates(self):
        self.assertEqual(normalize_coord("37266400", "lat"), 37.2664)
        self.assertEqual(normalize_coord("127000100", "lon"), 127.0001)
        self.assertIsNone(normalize_coord("", "lat"))

    def test_records_with_path_and_swapped_columns(self):
        data = {"data": {"list": [{"car": {"no": "1호차"}, "dt": "20261029063000", "x": "37.1", "y": "127.2"}]}}
        recs = get_path(data, "data.list")
        ps = records_to_positions(recs, {"vehicle_id": "car.no", "timestamp": "dt", "lat": "y", "lon": "x"})
        self.assertEqual((ps[0].vehicle_id, ps[0].lat, ps[0].lon), ("1호차", 37.1, 127.2))  # 뒤바뀐 위경도 자동 교정

    def test_uvis_compact_date_and_time_fields(self):
        # 실제 UVIS carLocationVehicleList.ajax 형태: 날짜·시각이 따로, biXPosition 이 위도
        d = date(2026, 10, 29)
        self.assertEqual(parse_timestamp("20261029"), datetime(2026, 10, 29, tzinfo=KST))
        self.assertEqual(parse_timestamp("073758", "auto", d), datetime(2026, 10, 29, 7, 37, 58, tzinfo=KST))
        self.assertEqual(parse_timestamp("1820", "auto", d), datetime(2026, 10, 29, 18, 20, tzinfo=KST))
        self.assertIsNone(parse_timestamp("073758"))  # 날짜를 모르면 해석하지 않음
        fields = {"vehicle_id": "cmNumber", "date": "biDate", "timestamp": "biTime",
                  "lat": "biXPosition", "lon": "biYPosition", "speed": "biGpsSpeed"}
        recs = [{"cmNumber": "경기70아1001", "biDate": "20261029", "biTime": "160743",
                 "biXPosition": "37.244984", "biYPosition": "127.043784", "biGpsSpeed": 41},
                {"cmNumber": "경기70아1002", "biDate": "20261027", "biTime": "203639",  # 이틀 전 마지막 위치
                 "biXPosition": "37.268644", "biYPosition": "126.999616", "biGpsSpeed": 0}]
        ps = records_to_positions(recs, fields, service_date=d)
        self.assertEqual(ps[0].ts, datetime(2026, 10, 29, 16, 7, 43, tzinfo=KST))
        self.assertEqual((ps[0].lat, ps[0].lon, ps[0].speed_kmh), (37.244984, 127.043784, 41))
        self.assertEqual(ps[1].ts, datetime(2026, 10, 27, 20, 36, 39, tzinfo=KST))  # 오늘로 착각하지 않음


class ExcelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _xlsx(self, name="운행경로.xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.append(["차량 운행경로 조회"])
        ws.append(["조회기간: 2026-10-29"])
        ws.append([])
        ws.append(["No", "차량번호", "수신일시", "속도(km/h)", "위도", "경도", "주소"])
        ws.append([1, "경기70아1001", datetime(2026, 10, 29, 6, 30), 0, 37.2664, 127.0001, "수원역"])
        ws.append([2, "경기70아1001", "2026-10-29 06:31:00", 25, 37.2670, 127.0050, "매산로"])
        ws.append([3, "경기70아1002", "2026-10-29 06:31:30", 10, 37200100, 127096000, ""])
        path = self.dir / name
        wb.save(path)
        return path

    def test_xlsx_with_title_rows(self):
        ps = load_positions(self._xlsx())
        self.assertEqual(len(ps), 3)
        self.assertEqual(ps[1].speed_kmh, 25)
        self.assertEqual(ps[2].lat, 37.2001)
        self.assertIn("자동 인식된 열", inspect(self._xlsx("b.xlsx")))

    def test_csv_cp949(self):
        path = self.dir / "export.csv"
        path.write_bytes("차량명,일시,위도,경도\n2호차,2026-10-29 06:40:00,37.2,127.1\n".encode("cp949"))
        ps = load_positions(path)
        self.assertEqual(ps[0].vehicle_id, "2호차")

    def test_html_disguised_as_xls_and_vehicle_from_filename(self):
        path = self.dir / "경기70아1003_운행경로.xls"
        path.write_text("<html><body><table><tr><th>시간</th><th>위도</th><th>경도</th></tr>"
                        "<tr><td>06:40:00</td><td>37.3</td><td>127.1</td></tr></table></body></html>",
                        encoding="utf-8")
        ps = load_positions(path, service_date=date(2026, 10, 29))
        self.assertEqual(ps[0].vehicle_id, "경기70아1003")
        self.assertEqual(ps[0].ts, datetime(2026, 10, 29, 6, 40, tzinfo=KST))

    def test_explicit_columns(self):
        path = self.dir / "custom.csv"
        path.write_text("CAR,WHEN,N,E\nA1,2026-10-29 06:00:00,37.1,127.1\n", encoding="utf-8")
        cols = {"vehicle_id": "CAR", "timestamp": "WHEN", "lat": "N", "lon": "E"}
        self.assertEqual(len(load_positions(path, columns=cols)), 1)
        with self.assertRaises(ValueError):
            load_positions(path)

    def test_source_returns_each_position_once_in_time(self):
        src = ExcelSource(files=[self._xlsx()])
        self.assertEqual(len(src.fetch(datetime(2026, 10, 29, 6, 30, 30, tzinfo=KST))), 1)
        self.assertEqual(len(src.fetch(datetime(2026, 10, 29, 6, 32, tzinfo=KST))), 2)
        self.assertEqual(src.fetch(datetime(2026, 10, 29, 7, 0, tzinfo=KST)), [])


if __name__ == "__main__":
    unittest.main()
