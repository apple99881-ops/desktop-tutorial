import unittest
from datetime import time, timedelta

from uvis_monitor.models import AnomalyKind, AnomalyStatus, Level, PointKind, PointStatus, Route, RoutePoint, Rules
from uvis_monitor.tracker import TripTracker

from helpers import D, LAT0, M_PER_DEG_LAT, at, drive, make_route, pos

NORMAL = [(at(6, 10), -1500), (at(6, 20), 0), (at(6, 30), 0), (at(6, 40), 2000), (at(6, 41), 2000),
          (at(6, 50), 4000), (at(6, 51), 4000), (at(7, 0), 6000), (at(7, 20), 6000)]


def keys(alerts):
    return [a.key.split(":", 2)[-1] for a in alerts]


def kinds(tracker):
    return {a.kind for a in tracker.active_anomalies()}


class TrackerTest(unittest.TestCase):
    def test_normal_run_has_no_anomaly(self):
        t = TripTracker(make_route(), D)
        alerts = drive(t, NORMAL)
        self.assertTrue(t.completed)
        self.assertEqual(kinds(t), set())
        self.assertFalse([a for a in alerts if a.level in (Level.CRITICAL, Level.WARNING)])
        dep = (t.actual[0] - t.planned[0]).total_seconds()
        self.assertTrue(0 <= dep < 60, dep)  # 반경 100m 이탈 시각 ≈ 06:30:30
        self.assertEqual(t.route_status(at(7, 20)), PointStatus.OK)

    def test_departure_delay_alerts_then_confirms(self):
        tl = [(at(6, 10), -1500), (at(6, 20), 0), (at(6, 37), 0), (at(6, 47), 2000), (at(7, 10), 6000)]
        t = TripTracker(make_route(), D)
        alerts = drive(t, tl)
        self.assertEqual(keys(alerts)[:2], ["dep_pending", "departed_late"])
        self.assertEqual(alerts[0].level, Level.CRITICAL)
        self.assertEqual(alerts[0].created_at, at(6, 35))  # 계획 06:30 + 5분에 즉시 알림
        a = t.anomalies[(0, AnomalyKind.DEPARTURE_DELAY)]
        self.assertEqual(a.status, AnomalyStatus.CONFIRMED)
        self.assertGreaterEqual(a.delay_min, 7)
        self.assertIn("미출발 알림", a.note)

    def test_small_departure_delay_is_caution_only(self):
        tl = [(at(6, 10), -1500), (at(6, 20), 0), (at(6, 34), 0), (at(6, 44), 2000), (at(7, 4), 6000)]
        t = TripTracker(make_route(), D)
        alerts = drive(t, tl)
        self.assertNotIn("dep_pending", keys(alerts))
        self.assertEqual(kinds(t), set())
        self.assertEqual(t.point_results(at(7, 10))[0].status, PointStatus.CAUTION)

    def test_stop_delay_pending_then_late_arrival(self):
        tl = [(at(6, 20), 0), (at(6, 30), 0), (at(6, 35), 1000), (at(6, 52), 1000), (at(6, 55), 2000),
              (at(7, 5), 4000), (at(7, 15), 6000)]
        t = TripTracker(make_route(), D)
        alerts = drive(t, tl)
        self.assertIn("stop_pending:1", keys(alerts))
        pend = next(a for a in alerts if a.key.endswith("stop_pending:1"))
        self.assertEqual(pend.created_at, at(6, 50))
        a = t.anomalies[(1, AnomalyKind.STOP_DELAY)]
        self.assertEqual(a.status, AnomalyStatus.CONFIRMED)
        self.assertGreaterEqual(a.delay_min, 14)

    def test_skipped_stop(self):
        t = TripTracker(make_route(), D)
        detour = lambda tm: 600 if at(6, 44) <= tm <= at(6, 56) else 0  # noqa: E731
        drive(t, NORMAL, east=detour)
        self.assertIn(AnomalyKind.STOP_SKIPPED, kinds(t))
        self.assertTrue(t.skipped[2])
        self.assertTrue(t.completed)

    def test_no_signal_and_recovery(self):
        t = TripTracker(make_route(), D)
        alerts = drive(t, NORMAL, gaps=[(at(6, 42), at(6, 56))])
        ks = keys(alerts)
        self.assertTrue(any(k.startswith("nosignal") for k in ks))
        self.assertTrue(any(k.startswith("signal_back") for k in ks))
        a = next(a for a in t.anomalies.values() if a.kind == AnomalyKind.NO_SIGNAL)
        self.assertEqual(a.status, AnomalyStatus.CONFIRMED)
        self.assertIn("추정", t.notes[2])  # 미수신 구간에 지난 정류장B 도착시각은 추정
        self.assertEqual(t.route_status(at(7, 20)), PointStatus.CAUTION)

    def test_early_departure(self):
        tl = [(at(6, 10), -1500), (at(6, 15), 0), (at(6, 25), 0), (at(6, 35), 2000), (at(7, 0), 6000)]
        t = TripTracker(make_route(), D)
        alerts = drive(t, tl)
        self.assertIn("departed_early", keys(alerts))
        self.assertIn(AnomalyKind.EARLY_DEPARTURE, kinds(t))

    def test_deadhead_past_stop_is_not_departure(self):
        # 차고지에서 기점으로 오는 길에 정류장A 를 먼저 지나치는 경우
        tl = [(at(5, 55), 3000), (at(6, 5), 2000), (at(6, 20), 0)] + NORMAL[2:]
        t = TripTracker(make_route(), D)
        drive(t, tl)
        self.assertEqual(kinds(t), set())
        self.assertLess(abs((t.actual[0] - t.planned[0]).total_seconds()), 60)

    def test_passing_through_origin_early_then_returning(self):
        # 06:05 기점을 남쪽으로 지나쳐 차고지에 갔다가 06:20 기점으로 복귀
        tl = [(at(5, 55), 1500), (at(6, 5), 0), (at(6, 10), -1500), (at(6, 20), 0)] + NORMAL[2:]
        t = TripTracker(make_route(), D)
        alerts = drive(t, tl)
        self.assertNotIn("departed_early", keys(alerts))
        self.assertEqual(kinds(t), set())

    def test_no_data_at_all(self):
        t = TripTracker(make_route(), D)
        for m in range(0, 70):
            t.evaluate(at(6, 0) + timedelta(minutes=m))
        alerts = t.pop_alerts()
        self.assertEqual(keys(alerts), ["dep_pending"])
        self.assertIn("위치 미수신", "\n".join(alerts[0].lines))
        t.finalize(at(9, 30))
        a = t.anomalies[(0, AnomalyKind.DEPARTURE_DELAY)]
        self.assertEqual(a.status, AnomalyStatus.ONGOING)
        self.assertIn("감시 종료", a.note)

    def test_vehicle_id_spacing_is_ignored(self):
        t = TripTracker(make_route(vehicle="경기70아1234"), D)
        t.ingest(pos(at(6, 20), 0, vehicle="경기 70아 1234"))
        self.assertTrue(t.inside_origin)

    def test_sparse_polling_catches_every_stop(self):
        t = TripTracker(make_route(), D)
        drive(t, NORMAL, step_sec=420)  # 7분 간격 – 한 구간에 정류장을 스쳐 지나감
        self.assertTrue(t.completed)
        self.assertEqual(kinds(t), set())
        self.assertTrue(all(a is not None for a in t.actual))

    def test_route_rules_override(self):
        rules = Rules(default_radius_m=100, departure_delay_min=8)
        tl = [(at(6, 20), 0), (at(6, 37), 0), (at(6, 47), 2000), (at(7, 10), 6000)]
        t = TripTracker(make_route(rules), D)
        alerts = drive(t, tl)
        self.assertNotIn("dep_pending", keys(alerts))
        self.assertNotIn(AnomalyKind.DEPARTURE_DELAY, kinds(t))

    def test_duplicate_and_out_of_order_positions_ignored(self):
        t = TripTracker(make_route(), D)
        t.ingest(pos(at(6, 20), 0))
        t.ingest(pos(at(6, 19), 5000))
        t.ingest(pos(at(6, 20), 5000))
        self.assertTrue(t.inside_origin)
        self.assertEqual(t.next_idx, 0)

    def test_late_start_does_not_flag_skipped_stops(self):
        # 프로그램이 07:30 에 처음 켜졌고 버스는 이미 종점에 있음 → 미경유/지연이 아니라 '미확인'
        t = TripTracker(make_route(), D)
        alerts = drive(t, [(at(7, 30), 6000), (at(7, 40), 6000)])
        self.assertEqual(kinds(t), set())
        self.assertFalse([a for a in alerts if a.level in (Level.CRITICAL, Level.WARNING)])
        self.assertEqual({p.status for p in t.point_results(at(7, 40))}, {PointStatus.UNKNOWN})

    def test_late_start_mid_route_then_normal(self):
        # 06:45 에 처음 켜짐(버스는 정류장A~B 사이) → A 는 미확인, 이후 B·종점은 정상 판정
        t = TripTracker(make_route(), D)
        drive(t, [(at(6, 45), 3000)] + NORMAL[4:])
        self.assertEqual(kinds(t), set())
        res = t.point_results(at(7, 20))
        self.assertEqual(res[1].status, PointStatus.UNKNOWN)
        self.assertEqual(res[2].status, PointStatus.OK)
        self.assertTrue(t.completed)

    def test_gap_hides_stop_is_unknown_not_skipped(self):
        # 위치 공백(06:44~06:56) 동안 우회 경로로 정류장B 를 지났는지 알 수 없음
        t = TripTracker(make_route(), D)
        detour = lambda tm: 600 if at(6, 44) <= tm <= at(6, 56) else 0  # noqa: E731
        drive(t, NORMAL, east=detour, gaps=[(at(6, 44), at(6, 57))])
        self.assertNotIn(AnomalyKind.STOP_SKIPPED, kinds(t))

    def test_late_start_long_route_bus_parked_at_destination(self):
        base = make_route()
        stops = [RoutePoint(f"S{i}", base.origin.lat + i * 0.005, base.origin.lon, time(6, 30 + i * 2),
                            PointKind.STOP, 100) for i in range(1, 7)]
        dest = RoutePoint("종점", base.origin.lat + 0.04, base.origin.lon, time(6, 50), PointKind.DESTINATION, 150)
        route = Route("L", "긴노선", base.vehicle_id, (base.origin, *stops, dest), base.rules)
        t = TripTracker(route, D)
        meters = (dest.lat - LAT0) * M_PER_DEG_LAT
        alerts = drive(t, [(at(8, 0), meters), (at(8, 10), meters)])  # 08:00 첫 위치 = 종점에 정차
        self.assertTrue(t.completed)
        self.assertFalse([a for a in alerts if a.level in (Level.CRITICAL, Level.WARNING)])


if __name__ == "__main__":
    unittest.main()
