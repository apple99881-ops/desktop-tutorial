"""하루 감시 실행기: 위치 수집 → 엔진 판정 → 알림 → 저장 → 종료 후 엑셀/요약 발송.

    매 poll_interval_sec 마다
      1) source.fetch(now)           UVIS 등에서 차량 위치 수집
      2) tracker.ingest / evaluate   노선별 출발·도착·지연 판정
      3) hub.send(alert)             카카오톡/텔레그램/웹훅 발송 (같은 알림은 하루 1번)
      4) store.save_*                DB 저장 (프로그램이 재시작돼도 이어서 감시)
    감시 종료 시 엑셀 일일보고서 생성 + 요약 메시지(+파일) 발송
"""
from __future__ import annotations

import logging
import time as _time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import WEEKDAY_KO, AppConfig
from .models import KST, Alert, AnomalyKind, PointStatus, Route, hhmm, norm_vid, now_kst
from .notify import NotifierHub
from .report import build_daily_report
from .sources.base import PositionSource
from .storage import Store
from .tracker import TripTracker

log = logging.getLogger(__name__)


class RealClock:
    def now(self) -> datetime:
        return now_kst()

    def sleep(self, seconds: float) -> None:
        _time.sleep(max(0.0, seconds))

    def sleep_until(self, dt: datetime) -> None:
        while (left := (dt - self.now()).total_seconds()) > 0:
            _time.sleep(min(left, 30))


class SimClock:
    """가상 시계: speed=0 이면 즉시, 60 이면 1분을 1초로 진행."""

    def __init__(self, start: datetime, speed: float = 0):
        self._now, self.speed = start, speed

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if self.speed > 0:
            _time.sleep(seconds / self.speed)
        self._now += timedelta(seconds=seconds)

    def sleep_until(self, dt: datetime) -> None:
        if dt > self._now:
            self.sleep((dt - self._now).total_seconds())


@dataclass
class DayResult:
    service_date: date
    report_path: Path | None
    trackers: list[TripTracker]
    summary_text: str


