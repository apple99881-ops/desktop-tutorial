"""테스트 공용 도우미: 직선 노선과 위치 생성."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from uvis_monitor.models import KST, PointKind, Route, RoutePoint, Rules, VehiclePosition

D = date(2026, 10, 29)  # 목요일
LAT0, LON0 = 37.25, 127.00
M_PER_DEG_LAT = 111_195.0


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(D.year, D.month, D.day, h, m, s, tzinfo=KST)


def north(meters: float) -> float:
    return LAT0 + meters / M_PER_DEG_LAT


def make_route(rules: Rules | None = None, vehicle: str = "경기70아1234") -> Route:
    """남→북 직선 노선: 기점(0m) 06:30 → A(2km) 06:40 → B(4km) 06:50 → 종점(6km) 07:00"""
    rules = rules or Rules(default_radius_m=100)
    pts = [
        RoutePoint("기점", north(0), LON0, time(6, 30), PointKind.ORIGIN, 100),
        RoutePoint("정류장A", north(2000), LON0, time(6, 40), PointKind.STOP, 100),
        RoutePoint("정류장B", north(4000), LON0, time(6, 50), PointKind.STOP, 100),
        RoutePoint("종점", north(6000), LON0, time(7, 0), PointKind.DESTINATION, 150),
    ]
    return Route(id="T1", name="시험노선", vehicle_id=vehicle, points=tuple(pts), rules=rules)


def pos(t: datetime, meters: float, vehicle: str = "경기70아1234", east_m: float = 0) -> VehiclePosition:
    lon = LON0 + east_m / (M_PER_DEG_LAT * 0.7955)
    return VehiclePosition(vehicle, t, north(meters), lon)


def drive(tracker, timeline: list[tuple[datetime, float]], step_sec: int = 60, end: datetime | None = None,
          gaps: list[tuple[datetime, datetime]] = (), east=None):
    """timeline 의 (시각, 거리m) 사이를 직선 보간하며 step_sec 마다 ingest/evaluate."""
    t = timeline[0][0]
    end = end or timeline[-1][0]
    alerts = []
    while t <= end:
        if not any(a <= t < b for a, b in gaps):
            m = _interp(timeline, t)
            tracker.set_now(t)
            tracker.ingest(pos(t, m, east_m=east(t) if east else 0))
        tracker.evaluate(t)
        alerts += tracker.pop_alerts()
        t += timedelta(seconds=step_sec)
    return alerts


def _interp(timeline, t):
    if t <= timeline[0][0]:
        return timeline[0][1]
    for (t0, m0), (t1, m1) in zip(timeline, timeline[1:]):
        if t0 <= t <= t1:
            f = (t - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return m0 + (m1 - m0) * f
    return timeline[-1][1]
