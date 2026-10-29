"""좌표 계산: 거리, 선분-원(지오펜스) 교차.

GPS 위치는 보통 30초~수 분 간격으로 들어오므로, 버스가 정류장 반경을
두 샘플 사이에 '스쳐 지나가는' 경우가 많다. 그래서 점-점 거리만 보지 않고
직전 위치→현재 위치 선분이 반경 원과 만나는지를 계산해 도착 시각을 보간한다.
"""
from __future__ import annotations

import math

EARTH_R = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _to_local(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """(lat0, lon0) 기준 평면 좌표(m). 수 km 범위에서 오차 0.5% 미만."""
    x = math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def segment_circle(a: tuple[float, float], b: tuple[float, float],
                   center: tuple[float, float], radius_m: float) -> tuple[float, float] | None:
    """선분 a→b 가 center 반경 radius_m 원 안에 있는 구간 [t_in, t_out] (0~1 비율).

    선분이 원과 만나지 않으면 None. a 가 이미 원 안이면 t_in = 0.
    """
    ax, ay = _to_local(a[0], a[1], center[0], center[1])
    bx, by = _to_local(b[0], b[1], center[0], center[1])
    dx, dy = bx - ax, by - ay
    qa = dx * dx + dy * dy
    qc = ax * ax + ay * ay - radius_m * radius_m
    if qa < 1e-9:  # 정지 상태(같은 위치)
        return (0.0, 1.0) if qc <= 0 else None
    qb = 2 * (ax * dx + ay * dy)
    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    t1 = (-qb - sq) / (2 * qa)
    t2 = (-qb + sq) / (2 * qa)
    if t2 < 0 or t1 > 1:
        return None
    return max(0.0, t1), min(1.0, t2)