class Monitor:
    def __init__(self, cfg: AppConfig, routes: list[Route], source: PositionSource, hub: NotifierHub,
                 store: Store, clock, notify: bool = True, source_name: str = "", restore: bool = True):
        self.cfg, self.routes, self.source, self.hub, self.store = cfg, routes, source, hub, store
        self.clock, self.notify, self.restore = clock, notify, restore
        self.source_name = source_name or (cfg.source or {}).get("type", "")
        self.opts = cfg.notify or {}
        self._fail_count = 0
        self._fail_alerted = False

    # ------------------------------------------------------------------ 하루 실행
    def run_day(self, service_date: date, start: datetime | None = None, end: datetime | None = None) -> DayResult:
        w = self.cfg.window
        start = start or datetime.combine(service_date, w.start, tzinfo=KST)
        end = end or datetime.combine(service_date, w.end, tzinfo=KST)
        routes = [r for r in self.routes if r.runs_on(service_date)]
        trackers = [TripTracker(r, service_date) for r in routes]
        for t in trackers:
            t.announce_normal = w.notify_normal_departure
        by_vid: dict[str, list[TripTracker]] = defaultdict(list)
        for t in trackers:
            by_vid[norm_vid(t.route.vehicle_id)].append(t)
        if not trackers:
            log.info("%s 운행 노선 없음", service_date)
            return DayResult(service_date, None, [], "")

        if self.clock.now() < start:
            log.info("감시 시작 대기: %s", start.strftime("%Y-%m-%d %H:%M"))
            self.clock.sleep_until(start)
        if self.restore:
            self._restore(service_date, trackers, by_vid, self.clock.now())
        if self.notify and self.opts.get("start_message", True):
            first = min(t.planned[0] for t in trackers)
            self._send_text(service_date, f"🟢 출근 운행 감시 시작 {service_date:%m/%d}({WEEKDAY_KO[service_date.weekday()]})\n"
                                          f"· 노선 {len(trackers)}개 · 첫 출발 {hhmm(first)} · 감시 ~{hhmm(end)}",
                            title="감시 시작", level="info")
        digest_every = w.status_digest_min
        next_digest = self.clock.now() + timedelta(minutes=digest_every) if digest_every else None

        while True:
            now = self.clock.now()
            if now > end:
                break
            self._tick(service_date, now, trackers, by_vid)
            if next_digest and now >= next_digest:
                self._send_text(service_date, self.status_board(trackers, now), title="실시간 현황", level="digest")
                next_digest = now + timedelta(minutes=digest_every)
            self._handle_commands(service_date, trackers, now)
            if all(t.completed for t in trackers):
                log.info("모든 노선 운행 완료 → 감시 종료")
                break
            self.clock.sleep(w.poll_interval_sec)

        return self.finish_day(service_date, trackers, min(self.clock.now(), end))

    def finish_day(self, service_date: date, trackers: list[TripTracker], now: datetime) -> DayResult:
        for t in trackers:
            t.finalize(now)
            for a in t.pop_alerts():
                self._dispatch(service_date, a, now)
            self.store.save_snapshot(t.snapshot(now), now)
        path = build_daily_report(self.store, service_date, trackers[0].rules if trackers else self.cfg.rules,
                                  self.cfg.report_dir, self.source_name, now)
        summary = self.daily_summary(service_date, trackers, now, path)
        if self.notify:
            self._send_text(service_date, summary, title="일일 요약", level="summary")
            if self.opts.get("send_report_file", True):
                self.hub.send_file(path, f"출근 운행 일일점검 {service_date:%Y-%m-%d}")
        log.info("일일 보고서: %s", path)
        return DayResult(service_date, path, trackers, summary)

    # ------------------------------------------------------------------ 한 주기
    def _tick(self, d: date, now: datetime, trackers, by_vid) -> None:
        try:
            positions = self.source.fetch(now)
            if self._fail_alerted:
                self._send_text(d, "✅ [시스템] UVIS 데이터 수집 정상화", title="수집 정상화", level="ok")
            self._fail_count, self._fail_alerted = 0, False
        except Exception as exc:
            self._fail_count += 1
            log.warning("위치 수집 실패(%d회 연속): %s", self._fail_count, exc)
            if self._fail_count >= 3 and not self._fail_alerted:
                self._fail_alerted = True
                self._send_text(d, f"🚨 [시스템] UVIS 데이터 수집 오류 {self._fail_count}회 연속\n· {str(exc)[:150]}\n"
                                   f"· 로그인/네트워크/설정 확인 필요 (감시는 계속 재시도)", title="수집 오류", level="critical")
            positions = []
        self.store.save_positions(positions)
        for p in sorted(positions, key=lambda p: p.ts):
            for t in by_vid.get(norm_vid(p.vehicle_id), ()):
                t.set_now(now)
                t.ingest(p)
        for t in trackers:
            t.evaluate(now)
            for a in t.pop_alerts():
                self._dispatch(d, a, now)
            self.store.save_snapshot(t.snapshot(now), now)

    def _restore(self, d: date, trackers, by_vid, now: datetime) -> None:
        """재시작 시: 오늘 이미 저장된 위치를 다시 읽어 상태를 복원 (이미 보낸 알림은 재발송 안 함)."""
        restored = 0
        for vid, ts in by_vid.items():
            begin = min(t.watch_from for t in ts) - timedelta(minutes=30)
            positions = self.store.positions(ts[0].route.vehicle_id, begin, now)
            for p in positions:
                for t in ts:
                    t.set_now(p.ts)
                    t.ingest(p)
                    t.evaluate(p.ts)
            restored += len(positions)
        for t in trackers:
            t.pop_alerts()
        if restored:
            log.info("저장된 위치 %d건으로 감시 상태 복원", restored)

    def _dispatch(self, d: date, alert: Alert, now: datetime) -> None:
        if self.store.alert_sent(alert.key):
            return
        if self.notify:
            results = self.hub.send(alert)
        else:
            results = [("미발송(재분석)", True)]
        self.store.save_alert(d, alert, alert.text(), results, now)

    def _send_text(self, d: date, text: str, title: str, level: str) -> None:
        results = self.hub.send_text(text, summary=True) if self.notify else [("미발송(재분석)", True)]
        self.store.save_alert(d, None, text, results, self.clock.now(), level=level, title=title)

    def _handle_commands(self, d: date, trackers, now: datetime) -> None:
        if not self.notify:
            return
        for notifier, cmd in self.hub.poll_commands():
            c = cmd.split()[0].split("@")[0].lower()
            if c in ("/현황", "/status", "/s"):
                notifier.send_text(self.status_board(trackers, now))
            elif c in ("/보고서", "/report", "/r"):
                path = build_daily_report(self.store, d, self.cfg.rules, self.cfg.report_dir, self.source_name, now)
                notifier.send_file(path, f"출근 운행 점검 (중간, {hhmm(now)} 기준)")
            elif c in ("/이상", "/issues"):
                lines = [t.status_line(now) for t in trackers if t.active_anomalies()]
                notifier.send_text("\n".join(lines) if lines else "✅ 현재 이상 없음")
            else:
                notifier.send_text("명령어: /현황 (전체 현황) · /이상 (이상 노선만) · /보고서 (현재까지 엑셀)")

    # ------------------------------------------------------------------ 메시지
    def status_board(self, trackers: list[TripTracker], now: datetime) -> str:
        counts = defaultdict(int)
        for t in trackers:
            counts[t.route_status(now)] += 1
        head = (f"🚌 출근 운행 실시간 현황 {now:%m/%d %H:%M}\n"
                f"정상 {counts[PointStatus.OK]} · 주의 {counts[PointStatus.CAUTION]} · "
                f"이상 {counts[PointStatus.ABNORMAL]} · 대기 {counts[PointStatus.WAITING]}")
        order = {PointStatus.ABNORMAL: 0, PointStatus.CAUTION: 1, PointStatus.OK: 2, PointStatus.WAITING: 3}
        lines = [t.status_line(now) for t in sorted(trackers, key=lambda t: (order.get(t.route_status(now), 9), t.route.id))]
        return head + "\n" + "\n".join(lines)

    def daily_summary(self, d: date, trackers: list[TripTracker], now: datetime, report_path: Path | None) -> str:
        counts = defaultdict(int)
        kinds = defaultdict(int)
        for t in trackers:
            counts[t.route_status(now)] += 1
            for a in t.active_anomalies():
                kinds[a.kind] += 1
        lines = [f"📋 출근 운행 일일점검 {d:%m/%d}({WEEKDAY_KO[d.weekday()]})",
                 f"운행 {len(trackers)}개 노선 | 정상 {counts[PointStatus.OK]} · 주의 {counts[PointStatus.CAUTION]} · "
                 f"이상 {counts[PointStatus.ABNORMAL]}",
                 " · ".join(f"{k.value} {kinds[k]}" for k in AnomalyKind)]
        bad = [t for t in trackers if t.active_anomalies()]
        if bad:
            lines.append("─" * 12)
            lines += [f"{'🚨' if t.route_status(now) == PointStatus.ABNORMAL else '⚠️'} {t.route.name}: "
                      f"{t.summary_text()}" for t in bad]
        else:
            lines.append("✅ 이상 없음 — 전 노선 정상 운행")
        if report_path:
            lines.append(f"📎 보고서: {report_path.name}")
        return "\n".join(lines)


