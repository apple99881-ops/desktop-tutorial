"""설정 파일(config.yaml, routes.yaml) 로딩.

- 문자열 안의 ${환경변수} / ${환경변수:-기본값} 은 실행 시 치환된다
  (비밀번호·토큰은 .env 파일이나 OS 환경변수에 두고 설정 파일에는 쓰지 않는다).
- 시간은 "06:30" 처럼 따옴표로 감싸 쓰는 것을 권장한다. 따옴표가 없으면 YAML 이
  06:30 을 60진수 정수(390)로 읽는데, 그 경우도 자동으로 06:30 으로 변환한다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import yaml

from .models import PointKind, Route, RoutePoint, Rules

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

WEEKDAY_NAMES = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
}
WEEKDAY_KO = "월화수목금토일"


class ConfigError(ValueError):
    pass


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 형식의 .env 파일을 읽어 환경변수에 넣는다 (이미 있는 값은 유지)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def expand_env(obj):
    if isinstance(obj, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), obj)
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    return obj


def parse_time(value, where: str = "") -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML 1.1 의 60진수: 06:30 → 390
        return time(value // 60, value % 60)
    if isinstance(value, str):
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).time()
            except ValueError:
                pass
    raise ConfigError(f"{where} 시간 형식 오류: {value!r} (예: \"06:30\")")


def parse_weekdays(values, where: str = "") -> frozenset[int]:
    if values is None:
        return frozenset(range(5))
    out = set()
    for v in values:
        key = str(v).strip().upper()
        if key not in WEEKDAY_NAMES:
            raise ConfigError(f"{where} 요일 형식 오류: {v!r} (예: 월, 화 또는 MON, TUE)")
        out.add(WEEKDAY_NAMES[key])
    return frozenset(out)


@dataclass
class MonitorWindow:
    start: time = time(6, 0)
    end: time = time(9, 30)
    poll_interval_sec: int = 60
    weekdays: frozenset[int] = frozenset(range(5))
    holidays: set[date] = field(default_factory=set)
    status_digest_min: int = 0          # N분마다 실시간 현황판 발송 (0 = 끔)
    notify_normal_departure: bool = False


@dataclass
class AppConfig:
    base_dir: Path
    routes_file: Path
    database: Path
    report_dir: Path
    log_dir: Path
    window: MonitorWindow
    rules: Rules
    source: dict
    notify: dict
    raw: dict

    def is_service_day(self, d: date) -> bool:
        return d.weekday() in self.window.weekdays and d not in self.window.holidays


def _resolve(base: Path, p: str | None, default: str) -> Path:
    path = Path(p or default)
    return path if path.is_absolute() else (base / path)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"설정 파일이 없습니다: {path}\n  → config/config.example.yaml 을 복사해 만드세요.")
    base = path.parent.parent if path.parent.name == "config" else path.parent
    load_dotenv(base / ".env")
    raw = expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    m = raw.get("monitor", {}) or {}
    holidays = set()
    for h in m.get("holidays", []) or []:
        holidays.add(h if isinstance(h, date) else date.fromisoformat(str(h)))
    window = MonitorWindow(
        start=parse_time(m.get("start", "06:00"), "monitor.start"),
        end=parse_time(m.get("end", "09:30"), "monitor.end"),
        poll_interval_sec=int(m.get("poll_interval_sec", 60)),
        weekdays=parse_weekdays(m.get("weekdays"), "monitor.weekdays"),
        holidays=holidays,
        status_digest_min=int(m.get("status_digest_min", 0) or 0),
        notify_normal_departure=bool(m.get("notify_normal_departure", False)),
    )
    if window.end <= window.start:
        raise ConfigError("monitor.end 는 monitor.start 보다 늦어야 합니다.")
    rules = Rules().with_overrides(raw.get("rules") or {})
    return AppConfig(
        base_dir=base,
        routes_file=_resolve(base, raw.get("routes_file"), "config/routes.yaml"),
        database=_resolve(base, raw.get("database"), "data/uvis_monitor.db"),
        report_dir=_resolve(base, raw.get("report_dir"), "reports"),
        log_dir=_resolve(base, raw.get("log_dir"), "logs"),
        window=window,
        rules=rules,
        source=raw.get("source") or {"type": "simulator"},
        notify=raw.get("notify") or {"channels": [{"type": "console"}]},
        raw=raw,
    )


def _point(d: dict, kind: PointKind, rules: Rules, where: str) -> RoutePoint:
    try:
        lat, lon = float(d["lat"]), float(d["lon"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"{where} 에 lat/lon(위도/경도)이 없습니다.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ConfigError(f"{where} 좌표 범위 오류: {lat}, {lon}")
    if "time" not in d:
        raise ConfigError(f"{where} 에 time(계획시각)이 없습니다.")
    return RoutePoint(
        name=str(d.get("name") or kind.value), lat=lat, lon=lon,
        planned=parse_time(d["time"], where), kind=kind,
        radius_m=float(d.get("radius_m") or rules.default_radius_m),
    )


def load_routes(path: str | Path, base_rules: Rules) -> list[Route]:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"노선 파일이 없습니다: {path}\n  → config/routes.example.yaml 을 복사해 만드세요.")
    raw = expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    routes: list[Route] = []
    seen: set[str] = set()
    for i, r in enumerate(raw.get("routes") or [], 1):
        rid = str(r.get("id") or f"R{i:02d}")
        where = f"[노선 {rid}]"
        if rid in seen:
            raise ConfigError(f"{where} id 가 중복됩니다.")
        seen.add(rid)
        if r.get("enabled", True) is False:
            continue
        rules = base_rules.with_overrides(r.get("rules") or {})
        if "origin" not in r or "destination" not in r:
            raise ConfigError(f"{where} origin(기점)과 destination(종점)이 필요합니다.")
        points = [_point(r["origin"], PointKind.ORIGIN, rules, f"{where} 기점")]
        for j, s in enumerate(r.get("stops") or [], 1):
            points.append(_point(s, PointKind.STOP, rules, f"{where} 정차지 {j}"))
        points.append(_point(r["destination"], PointKind.DESTINATION, rules, f"{where} 종점"))
        for a, b in zip(points, points[1:]):
            if b.planned < a.planned:
                raise ConfigError(f"{where} 계획시각이 거꾸로입니다: {a.name} {a.planned} → {b.name} {b.planned}")
        routes.append(Route(
            id=rid, name=str(r.get("name") or rid), vehicle_id=str(r.get("vehicle_id") or ""),
            points=tuple(points), rules=rules,
            weekdays=parse_weekdays(r.get("weekdays"), where) if r.get("weekdays") else frozenset(range(7)),
            driver=str(r.get("driver") or ""), phone=str(r.get("phone") or ""),
            scenario=str(r.get("scenario") or ""),
        ))
        if not routes[-1].vehicle_id:
            raise ConfigError(f"{where} vehicle_id(UVIS 에 표시되는 차량번호/차량명)가 필요합니다.")
    if not routes:
        raise ConfigError(f"{path} 에 활성화된 노선이 없습니다.")
    return routes
