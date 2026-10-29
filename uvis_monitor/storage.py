"""SQLite 저장소 — 위치 원본, 지점별 결과, 이상내역, 알림 이력.

data/uvis_monitor.db 하나에 모두 쌓이므로, 엑셀 외의 분석(월간 추세, 노선별 상습 지연 등)을
하고 싶을 때 이 DB 를 그대로 쓰면 된다 (DB Browser for SQLite 로도 열람 가능).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import KST, Alert, TripSnapshot, VehiclePosition, norm_vid

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    vehicle_id TEXT, ts TEXT, lat REAL, lon REAL, speed REAL, address TEXT,
    PRIMARY KEY (vehicle_id, ts));
CREATE TABLE IF NOT EXISTS route_days (
    service_date TEXT, route_id TEXT, route_name TEXT, vehicle_id TEXT, driver TEXT,
    status TEXT, departed INTEGER, completed INTEGER, summary TEXT, updated_at TEXT,
    PRIMARY KEY (service_date, route_id));
CREATE TABLE IF NOT EXISTS point_results (
    service_date TEXT, route_id TEXT, idx INTEGER, point_name TEXT, kind TEXT,
    planned TEXT, actual TEXT, delay_min REAL, status TEXT, note TEXT,
    PRIMARY KEY (service_date, route_id, idx));
CREATE TABLE IF NOT EXISTS anomalies (
    service_date TEXT, route_id TEXT, route_name TEXT, vehicle_id TEXT, point_idx INTEGER,
    point_name TEXT, kind TEXT, planned TEXT, actual TEXT, delay_min REAL, detected_at TEXT,
    status TEXT, note TEXT,
    PRIMARY KEY (service_date, route_id, point_idx, kind));
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service_date TEXT, sent_at TEXT, alert_key TEXT,
    route_id TEXT, level TEXT, title TEXT, body TEXT, channels TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(alert_key);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(KST).isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def clear_day(self, d: date) -> None:
        with self.db:
            for table in ("route_days", "point_results", "anomalies", "alerts"):
                self.db.execute(f"DELETE FROM {table} WHERE service_date = ?", (d.isoformat(),))

    # ------------------------------------------------------------ 저장
    def save_positions(self, positions: list[VehiclePosition]) -> None:
        if not positions:
            return
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO positions VALUES (?,?,?,?,?,?)",
                [(p.vehicle_id, _iso(p.ts), p.lat, p.lon, p.speed_kmh, p.address) for p in positions])

    def save_snapshot(self, s: TripSnapshot, now: datetime) -> None:
        d, r = s.service_date.isoformat(), s.route
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO route_days VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (d, r.id, r.name, r.vehicle_id, r.driver, s.status.value, int(s.departed),
                             int(s.completed), s.summary, _iso(now)))
            self.db.executemany(
                "INSERT OR REPLACE INTO point_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(d, r.id, p.idx, p.name, p.kind.value, _iso(p.planned), _iso(p.actual), p.delay_min,
                  p.status.value, p.note) for p in s.points])
            self.db.executemany(
                "INSERT OR REPLACE INTO anomalies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(d, a.route_id, a.route_name, a.vehicle_id, a.point_idx, a.point_name, a.kind.value,
                  _iso(a.planned), _iso(a.actual), a.delay_min, _iso(a.detected_at), a.status.value, a.note)
                 for a in s.anomalies])

    def alert_sent(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE alert_key = ? LIMIT 1", (key,)).fetchone() is not None

    def save_alert(self, service_date: date, alert: Alert | None, text: str, results: list[tuple[str, bool]],
                   sent_at: datetime, level: str = "", title: str = "") -> None:
        channels = ", ".join(f"{name}{'' if ok else '(실패)'}" for name, ok in results)
        ok = int(bool(results) and all(r for _, r in results))
        with self.db:
            self.db.execute(
                "INSERT INTO alerts (service_date, sent_at, alert_key, route_id, level, title, body, channels, ok) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (service_date.isoformat(), _iso(sent_at), alert.key if alert else None,
                 alert.route_id if alert else "", alert.level.value if alert else level,
                 alert.title if alert else title, text, channels, ok))

    # ------------------------------------------------------------ 조회
    def _rows(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def route_days(self, d: date) -> list[dict]:
        return self._rows("SELECT * FROM route_days WHERE service_date = ? ORDER BY route_id", (d.isoformat(),))

    def point_results(self, d: date) -> list[dict]:
        return self._rows("SELECT * FROM point_results WHERE service_date = ? ORDER BY route_id, idx",
                          (d.isoformat(),))

    def anomalies(self, d: date | None = None, month: str | None = None, include_void: bool = False) -> list[dict]:
        where, args = [], []
        if d:
            where.append("service_date = ?")
            args.append(d.isoformat())
        if month:
            where.append("substr(service_date, 1, 7) = ?")
            args.append(month)
        if not include_void:
            where.append("status != '해제'")
        sql = "SELECT * FROM anomalies" + (" WHERE " + " AND ".join(where) if where else "")
        return self._rows(sql + " ORDER BY service_date, detected_at, route_id", args)

    def alerts(self, d: date) -> list[dict]:
        return self._rows("SELECT * FROM alerts WHERE service_date = ? ORDER BY id", (d.isoformat(),))

    def month_route_days(self, month: str) -> list[dict]:
        return self._rows("SELECT * FROM route_days WHERE substr(service_date, 1, 7) = ? "
                          "ORDER BY service_date, route_id", (month,))

    def positions(self, vehicle_id: str, start: datetime, end: datetime) -> list[VehiclePosition]:
        """차량 위치 이력 (차량 이름의 공백/하이픈/대소문자 차이는 무시)."""
        rows = self._rows("SELECT * FROM positions WHERE UPPER(REPLACE(REPLACE(vehicle_id, ' ', ''), '-', '')) = ? "
                          "AND ts BETWEEN ? AND ? ORDER BY ts", (norm_vid(vehicle_id), _iso(start), _iso(end)))
        return [VehiclePosition(r["vehicle_id"], parse_iso(r["ts"]), r["lat"], r["lon"], r["speed"],
                                r["address"] or "") for r in rows]
