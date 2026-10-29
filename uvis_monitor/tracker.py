"""노선 1회 운행 추적 엔진 (상태 기계).

    [기점 감시] --기점 반경 이탈 + confirm 거리 이상 멀어짐--> [운행중: 다음 지점 대기]
        |                                                      |
        +-- 계획출발 + 5분 경과, 미출발 → 🚨 출발지연            +-- 계획도착 + 10분 경과, 미도착 → 🚨 정차지연
                                                               +-- 뒤 지점에 먼저 도착 → ⚠️ 미경유
                                                               +-- N분 이상 위치 없음 → ⚠️ 위치미수신
                                                               +-- 종점 도착 → [운행완료]

입력은 ingest(위치), 시간 경과 판정은 evaluate(현재시각).
결과는 anomalies(이상내역), point_results(지점별 계획/실제), pop_alerts(알림).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .geo import haversine_m, segment_circle
from .models import (
    Alert, Anomaly, AnomalyKind, AnomalyStatus, Level, PointResult,
    PointStatus, Route, TripSnapshot, VehiclePosition, fmt_delay, hhmm, minutes_between, norm_vid,
)


class TripTracker:
    def __init__(self, route: Route, service_date: date):
        self.route = route
        self.rules = route.rules
        self.service_date = service_date
        n = len(route.points)
        self.planned = [route.planned_at(i, service_date) for i in range(n)]
        self.actual: list[datetime | None] = [None] * n
        self.skipped = [False] * n
        self.unknown = [False] * n   # 감시 시작 전/위치 공백 중 지나가 경유 여부를 알 수 없는 지점
        self.notes = [""] * n
        self.next_idx = 0            # 다음에 도달해야 할 지점 (0 = 아직 출발 전)
        self.inside_origin = False
        self.origin_seen = False
        self.exit_time: datetime | None = None  # 기점 반경 이탈 시각 (출발 확정 전)
        self.last_pos: VehiclePosition | None = None
        self.first_pos: VehiclePosition | None = None  # 감시창 안 첫 위치 (늦게 시작한 경우 판단용)
        self.signal_lost_since: datetime | None = None
        self.completed = False
        self.anomalies: dict[tuple[int, AnomalyKind], Anomaly] = {}
        self._alerts: list[Alert] = []
        self._sent: set[str] = set()
        self._now: datetime | None = None
        self._vid = norm_vid(route.vehicle_id)
        self.announce_normal = False  # True 면 정상 출발도 알림 (monitor.notify_normal_departure)

    # ------------------------------------------------------------------ 속성
    @property
    def departed(self) -> bool:
        return self.next_idx > 0

    @property
    def watch_from(self) -> datetime:
        return self.planned[0] - timedelta(minutes=self.rules.pre_departure_window_min)

    @property
    def late_start(self) -> bool:
        """계획 출발시각이 지나서야 첫 위치가 들어옴 (프로그램을 늦게 켠 경우 등)."""
        return self.first_pos is not None and self.first_pos.ts > self.planned[0] and not self.origin_seen

    def set_now(self, now: datetime) -> None:
        self._now = now

    # ------------------------------------------------------------------ 입력
    def ingest(self, pos: VehiclePosition) -> None:
        if norm_vid(pos.vehicle_id) != self._vid:
            return
        prev = self.last_pos
        if prev is not None and pos.ts <= prev.ts:
            return  # 중복/역순 데이터 무시
        if self.signal_lost_since is not None:
            self._signal_recovered(pos)
        self.last_pos = pos
        if self.completed or pos.ts < self.watch_from:
            return
        if prev is not None and prev.ts < self.watch_from:
            prev = None  # 감시 시작 전 위치로는 구간 판정하지 않는다
        if self.first_pos is None:
            self.first_pos = pos
        t_floor = 0.0
        if self.next_idx == 0:
            t_floor = self._track_origin(prev, pos)
        if self.next_idx >= 1 and not self.completed:
            self._track_points(prev, pos, t_floor)

    # ------------------------------------------------------------------ 기점
    def _track_origin(self, prev: VehiclePosition | None, pos: VehiclePosition) -> float:
        o = self.route.origin
        d = haversine_m(pos.lat, pos.lon, o.lat, o.lon)
        if d <= o.radius_m:
            self.inside_origin = True
            self.origin_seen = True
            self.exit_time = None  # 재진입 → 이전 이탈은 출발이 아님
            return 0.0

        t_floor = 0.0
        if prev is not None and self.exit_time is None:
            span = segment_circle((prev.lat, prev.lon), (pos.lat, pos.lon), (o.lat, o.lon), o.radius_m)
            if span is not None:  # 직전엔 기점 안이었거나, 두 샘플 사이에 기점을 통과
                t_floor = span[1]
                self.exit_time = self._interp(prev, pos, t_floor)
                self.origin_seen = True
        self.inside_origin = False

        n = len(self.route.points)
        upto = n if prev is None else min(n, 2 + self.rules.stop_lookahead)  # 첫 위치는 전 지점 확인
        reached_next = False
        for k in range(1, upto):
            hit = self._hit(prev, pos, k, t_floor)
            # 기점으로 오는 길(공차 회송)에 정차지를 지나치는 경우를 출발로 오인하지 않도록
            # 계획 도착시각보다 너무 이른 통과는 무시한다
            if hit and hit[0] >= self.planned[k] - timedelta(minutes=self.rules.pre_departure_window_min / 2):
                reached_next = True
                break
        if self.exit_time is not None:
            early_exit = (self.rules.early_departure_min is not None and
                          minutes_between(self.exit_time, self.planned[0]) <= -self.rules.early_departure_min)
            far_enough = d >= self.rules.departure_confirm_m
            if early_exit:
                # 조기출발 오탐(차고지 오가며 기점을 스쳐 지나감) 방지: 다음 정차지 쪽으로 실제 진행했을 때만 확정
                p1 = self.route.points[1]
                base = haversine_m(o.lat, o.lon, p1.lat, p1.lon)
                far_enough = haversine_m(pos.lat, pos.lon, p1.lat, p1.lon) <= base - self.rules.departure_confirm_m
            if far_enough or reached_next:
                self._confirm_departure(self.exit_time)
            return t_floor
        if reached_next:
            # 기점에서 위치가 한 번도 잡히지 않은 채 정차지에 도달 → 출발시각 미확인
            self._confirm_departure(None)
        elif self.late_start and haversine_m(pos.lat, pos.lon, self.first_pos.lat,
                                             self.first_pos.lon) >= self.rules.departure_confirm_m:
            # 계획 출발 이후에 감시를 시작했고 차량이 도로 위를 달리고 있음 → 이미 출발한 것으로 본다
            self._confirm_departure(None)
            self.notes[0] = "감시 시작이 늦어 출발시각 미확인 – 운행 중 확인"
        return 0.0

    def _confirm_departure(self, t: datetime | None) -> None:
        self.next_idx = 1
        self.actual[0] = t
        r, o, route = self.rules, self.route.origin, self.route
        pending = self.anomalies.get((0, AnomalyKind.DEPARTURE_DELAY))
        if t is None:
            self.skipped[0] = True
            self.notes[0] = "기점 출발 위치 미수신 – 정차지 도착으로 출발 판정"
            if pending:
                pending.status = AnomalyStatus.CONFIRMED
                pending.note = "기점 출발시각 미확인(정차지 도착으로 출발 판정), 최소 지연값"
            return
        delay = minutes_between(t, self.planned[0])
        base_lines = [f"기점: {o.name}", f"계획 {hhmm(self.planned[0])} → 출발 {hhmm(t)} ({fmt_delay(delay)})",
                      f"차량: {route.vehicle_id}"]
        if delay >= r.departure_delay_min:
            self._upsert(0, AnomalyKind.DEPARTURE_DELAY, t, delay, AnomalyStatus.CONFIRMED,
                         note=f"미출발 알림({hhmm(pending.detected_at)}) 후 출발 확인" if pending else "지연 출발")
            title = "[지연출발 확인]" if pending else "[출발지연]"
            self._alert("departed_late", Level.WARNING, f"{title} {route.name}", base_lines, o)
        elif r.early_departure_min is not None and delay <= -r.early_departure_min:
            self._upsert(0, AnomalyKind.EARLY_DEPARTURE, t, delay, AnomalyStatus.CONFIRMED,
                         note="계획보다 일찍 출발 – 미탑승자 발생 가능")
            self._alert("departed_early", Level.WARNING, f"[조기출발] {route.name}",
                        base_lines + ["미탑승자 발생 가능 – 기사 확인 필요"], o)
        elif pending:
            pending.status = AnomalyStatus.VOID
            pending.actual, pending.delay_min = t, delay
            pending.note = "출발 확인 결과 기준 미만"
            self._alert("departed_ok", Level.OK, f"[출발확인] {route.name}", base_lines, o)
        elif self.announce_normal:
            self._alert("departed", Level.INFO, f"[정상출발] {route.name}", base_lines, o)

    # ------------------------------------------------------------------ 정차지/종점
    def _track_points(self, prev: VehiclePosition | None, pos: VehiclePosition, t_floor: float) -> None:
        n = len(self.route.points)
        first_sample = prev is None
        gap = prev is not None and minutes_between(pos.ts, prev.ts) > self.rules.estimate_gap_min
        while not self.completed:
            upto = n if first_sample else min(n, self.next_idx + 1 + self.rules.stop_lookahead)
            found = None
            for k in range(self.next_idx, upto):
                hit = self._hit(prev, pos, k, t_floor)
                if hit:
                    found = (k, *hit)
                    break
            if not found:
                return
            k, t_arr, frac = found
            # 미경유는 '앞 지점을 실제로 관찰한 뒤 연속된 위치'로만 판정한다.
            # 감시를 늦게 시작했거나 위치 공백 중에 지나간 지점은 '미확인'으로 남긴다 (오탐 방지).
            anchored = any(self.actual[i] is not None for i in range(self.next_idx))
            for j in range(self.next_idx, k):
                if first_sample or gap or not anchored:
                    self._mark_unknown(j, "감시 시작 전 통과 – 경유 여부 미확인" if first_sample or not anchored
                                       else "위치 미수신 구간 – 경유 여부 미확인")
                else:
                    self._skip(j, k, t_arr)
            if first_sample:
                self._mark_unknown(k, "감시 시작 시 이미 도착 상태 – 도착시각 미확인")
                self.next_idx = k + 1
                self.completed = k == n - 1
            else:
                self._arrive(k, t_arr, gap)
            t_floor = frac

    def _mark_unknown(self, j: int, note: str) -> None:
        self.unknown[j] = True
        self.notes[j] = note
        pending = self.anomalies.get((j, AnomalyKind.STOP_DELAY))
        if pending and pending.status == AnomalyStatus.ONGOING:
            pending.status = AnomalyStatus.CONFIRMED
            pending.note = f"{note} (최소 지연값)"

    def _arrive(self, k: int, t: datetime, estimated: bool) -> None:
        self.actual[k] = t
        self.next_idx = k + 1
        if estimated:
            self.notes[k] = "위치 미수신 구간 – 도착시각 추정"
        p, route = self.route.points[k], self.route
        delay = minutes_between(t, self.planned[k])
        pending = self.anomalies.get((k, AnomalyKind.STOP_DELAY))
        lines = [f"{p.kind.value}: {p.name}", f"계획 {hhmm(self.planned[k])} → 도착 {hhmm(t)} ({fmt_delay(delay)})",
                 f"차량: {route.vehicle_id}"]
        if delay >= self.rules.stop_delay_min:
            note = f"미도착 알림({hhmm(pending.detected_at)}) 후 도착 확인" if pending else "지연 도착"
            if self.notes[k]:
                note += f" · {self.notes[k]}"
            self._upsert(k, AnomalyKind.STOP_DELAY, t, delay, AnomalyStatus.CONFIRMED, note=note)
            title = "[지연도착 확인]" if pending else f"[{p.kind.value}지연]"
            self._alert(f"arrived_late:{k}", Level.WARNING, f"{title} {route.name}", lines, p)
        elif pending:
            pending.status = AnomalyStatus.VOID
            pending.actual, pending.delay_min = t, delay
            pending.note = "도착 확인 결과 기준 미만"
            self._alert(f"arrived_ok:{k}", Level.OK, f"[도착확인] {route.name}", lines, p)
        if k == len(self.route.points) - 1:
            self.completed = True

    def _skip(self, j: int, reached: int, t_ref: datetime) -> None:
        self.skipped[j] = True
        p, q, route = self.route.points[j], self.route.points[reached], self.route
        pending = self.anomalies.get((j, AnomalyKind.STOP_DELAY))
        if pending:
            pending.status = AnomalyStatus.VOID
            pending.note = "미경유로 전환"
        note = f"{q.name} 도착({hhmm(t_ref)})까지 반경 {int(p.radius_m)}m 내 진입 기록 없음"
        self._upsert(j, AnomalyKind.STOP_SKIPPED, None, None, AnomalyStatus.CONFIRMED, note=note)
        self._alert(f"skipped:{j}", Level.WARNING, f"[미경유] {route.name}",
                    [f"{p.kind.value}: {p.name} (계획 {hhmm(self.planned[j])})", note,
                     "노선 우회 또는 정차지 좌표/반경 확인 필요"], p)

    # ------------------------------------------------------------------ 시간 경과 판정
    def evaluate(self, now: datetime) -> None:
        self._now = now
        if self.completed:
            return
        r, route = self.rules, self.route
        if self.next_idx == 0:
            over = minutes_between(now, self.planned[0])
            leaving = self.exit_time is not None and minutes_between(now, self.exit_time) < 3
            # 늦게 시작한 경우 기점 밖 차량은 3분간 움직임을 지켜본 뒤 판단 (달리는 중이면 출발로 처리됨)
            observing = (self.late_start and not self.inside_origin
                         and minutes_between(now, self.first_pos.ts) < 3)
            if over >= r.departure_delay_min and not leaving and not observing:
                state = self._origin_state(now)
                self._upsert(0, AnomalyKind.DEPARTURE_DELAY, None, over, AnomalyStatus.ONGOING, note=state)
                o = route.origin
                self._alert("dep_pending", Level.CRITICAL, f"[출발지연] {route.name}",
                            [f"기점: {o.name}",
                             f"계획 {hhmm(self.planned[0])} → 현재 {hhmm(now)} 미출발 ({fmt_delay(over)})",
                             f"상태: {state}", f"차량: {route.vehicle_id}{self._driver_text()}"],
                            o, use_last_pos=True)
            return

        k = self.next_idx
        p = route.points[k]
        over = minutes_between(now, self.planned[k])
        if over >= r.stop_delay_min:
            where = self._where_text(p)
            self._upsert(k, AnomalyKind.STOP_DELAY, None, over, AnomalyStatus.ONGOING, note=where)
            self._alert(f"stop_pending:{k}", Level.CRITICAL, f"[{p.kind.value}지연] {route.name}",
                        [f"{p.kind.value}: {p.name}",
                         f"계획 {hhmm(self.planned[k])} → 현재 {hhmm(now)} 미도착 ({fmt_delay(over)})",
                         f"현재: {where}", f"차량: {route.vehicle_id}{self._driver_text()}"],
                        p, use_last_pos=True)

        if self.last_pos is None:
            return
        gap = minutes_between(now, self.last_pos.ts)
        if self.signal_lost_since is None and gap >= r.no_signal_min:
            self.signal_lost_since = self.last_pos.ts
            self._upsert(k, AnomalyKind.NO_SIGNAL, None, gap, AnomalyStatus.ONGOING,
                         note=f"마지막 수신 {hhmm(self.last_pos.ts)}")
            self._alert(f"nosignal:{hhmm(self.last_pos.ts)}", Level.WARNING, f"[위치미수신] {route.name}",
                        [f"마지막 수신 {hhmm(self.last_pos.ts)} ({int(gap)}분 경과)",
                         f"다음 지점: {p.name} (계획 {hhmm(self.planned[k])})",
                         "단말기 전원/통신 상태 확인 필요"], p, use_last_pos=True)
        elif self.signal_lost_since is not None:
            a = self.anomalies.get((k, AnomalyKind.NO_SIGNAL))
            if a and a.status == AnomalyStatus.ONGOING:
                a.delay_min = gap

    def _signal_recovered(self, pos: VehiclePosition) -> None:
        since = self.signal_lost_since
        self.signal_lost_since = None
        lost = minutes_between(pos.ts, since)
        for a in self.anomalies.values():
            if a.kind == AnomalyKind.NO_SIGNAL and a.status == AnomalyStatus.ONGOING:
                a.status = AnomalyStatus.CONFIRMED
                a.actual, a.delay_min = pos.ts, lost
                a.note = f"{hhmm(since)}~{hhmm(pos.ts)} 위치 미수신 ({int(lost)}분)"
        self._alert(f"signal_back:{hhmm(since)}", Level.OK, f"[위치수신 재개] {self.route.name}",
                    [f"{hhmm(since)}~{hhmm(pos.ts)} 미수신 ({int(lost)}분)"])

    def finalize(self, now: datetime) -> None:
        """감시 종료 시 호출: 진행중 이상내역에 종료 메모를 남긴다."""
        self.evaluate(now)
        for a in self.anomalies.values():
            if a.status == AnomalyStatus.ONGOING and "감시 종료" not in a.note:
                a.note = (a.note + " / " if a.note else "") + f"감시 종료({hhmm(now)})까지 미해소"

    # ------------------------------------------------------------------ 결과
    def pop_alerts(self) -> list[Alert]:
        out, self._alerts = self._alerts, []
        return out

    def active_anomalies(self) -> list[Anomaly]:
        return [a for a in self.anomalies.values() if a.status != AnomalyStatus.VOID]

    def point_results(self, now: datetime) -> list[PointResult]:
        r = self.rules
        out = []
        for i, p in enumerate(self.route.points):
            planned, actual = self.planned[i], self.actual[i]
            delay = minutes_between(actual, planned) if actual else None
            note = self.notes[i]
            if i == 0:
                thr, caution = r.departure_delay_min, r.departure_caution_min
            else:
                thr, caution = r.stop_delay_min, r.stop_caution_min
            if (self.skipped[i] and i == 0) or self.unknown[i]:
                status = PointStatus.UNKNOWN
            elif self.skipped[i]:
                status, note = PointStatus.ABNORMAL, note or "미경유"
            elif actual is not None:
                if i == 0 and r.early_departure_min is not None and delay <= -r.early_departure_min:
                    status, note = PointStatus.ABNORMAL, note or "조기출발"
                elif delay >= thr:
                    status = PointStatus.ABNORMAL
                elif delay >= caution:
                    status = PointStatus.CAUTION
                else:
                    status = PointStatus.OK
            else:
                over = minutes_between(now, planned)
                if over >= thr and (i == 0 or self.departed):
                    status = PointStatus.ABNORMAL
                    note = note or ("미출발" if i == 0 else "미도착")
                    delay = over
                elif over >= thr:
                    status, note = PointStatus.UNKNOWN, note or "미출발로 미도착"
                else:
                    status = PointStatus.WAITING
            out.append(PointResult(i, p.name, p.kind, planned, actual, delay, status, note))
        return out

    def route_status(self, now: datetime) -> PointStatus:
        active = self.active_anomalies()
        if any(a.kind != AnomalyKind.NO_SIGNAL for a in active):
            return PointStatus.ABNORMAL
        points = self.point_results(now)
        if active or any(p.status == PointStatus.CAUTION for p in points):
            return PointStatus.CAUTION  # 위치 미수신만 있으면 운행 자체는 정상일 수 있어 '주의'
        if not self.departed and now < self.planned[0]:
            return PointStatus.WAITING
        return PointStatus.OK

    def summary_text(self) -> str:
        parts = []
        for a in sorted(self.active_anomalies(), key=lambda a: (a.point_idx, a.kind.value)):
            if a.kind == AnomalyKind.STOP_SKIPPED:
                parts.append(f"{a.point_name} 미경유")
            elif a.kind == AnomalyKind.NO_SIGNAL:
                parts.append(f"위치미수신 {int(a.delay_min or 0)}분")
            elif a.actual is None:
                what = "미출발" if a.point_idx == 0 else "미도착"
                parts.append(f"{a.point_name} {what}({fmt_delay(a.delay_min)})")
            else:
                parts.append(f"{a.kind.value} {a.point_name} {fmt_delay(a.delay_min)}")
        return "; ".join(parts)

    def status_line(self, now: datetime) -> str:
        """메신저 실시간 현황판 한 줄."""
        st = self.route_status(now)
        icon = {PointStatus.ABNORMAL: "🚨", PointStatus.CAUTION: "⚠️", PointStatus.WAITING: "⏳"}.get(st, "✅")
        name = self.route.name
        if self.completed:
            d = minutes_between(self.actual[-1], self.planned[-1]) if self.actual[-1] else None
            body = f"운행완료 (종점 {hhmm(self.actual[-1])}, {fmt_delay(d)})"
        elif not self.departed:
            over = minutes_between(now, self.planned[0])
            body = (f"출발대기 ({hhmm(self.planned[0])} 출발)" if over < 0
                    else f"미출발 ({fmt_delay(over)}) · {self._origin_state(now)}")
        else:
            k = self.next_idx
            over = minutes_between(now, self.planned[k])
            late = f", 현재 {fmt_delay(over)}" if over > 0 else ""
            body = f"운행중 → {self.route.points[k].name} {hhmm(self.planned[k])}{late}"
        extra = self.summary_text()
        return f"{icon} {name}: {body}" + (f"\n    └ {extra}" if extra else "")

    def snapshot(self, now: datetime) -> TripSnapshot:
        return TripSnapshot(route=self.route, service_date=self.service_date,
                            points=self.point_results(now), anomalies=list(self.anomalies.values()),
                            status=self.route_status(now), completed=self.completed,
                            departed=self.departed, summary=self.summary_text())

    # ------------------------------------------------------------------ 내부 도우미
    def _hit(self, prev: VehiclePosition | None, pos: VehiclePosition, k: int,
             t_floor: float = 0.0) -> tuple[datetime, float] | None:
        p = self.route.points[k]
        if prev is None:
            if haversine_m(pos.lat, pos.lon, p.lat, p.lon) <= p.radius_m:
                return pos.ts, 1.0
            return None
        span = segment_circle((prev.lat, prev.lon), (pos.lat, pos.lon), (p.lat, p.lon), p.radius_m)
        if span is None or span[1] < t_floor:
            return None
        t = max(span[0], t_floor)
        return self._interp(prev, pos, t), t

    @staticmethod
    def _interp(a: VehiclePosition, b: VehiclePosition, t: float) -> datetime:
        return a.ts + (b.ts - a.ts) * t

    def _origin_state(self, now: datetime) -> str:
        if self.last_pos is None or self.last_pos.ts < self.watch_from:
            return "위치 미수신(단말 OFF/통신장애 가능)"
        o = self.route.origin
        d = haversine_m(self.last_pos.lat, self.last_pos.lon, o.lat, o.lon)
        age = minutes_between(now, self.last_pos.ts)
        stale = f", {int(age)}분 전 위치" if age >= 3 else ""
        if self.inside_origin:
            return f"기점 대기 중{stale}"
        if self.exit_time is not None:
            return f"기점 이탈({hhmm(self.exit_time)}) 후 출발 확정 대기, 기점에서 {d / 1000:.1f}km{stale}"
        return f"기점 미도착, 기점까지 {d / 1000:.1f}km{stale}"

    def _where_text(self, p) -> str:
        if self.last_pos is None:
            return "위치 미수신"
        d = haversine_m(self.last_pos.lat, self.last_pos.lon, p.lat, p.lon)
        addr = f"{self.last_pos.address}, " if self.last_pos.address else ""
        return f"{addr}{p.name}까지 {d / 1000:.1f}km ({hhmm(self.last_pos.ts)} 위치 기준)"

    def _driver_text(self) -> str:
        if self.route.driver:
            return f" / 기사 {self.route.driver}" + (f" {self.route.phone}" if self.route.phone else "")
        return ""

    def _upsert(self, idx: int, kind: AnomalyKind, actual: datetime | None, delay: float | None,
                status: AnomalyStatus, note: str = "") -> Anomaly:
        key = (idx, kind)
        a = self.anomalies.get(key)
        p = self.route.points[idx]
        if a is None:
            a = Anomaly(service_date=self.service_date, route_id=self.route.id, route_name=self.route.name,
                        vehicle_id=self.route.vehicle_id, point_idx=idx,
                        point_name=p.name if kind != AnomalyKind.NO_SIGNAL else f"운행중(→{p.name})",
                        kind=kind, planned=None if kind == AnomalyKind.NO_SIGNAL else self.planned[idx],
                        actual=actual, delay_min=delay, detected_at=self._now or actual or self.planned[idx],
                        status=status, note=note)
            self.anomalies[key] = a
        else:
            a.actual, a.delay_min, a.status = actual, delay, status
            if note:
                a.note = note
        return a

    def _alert(self, key: str, level: Level, title: str, lines: list[str], place=None,
               use_last_pos: bool = False) -> None:
        if key in self._sent:
            return
        self._sent.add(key)
        lat = lon = None
        name = ""
        if use_last_pos and self.last_pos is not None:
            lat, lon, name = self.last_pos.lat, self.last_pos.lon, f"{self.route.vehicle_id} 현재위치"
        elif place is not None:
            lat, lon, name = place.lat, place.lon, place.name
        created = self._now or (self.last_pos.ts if self.last_pos else self.planned[0])
        self._alerts.append(Alert(key=f"{self.service_date}:{self.route.id}:{key}", level=level, title=title,
                                  lines=lines, created_at=created, route_id=self.route.id,
                                  lat=lat, lon=lon, place=name))
