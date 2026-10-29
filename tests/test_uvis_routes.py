"""UVIS 노선관리 데이터 → routes.yaml 변환 시험 (실제 응답 형식을 본뜬 가짜 값)."""
import tempfile
import unittest
from pathlib import Path

import yaml

from uvis_monitor.config import Rules, load_routes
from uvis_monitor.sources.uvis_routes import build_routes


def _st(order, si, name, lat, lon, arr):
    return {"rmIdx": 9043, "rdOrder": order, "siIdx": si, "siName": name, "siLatitude": str(lat),
            "siLongitude": str(lon), "rdArrTime": str(arr), "siFlag": "2", "siTempFlag": "정상"}


RAW = [{
    "route": {"rmIdx": 9043, "rmName": "수원&#40;영통&#41;", "rmFlag": "2"},
    "stations": [
        _st(1, 1, "&#40;퇴&#41;본관 앞", 37.2880, 127.0530, 0),
        _st(3, 3, "&#40;퇴&#41;영통역", 37.2516, 127.0716, 30),
        _st(2, 2, "&#40;퇴&#41;매탄동", 37.2650, 127.0500, 20),
        _st(4, 3, "&#40;퇴&#41;영통역", 37.2516, 127.0716, 5),     # 같은 정류장 중복 등록
        _st(5, 4, "&#40;퇴&#41;영통역 2번출구", 37.2535, 127.0720, 5),  # 210m 옆 정류장
    ],
    "times": [{"rtKey": "1", "rtStart": "1820", "rtEnd": "1930", "rtDayName": "평일", "cmNumber": "경기70아1001"},
              {"rtKey": "2", "rtStart": "2000", "rtEnd": "2100", "rtDayName": "월수금", "cmNumber": ""}],
}, {
    "route": {"rmIdx": 9050, "rmName": "배차없음", "rmFlag": "2"}, "stations": [], "times": [],
}]


class BuildRoutesTest(unittest.TestCase):
    def test_trips_times_merging_and_radius(self):
        routes, warnings = build_routes(RAW)
        self.assertEqual([r["id"] for r in routes], ["U9043-1", "U9043-2"])  # 배차 1건 = 운행 1회
        a, b = routes
        self.assertEqual(a["name"], "[퇴근] 수원(영통) 18:20")
        self.assertEqual(a["origin"]["name"], "(퇴)본관 앞")  # HTML 문자 코드 풀기
        self.assertEqual([a["origin"]["time"]] + [s["time"] for s in a["stops"]] + [a["destination"]["time"]],
                         ["18:20", "18:40", "19:10", "19:20"])  # rdOrder 순, 소요분 누적, 중복 정류장 합침
        self.assertEqual(a["weekdays"], list("월화수목금"))
        self.assertNotIn("radius_m", a["origin"])  # 이웃과 멀면 기본 반경
        self.assertEqual(a["destination"]["radius_m"], 100)  # 210m 옆 정류장과 겹치지 않게
        self.assertIn("19:20 ≠ 배차 종료 19:30", a["note"])  # 소요분 합계와 배차 종료가 다르면 표시
        self.assertEqual(b["weekdays"], ["월", "수", "금"])
        self.assertIs(b["enabled"], False)  # 배정 차량 없음
        self.assertIn("차량이 없어", b["note"])
        self.assertTrue(any("배차없음" in w and "건너뜀" in w for w in warnings))

    def test_output_loads_as_routes_file(self):
        routes, _ = build_routes(RAW)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "routes.yaml"
            path.write_text(yaml.safe_dump({"routes": routes}, allow_unicode=True, sort_keys=False), encoding="utf-8")
            loaded = load_routes(path, Rules())
        self.assertEqual(len(loaded), 1)  # 차량 없는 운행은 꺼 둠
        self.assertEqual((loaded[0].vehicle_id, len(loaded[0].points)), ("경기70아1001", 4))
        self.assertEqual(loaded[0].points[-1].radius_m, 100)


if __name__ == "__main__":
    unittest.main()
