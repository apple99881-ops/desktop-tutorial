"""도메인 모델: 노선 · 지점(기점/정차지/종점) · 차량위치 · 이상내역 · 알림.

UVIS(유비스)에서 가져온 원시 위치 데이터는 모두 VehiclePosition 으로 변환된 뒤
감시 엔진(tracker.py)으로 들어간다. 엔진의 결과는 Anomaly(이상내역),
PointResult(지점별 계획/실제), Alert(메신저 알림) 세 가지다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

# 한국은 서머타임이 없으므로 고정 오프셋을 쓴다 (Windows 에서 tzdata 설치 불필요).
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def norm_vid(vehicle_id: str) -> str:
    """차량 식별자 비교용 정규화: 공백/하이픈 제거, 대문자."""
    return re.sub(r"[\s\-]", "", str(vehicle_id)).upper()


def minutes_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 60.0


def fmt_delay(minutes: float | None) -> str:
    """지연(분)을 사람이 읽는 문자열로. 기준 비교와 일치하도록 0 방향으로 절사한다."""
    if minutes is None:
        return "-"
    m = int(minutes)
    if m > 0:
        return f"+{m}분"
    if m < 0:
        return f"{-m}분 빠름"
    return "정시"


def hhmm(dt: datetime | None) -> str:
    return dt.astimezone(KST).strftime("%H:%M") if dt else "--:--"


class PointKind(str, Enum):
    ORIGIN = "기점"
    STOP = "정차지"
    DESTINATION = "종점"


@dataclass(frozen=True)
class Rules:
    """이상 판정 기준. config.yaml 의 rules 와 노선별 rules 로 덮어쓸 수 있다."""

    departure_delay_min: float = 5        # 기점 출발 N분 이상 지연 → 이상
    departure_caution_min: float = 3      # 기점 출발 N분 이상 지연 → 주의
    early_departure_min: float | None = 3  # N분 이상 일찍 출발 → 이상 (None 이면 미사용)
    stop_delay_min: float = 10            # 정차지/종점 N분 이상 지연 → 이상
    stop_caution_min: float = 5           # 정차지/종점 N분 이상 지연 → 주의
    no_signal_min: float = 10             # 운행 중 N분 이상 위치 미수신 → 알림
    departure_confirm_m: float = 300      # 기점 반경을 벗어나 이 거리 이상 멀어지면 출발 확정
    default_radius_m: float = 100         # 지점 도착 판정 반경(지오펜스)
    pre_departure_window_min: float = 40  # 계획 출발 N분 전부터 기점 감시 시작
    stop_lookahead: int = 3               # 미경유 판정 시 앞으로 몇 개 지점까지 볼지
    estimate_gap_min: float = 5           # 위치 간격이 이보다 크면 도착시각을 '추정'으로 표시

    def with_overrides(self, overrides: dict | None) -> "Rules":
        if not overrides:
            return self
        names = {f.name for f in fields(self)}
        unknown = set(overrides) - names
        if unknown:
            raise ValueError(f"알 수 없는 rules 항목: {', '.join(sorted(unknown))}")
        return replace(self, **overrides)


@dataclass(frozen=True)
class RoutePoint:
    name: str
    lat: float
    lon: float
    planned: time
    kind: PointKind
    radius_m: float


@dataclass(frozen=True)
class Route:
    """출근(또는 퇴근) 1회 운행 = Route 1개. 같은 차량이 2회 운행하면 Route 를 2개 둔다."""

    id: str
    name: str
    vehicle_id: str
    points: tuple[RoutePoint, ...]  # [기점, 정차지..., 종점]
    rules: Rules
    weekdays: frozenset[int] = frozenset(range(5))  # 0=월 ... 6=일
    driver: str = ""
    phone: str = ""
    scenario: str = ""  # 시뮬레이터 전용 (normal / departure_delay / stop_delay ...)

    @property
    def origin(self) -> RoutePoint:
        return self.points[0]

    @property
    def destination(self) -> RoutePoint:
        return self.points[-1]

    def runs_on(self, d: date) -> bool:
        return d.weekday() in self.weekdays

    def planned_at(self, idx: int, service_date: date) -> datetime:
        return datetime.combine(service_date, self.points[idx].planned, tzinfo=KST)

    @property
    def label(self) -> str:
        return f"{self.name}({self.vehicle_id})" if self.vehicle_id else self.name


@dataclass(frozen=True)
class VehiclePosition:
    vehicle_id: str
    ts: datetime  # tz-aware (KST)
    lat: float
    lon: float
    speed_kmh: float | None = None
    address: str = ""


class AnomalyKind(str, Enum):
    DEPARTURE_DELAY = "출발지연"
    EARLY_DEPARTURE = "조기출발"
    STOP_DELAY = "정차지연"
    STOP_SKIPPED = "미경유"
    NO_SIGNAL = "위치미수신"


class AnomalyStatus(str, Enum):
    ONGOING = "진행중"     # 아직 출발/도착하지 않음 (지연이 계속 늘어나는 중)
    CONFIRMED = "확정"     # 실제 시각이 확인되어 지연이 확정됨
    VOID = "해제"          # 사후 확인 결과 기준 미만 → 이상 아님


@dataclass
class Anomaly:
    service_date: date
    route_id: str
    route_name: str
    vehicle_id: str
    point_idx: int
    point_name: str
    kind: AnomalyKind
    planned: datetime | None
    actual: datetime | None
    delay_min: float | None
    detected_at: datetime
    status: AnomalyStatus = AnomalyStatus.ONGOING
    note: str = ""


class PointStatus(str, Enum):
    OK = "정상"
    CAUTION = "주의"
    ABNORMAL = "이상"
    UNKNOWN = "미확인"
    WAITING = "대기"


@dataclass
class PointResult:
    idx: int
    name: str
    kind: PointKind
    planned: datetime
    actual: datetime | None
    delay_min: float | None
    status: PointStatus
    note: str = ""


class Level(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    OK = "ok"


LEVEL_ICON = {Level.CRITICAL: "🚨", Level.WARNING: "⚠️", Level.INFO: "ℹ️", Level.OK: "✅"}


@dataclass
class Alert:
    key: str  # 중복 발송 방지 키 (같은 날 같은 키는 1번만 발송)
    level: Level
    title: str
    lines: list[str]
    created_at: datetime
    route_id: str = ""
    lat: float | None = None
    lon: float | None = None
    place: str = ""

    def text(self) -> str:
        body = "\n".join(f"· {line}" for line in self.lines)
        return f"{LEVEL_ICON[self.level]} {self.title}\n{body}" if body else f"{LEVEL_ICON[self.level]} {self.title}"


@dataclass
class TripSnapshot:
    """보고서/저장용 노선 1개의 하루 결과."""

    route: Route
    service_date: date
    points: list[PointResult]
    anomalies: list[Anomaly]
    status: PointStatus
    completed: bool
    departed: bool
    summary: str = ""
    extra: dict = field(default_factory=dict)
