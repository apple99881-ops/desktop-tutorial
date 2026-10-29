"""가상 차량 시뮬레이터 — UVIS 연결 전에 전체 흐름(감시→알림→엑셀)을 시험하는 용도.

노선별 scenario 로 상황을 재현한다 (routes.yaml 의 scenario 항목, 없으면 무작위):
    normal            정상 운행
    departure_delay   기점 7~9분 지연 출발
    early_departure   기점 4~5분 조기 출발
    stop_delay        중간 구간 정체로 정차지 12분 이상 지연
    skip_stop         중간 정차지 미경유(우회)
    no_signal         운행 중 13분간 위치 미수신
    not_departed      차량 고장 등으로 기점 미출발
"""
from __future__ import annotations

import math
import random
from bisect import bisect_right
from datetime import date, datetime, timedelta

from ..models import Route, VehiclePosition, minutes_between, norm_vid
from .base import PositionSource

SCENARIOS = ("normal", "departure_delay", "early_departure", "stop_delay", "skip_stop", "no_signal", "not_departed")
_RANDOM_WEIGHTS = (60, 10, 5, 10, 5, 5, 5)


def _offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


def _detour(prev, stop, nxt, dist_m: float) -> tuple[float, float]:
    """이전→다음 지점 진행방향에 수직으로 dist_m 만큼 비켜난 우회 지점."""
    north = (nxt.lat - prev.lat) * 111_320.0
    east = (nxt.lon - prev.lon) * 111_320.0 * math.cos(math.radians(stop.lat))
    norm = math.hypot(north, east) or 1.0
    return _offset(stop.lat, stop.lon, -east / norm * dist_m, north / norm * dist_m)


class _VehiclePlan:
    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id
        self.waypoints: list[tuple[datetime, float, float]] = []
        self.gaps: list[tuple[datetime, datetime]] = []
        self.active_from: datetime | None = None

    def add(self, t: datetime, lat: float, lon: float) -> None:
        if self.waypoints and t <= self.waypoints[-1][0]:
            t = self.waypoints[-1][0] + timedelta(seconds=1)
        self.waypoints.append((t, lat, lon))

    def position(self, now: datetime) -> tuple[float, float, float] | None:
        if not self.waypoints or (self.active_from and now < self.active_from):
            return None
        if any(a <= now < b for a, b in self.gaps):
            return None
        times = [w[0] for w in self.waypoints]
        i = bisect_right(times, now)
        if i == 0:
            return self.waypoints[0][1], self.waypoints[0][2], 0.0
        if i >= len(self.waypoints):
            return self.waypoints[-1][1], self.waypoints[-1][2], 0.0
        (t0, la0, lo0), (t1, la1, lo1) = self.waypoints[i - 1], self.waypoints[i]
        f = (now - t0) / (t1 - t0)
        lat, lon = la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f
        dist = math.hypot((la1 - la0) * 111_320, (lo1 - lo0) * 111_320 * math.cos(math.radians(la0)))
        speed = dist / max(1.0, (t1 - t0).total_seconds()) * 3.6
        return lat, lon, speed


class SimulatorSource(PositionSource):
    name = "simulator"

    def __init__(self, routes: list[Route], service_date: date, seed: int = 42, noise_m: float = 8.0):
        self.service_date = service_date
        self.noise_m = noise_m
        self.rng = random.Random(f"{seed}:{service_date}")
        self.scenarios: dict[str, str] = {}
        self.plans: dict[str, _VehiclePlan] = {}
        for route in sorted(routes, key=lambda r: r.points[0].planned):
            plan = self.plans.setdefault(norm_vid(route.vehicle_id), _VehiclePlan(route.vehicle_id))
            self._build(route, plan, random.Random(f"{seed}:{route.id}:{service_date}"))

    def _build(self, route: Route, plan: _VehiclePlan, rng: random.Random) -> None:
        sc = route.scenario or rng.choices(SCENARIOS, weights=_RANDOM_WEIGHTS)[0]
        if sc not in SCENARIOS:
            raise ValueError(f"[노선 {route.id}] 알 수 없는 scenario: {sc} (가능: {', '.join(SCENARIOS)})")
        self.scenarios[route.id] = sc
        d = self.service_date
        planned = [route.planned_at(i, d) for i in range(len(route.points))]
        o = route.origin
        garage = _offset(o.lat, o.lon, -1500, -2000)
        if plan.active_from is None:
            plan.active_from = planned[0] - timedelta(minutes=35)
            plan.add(plan.active_from, *garage)

        if sc == "departure_delay":
            dep = planned[0] + timedelta(minutes=rng.uniform(7, 9))
            arrive_origin = planned[0] + timedelta(minutes=2)
        elif sc == "early_departure":
            dep = planned[0] - timedelta(minutes=rng.uniform(4, 5))
            arrive_origin = planned[0] - timedelta(minutes=12)
        else:
            dep = planned[0] + timedelta(minutes=rng.uniform(-0.5, 1.5))
            arrive_origin = planned[0] - timedelta(minutes=10)
        plan.add(arrive_origin - timedelta(minutes=12), *garage)
        plan.add(arrive_origin, o.lat, o.lon)
        if sc == "not_departed":
            plan.add(planned[-1] + timedelta(hours=3), o.lat, o.lon)
            return
        plan.add(dep, o.lat, o.lon)

        n = len(route.points)
        slow_seg = rng.randrange(1, n) if n > 2 else 1
        skip_idx = rng.randrange(1, n - 1) if n > 2 else None
        t = dep
        for i in range(1, n):
            p = route.points[i]
            seg = max(1.0, minutes_between(planned[i], planned[i - 1]))
            travel = seg * rng.uniform(0.9, 1.0) - (0.7 if i > 1 else 0)
            if sc == "stop_delay" and i == slow_seg:
                travel += rng.uniform(12, 15)
            t = t + timedelta(minutes=max(1.0, travel))
            if sc == "skip_stop" and i == skip_idx:
                plan.add(t, *_detour(route.points[i - 1], p, route.points[i + 1], 800))
                continue
            plan.add(t, p.lat, p.lon)
            if i < n - 1:
                t = t + timedelta(minutes=0.7)  # 정차(승차) 시간
                plan.add(t, p.lat, p.lon)
            if sc == "no_signal" and i == 1:
                gap_start = t + timedelta(minutes=1)
                plan.gaps.append((gap_start, gap_start + timedelta(minutes=13)))
        plan.add(t + timedelta(hours=3), route.destination.lat, route.destination.lon)

    def fetch(self, now: datetime) -> list[VehiclePosition]:
        out = []
        for plan in self.plans.values():
            pos = plan.position(now)
            if pos is None:
                continue
            lat, lon, speed = pos
            if self.noise_m:
                lat, lon = _offset(lat, lon, self.rng.gauss(0, self.noise_m / 2), self.rng.gauss(0, self.noise_m / 2))
            out.append(VehiclePosition(vehicle_id=plan.vehicle_id, ts=now, lat=lat, lon=lon,
                                       speed_kmh=round(speed, 1), address=""))
        return out