def run_replay(cfg: AppConfig, routes: list[Route], source: PositionSource, hub: NotifierHub, store: Store,
               service_date: date, notify: bool = False, source_name: str = "replay",
               speed: float = 0) -> DayResult:
    """지난 데이터(엑셀 등)나 시뮬레이터를 가상 시계로 처음부터 끝까지 돌린다."""
    store.clear_day(service_date)
    start = datetime.combine(service_date, cfg.window.start, tzinfo=KST)
    clock = SimClock(start, speed)
    for n in hub.notifiers:
        if hasattr(n, "clock"):
            n.clock = clock
    mon = Monitor(cfg, routes, source, hub, store, clock, notify=notify, source_name=source_name, restore=False)
    return mon.run_day(service_date)


def next_run_time(cfg: AppConfig, after: datetime) -> datetime:
    """다음 감시 시작 시각 (휴일/주말 건너뜀)."""
    d = after.date()
    for _ in range(15):
        start = datetime.combine(d, cfg.window.start, tzinfo=KST)
        end = datetime.combine(d, cfg.window.end, tzinfo=KST)
        if cfg.is_service_day(d) and after < end:
            return max(start, after)
        d += timedelta(days=1)
    raise RuntimeError("15일 안에 감시할 날이 없습니다 (monitor.weekdays/holidays 확인).")
