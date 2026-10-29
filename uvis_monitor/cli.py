"""명령줄 실행: python -m uvis_monitor <명령> [옵션]

  check            설정·노선 파일 점검 (좌표/시각 오류 찾기)
  demo             시뮬레이터로 아침 운행 전체를 재현 → 알림 + 엑셀 (UVIS 연결 없이 시험)
  run              오늘 아침 실시간 감시 1회 (작업 스케줄러로 매일 실행)
  daemon           매일 자동 반복 감시 (PC 를 켜 두는 경우)
  replay           UVIS 엑셀 다운로드 파일로 지난 운행 재분석 → 엑셀 보고서
  report           특정 날짜 일일 보고서 다시 만들기
  monthly          월간 집계 보고서
  test-notify      알림 채널 시험 발송
  kakao-auth       카카오톡 '나에게 보내기' 인증 (최초 1회)
  telegram-chatid  텔레그램 chat_id 확인
  inspect-excel    UVIS 엑셀 파일 구조/열 자동인식 확인
  web-probe        UVIS 웹 화면이 주고받는 데이터 구조 기록 (본인 PC 에서 직접 로그인)
  import-routes    UVIS 노선관리의 노선·정류장·배차로 routes.yaml 초안 만들기
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError, load_config, load_routes
from .geo import haversine_m
from .models import KST, Alert, Level, now_kst

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("uvis_monitor")


def _setup_io(log_dir: Path | None, verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / f"uvis_monitor_{now_kst():%Y%m}.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load(args, demo: bool = False) -> AppConfig:
    path = Path(args.config)
    if not path.is_file() and demo:
        path = ROOT / "config" / "config.example.yaml"
    cfg = load_config(path)
    if demo:
        cfg.database = cfg.base_dir / "data" / "demo.db"
        cfg.report_dir = cfg.report_dir / "demo"
        if not cfg.routes_file.is_file():
            cfg.routes_file = ROOT / "config" / "routes.example.yaml"
    return cfg


def _date(s: str | None) -> date:
    return date.fromisoformat(s) if s else now_kst().date()


# ---------------------------------------------------------------------------- 명령
def cmd_check(args) -> int:
    cfg = _load(args)
    routes = load_routes(cfg.routes_file, cfg.rules)
    w = cfg.window
    print(f"설정 파일 OK: {args.config}")
    print(f"  감시 시간 {w.start:%H:%M}~{w.end:%H:%M}, 주기 {w.poll_interval_sec}초, "
          f"요일 {''.join('월화수목금토일'[i] for i in sorted(w.weekdays))}, 휴일 {len(w.holidays)}일")
    print(f"  데이터 소스: {cfg.source.get('type')} / 알림 채널: "
          f"{', '.join(c.get('type', '?') for c in (cfg.notify.get('channels') or []))}")
    print(f"노선 {len(routes)}개 ({cfg.routes_file})")
    problems = 0
    for r in routes:
        print(f"\n[{r.id}] {r.name} · 차량 {r.vehicle_id}")
        for i, p in enumerate(r.points):
            line = f"  {i:>2}. {p.kind.value:<3} {p.planned:%H:%M}  {p.name}  ({p.lat:.6f}, {p.lon:.6f}) 반경 {p.radius_m:.0f}m"
            if i:
                q = r.points[i - 1]
                dist = haversine_m(q.lat, q.lon, p.lat, p.lon)
                mins = (datetime.combine(date.today(), p.planned) - datetime.combine(date.today(), q.planned)).total_seconds() / 60
                speed = dist / 1000 / (mins / 60) if mins > 0 else float("inf")
                line += f"  ← {dist / 1000:.1f}km/{mins:.0f}분"
                if dist < q.radius_m + p.radius_m:
                    line += "  ⚠ 앞 지점과 반경이 겹침"
                    problems += 1
                elif speed > 70:
                    line += f"  ⚠ 평균 {speed:.0f}km/h – 좌표나 시각 확인"
                    problems += 1
            print(line)
        if not (33 <= r.origin.lat <= 39 and 124 <= r.origin.lon <= 132):
            print("  ⚠ 기점 좌표가 한국 범위를 벗어남 (위도/경도 순서 확인)")
            problems += 1
    print(f"\n점검 완료: 확인 필요 {problems}건")
    return 0


def cmd_demo(args) -> int:
    from .monitor import run_replay
    from .notify import create_hub
    from .sources.simulator import SimulatorSource
    from .storage import Store

    cfg = _load(args, demo=True)
    routes = load_routes(cfg.routes_file, cfg.rules)
    d = _date(args.date)
    while not cfg.is_service_day(d):
        d = date.fromordinal(d.toordinal() - 1)
    source = SimulatorSource(routes, d, seed=args.seed)
    print(f"▶ 시뮬레이션 {d} ({len(routes)}개 노선, 속도 {'즉시' if not args.speed else f'{args.speed:g}배'})")
    for r in routes:
        print(f"   {r.id} {r.name}: 시나리오 = {source.scenarios[r.id]}")
    hub = create_hub(cfg, console_only=not args.notify)
    store = Store(cfg.database)
    result = run_replay(cfg, routes, source, hub, store, d, notify=True, source_name="simulator(데모)",
                        speed=args.speed)
    print(f"\n▶ 엑셀 보고서: {result.report_path}")
    return 0


def cmd_run(args) -> int:
    from .monitor import Monitor, RealClock
    from .notify import create_hub
    from .sources import create_source
    from .storage import Store

    cfg = _load(args)
    routes = load_routes(cfg.routes_file, cfg.rules)
    now = now_kst()
    d = now.date()
    end = datetime.combine(d, cfg.window.end, tzinfo=KST)
    if not cfg.is_service_day(d) and not args.force:
        print(f"{d} 은(는) 감시 요일/휴일이 아니어서 종료합니다 (--force 로 강제 실행).")
        return 0
    if now > end and not args.force:
        print(f"오늘 감시 종료 시각({cfg.window.end:%H:%M})이 지났습니다. "
              f"지난 데이터는 replay, 보고서는 report 명령을 쓰세요.")
        return 0
    clock = RealClock()
    hub = create_hub(cfg, clock=clock)
    store = Store(cfg.database)
    source = create_source(cfg, routes, d)
    try:
        mon = Monitor(cfg, routes, source, hub, store, clock)
        if args.force:
            end = max(end, now + timedelta(minutes=args.minutes))
        result = mon.run_day(d, end=end)
        print(f"\n▶ 엑셀 보고서: {result.report_path}")
    finally:
        source.close()
        store.close()
    return 0


def cmd_daemon(args) -> int:
    from .monitor import Monitor, RealClock, next_run_time
    from .notify import create_hub
    from .sources import create_source
    from .storage import Store

    cfg = _load(args)
    clock = RealClock()
    print("매일 자동 감시를 시작합니다. 끝내려면 Ctrl+C")
    while True:
        try:
            routes = load_routes(cfg.routes_file, cfg.rules)  # 매일 노선 파일을 다시 읽음 (대차·시간 변경 반영)
            start = next_run_time(cfg, clock.now())
            log.info("다음 감시: %s", start.strftime("%Y-%m-%d(%a) %H:%M"))
            clock.sleep_until(start)
            d = start.date()
            hub = create_hub(cfg, clock=clock)
            store = Store(cfg.database)
            source = create_source(cfg, routes, d)
            try:
                Monitor(cfg, routes, source, hub, store, clock).run_day(d)
            finally:
                source.close()
                store.close()
            clock.sleep_until(datetime.combine(d, cfg.window.end, tzinfo=KST))
            clock.sleep(60)
        except KeyboardInterrupt:
            print("종료합니다.")
            return 0
        except Exception:
            log.exception("감시 중 오류 – 5분 후 다시 시도")
            clock.sleep(300)


def cmd_replay(args) -> int:
    from .monitor import run_replay
    from .notify import create_hub
    from .sources.excel_export import ExcelSource
    from .storage import Store

    cfg = _load(args)
    routes = load_routes(cfg.routes_file, cfg.rules)
    excel_opts = (cfg.source or {}).get("excel") or {}
    files = [Path(f) for f in args.file]
    probe = ExcelSource(files=files, columns=excel_opts.get("columns"),
                        default_vehicle=args.vehicle or excel_opts.get("default_vehicle"),
                        service_date=_date(args.date) if args.date else None)
    rng = probe.time_range()
    if not rng:
        print("파일에서 위치 데이터를 찾지 못했습니다. inspect-excel 로 확인하세요.")
        return 1
    d = _date(args.date) if args.date else rng[0].date()
    source = ExcelSource(files=files, columns=excel_opts.get("columns"),
                         default_vehicle=args.vehicle or excel_opts.get("default_vehicle"), service_date=d)
    print(f"▶ 재분석 {d}: 위치 {len(probe.all_positions())}건 ({rng[0]:%H:%M}~{rng[1]:%H:%M})")
    hub = create_hub(cfg, console_only=not args.notify)
    store = Store(cfg.database)
    result = run_replay(cfg, routes, source, hub, store, d, notify=True,
                        source_name=f"excel({', '.join(f.name for f in files)})")
    print(f"\n▶ 엑셀 보고서: {result.report_path}")
    return 0


def cmd_report(args) -> int:
    from .report import build_daily_report
    from .storage import Store

    cfg = _load(args)
    store = Store(cfg.database)
    d = _date(args.date)
    if not store.route_days(d):
        print(f"{d} 데이터가 DB({cfg.database})에 없습니다.")
        return 1
    path = build_daily_report(store, d, cfg.rules, cfg.report_dir, (cfg.source or {}).get("type", ""))
    print(f"일일 보고서: {path}")
    return 0


def cmd_monthly(args) -> int:
    from .report import build_monthly_report
    from .storage import Store

    cfg = _load(args)
    month = args.month or f"{now_kst():%Y-%m}"
    store = Store(cfg.database)
    path = build_monthly_report(store, month, cfg.rules, cfg.report_dir)
    print(f"월간 보고서: {path}")
    if args.send:
        from .notify import create_hub
        create_hub(cfg).send_file(path, f"출근 운행 월간 집계 {month}")
    return 0


def cmd_test_notify(args) -> int:
    from .notify import create_hub

    cfg = _load(args)
    hub = create_hub(cfg)
    if not hub.notifiers:
        print("사용 가능한 알림 채널이 없습니다. config.yaml 의 notify.channels 를 확인하세요.")
        return 1
    alert = Alert(key=f"test:{now_kst():%H%M%S}", level=Level.CRITICAL, title="[시험] 출발지연 알림 예시",
                  lines=["기점: 시험 정류장", "계획 06:30 → 현재 06:36 미출발 (+6분)", "이 메시지가 보이면 설정 완료!"],
                  created_at=now_kst(), lat=37.2664, lon=127.0000, place="시험 정류장")
    for name, ok in hub.send(alert):
        print(f"  {name}: {'성공' if ok else '실패 (logs 폴더의 로그 확인)'}")
    return 0


def cmd_kakao_auth(args) -> int:
    from .notify.kakao import authorize

    cfg = _load(args)
    ch = next((c for c in cfg.notify.get("channels") or [] if c.get("type") == "kakao"), None)
    if not ch or not ch.get("rest_api_key"):
        print("config.yaml 의 notify.channels 에 kakao 채널과 rest_api_key(KAKAO_REST_API_KEY)를 먼저 설정하세요.")
        return 1
    token_file = Path(ch.get("token_file", "config/kakao_token.json"))
    if not token_file.is_absolute():
        token_file = cfg.base_dir / token_file
    path = authorize(ch["rest_api_key"], ch.get("redirect_uri", "http://localhost:8765/callback"), token_file,
                     ch.get("client_secret", ""))
    print(f"카카오 토큰 저장 완료: {path}\n이제 'python -m uvis_monitor test-notify' 로 시험해 보세요.")
    return 0


def cmd_telegram_chatid(args) -> int:
    from .notify.telegram import find_chat_ids

    cfg = _load(args)
    ch = next((c for c in cfg.notify.get("channels") or [] if c.get("type") == "telegram"), {})
    token = args.token or ch.get("bot_token")
    if not token:
        print("봇 토큰이 필요합니다: --token 또는 .env 의 TELEGRAM_BOT_TOKEN")
        return 1
    chats = find_chat_ids(token)
    if not chats:
        print("받은 메시지가 없습니다. 텔레그램에서 내 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return 1
    for cid, name in chats:
        print(f"chat_id = {cid}   ({name})")
    print("\n.env 에 TELEGRAM_CHAT_ID=위 숫자 를 적으세요.")
    return 0


def cmd_inspect_excel(args) -> int:
    from .sources.excel_export import inspect

    print(inspect(args.file))
    return 0


def cmd_web_probe(args) -> int:
    from .sources.uvis_web import web_probe

    out = Path(args.out) if args.out else ROOT / "data" / "probe" / now_kst().strftime("%Y%m%d_%H%M")
    summary = web_probe(args.url, out, minutes=args.minutes)
    print(f"\n기록 완료: {summary}\n※ 이 폴더에는 회사 운행 데이터가 들어 있으니 외부에 공유하지 마세요.")
    return 0


def cmd_import_routes(args) -> int:
    import json

    import yaml

    from .sources.uvis_routes import build_routes, fetch_uvis_routes
    from .sources.uvis_web import UvisWebSource

    cfg = _load(args)
    opts = (cfg.source or {}).get("uvis_web") or {}
    if not opts.get("monitor_url"):
        print("config.yaml 의 source.uvis_web (monitor_url, username, password)을 먼저 설정하세요.")
        return 1
    flag = {"출근": "1", "퇴근": "2", "전체": None}[args.flag]
    src = UvisWebSource(opts, state_dir=cfg.database.parent)
    try:
        raw = fetch_uvis_routes(src, flag=flag, name_filter=args.name)
    finally:
        src.close()
    raw_path = cfg.database.parent / "uvis_routes_raw.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    routes, warnings = build_routes(raw)
    out = Path(args.out) if args.out else cfg.base_dir / "config" / f"routes_uvis_{args.flag}.yaml"
    header = (f"# UVIS 노선관리에서 가져온 노선 ({args.flag}, {now_kst():%Y-%m-%d %H:%M})\n"
              f"# 정류장 계획시각 = 배차 출발시각 + UVIS 정류장 소요분 누적. 'note' 가 있는 노선은 확인하세요.\n"
              f"# 사용: config.yaml 의 routes_file 을 이 파일로 바꾸거나 routes.yaml 로 복사 → check 로 점검\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump({"routes": routes}, allow_unicode=True, sort_keys=False, width=200),
                   encoding="utf-8")
    print(f"UVIS 노선 {len(raw)}개 → 운행 {len(routes)}건: {out}\n원본: {raw_path}")
    for w in warnings[:30]:
        print(f"  ⚠ {w}")
    if len(warnings) > 30:
        print(f"  … 외 {len(warnings) - 30}건")
    return 0


# ---------------------------------------------------------------------------- 진입점
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m uvis_monitor", description="에스원 UVIS 통근버스 출근 운행 감시")
    p.add_argument("--config", default=str(ROOT / "config" / "config.yaml"), help="설정 파일 (기본 config/config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="자세한 로그")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="설정·노선 점검").set_defaults(fn=cmd_check)
    s = sub.add_parser("demo", help="시뮬레이터 시연")
    s.add_argument("--date", help="YYYY-MM-DD (기본 오늘, 주말이면 직전 평일)")
    s.add_argument("--speed", type=float, default=0, help="진행 배속 (0=즉시, 60=1분→1초)")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--notify", action="store_true", help="설정된 실제 메신저로도 발송")
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("run", help="오늘 실시간 감시")
    s.add_argument("--force", action="store_true", help="요일/시간과 관계없이 지금부터 감시 (시험용)")
    s.add_argument("--minutes", type=float, default=30, help="--force 일 때 감시 시간(분)")
    s.set_defaults(fn=cmd_run)
    sub.add_parser("daemon", help="매일 자동 반복 감시").set_defaults(fn=cmd_daemon)
    s = sub.add_parser("replay", help="UVIS 엑셀로 지난 운행 재분석")
    s.add_argument("file", nargs="+", help="UVIS 에서 받은 엑셀/CSV 파일 (여러 개 가능)")
    s.add_argument("--date", help="운행일 (기본: 파일의 첫 날짜)")
    s.add_argument("--vehicle", help="파일에 차량 열이 없을 때 차량번호")
    s.add_argument("--notify", action="store_true", help="재분석 결과를 메신저로도 발송")
    s.set_defaults(fn=cmd_replay)
    s = sub.add_parser("report", help="일일 보고서 다시 만들기")
    s.add_argument("--date", help="YYYY-MM-DD (기본 오늘)")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("monthly", help="월간 집계 보고서")
    s.add_argument("--month", help="YYYY-MM (기본 이번 달)")
    s.add_argument("--send", action="store_true", help="메신저로 파일 발송 (텔레그램)")
    s.set_defaults(fn=cmd_monthly)
    sub.add_parser("test-notify", help="알림 시험 발송").set_defaults(fn=cmd_test_notify)
    sub.add_parser("kakao-auth", help="카카오톡 인증").set_defaults(fn=cmd_kakao_auth)
    s = sub.add_parser("telegram-chatid", help="텔레그램 chat_id 확인")
    s.add_argument("--token", help="봇 토큰 (기본: 설정값)")
    s.set_defaults(fn=cmd_telegram_chatid)
    s = sub.add_parser("inspect-excel", help="UVIS 엑셀 구조 확인")
    s.add_argument("file")
    s.set_defaults(fn=cmd_inspect_excel)
    s = sub.add_parser("web-probe", help="UVIS 웹 데이터 구조 기록")
    s.add_argument("--url", default="https://www.u-vis.com/uvis/login.do", help="UVIS 로그인 주소")
    s.add_argument("--minutes", type=float, default=10)
    s.add_argument("--out", help="저장 폴더 (기본 data/probe/날짜)")
    s.set_defaults(fn=cmd_web_probe)
    s = sub.add_parser("import-routes", help="UVIS 노선관리 → routes.yaml 초안")
    s.add_argument("--flag", choices=["출근", "퇴근", "전체"], default="출근", help="가져올 노선 구분 (기본 출근)")
    s.add_argument("--name", help="노선명에 이 글자가 든 노선만")
    s.add_argument("--out", help="저장 파일 (기본 config/routes_uvis_구분.yaml)")
    s.set_defaults(fn=cmd_import_routes)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = None
    try:
        if args.cmd not in ("inspect-excel",):
            try:
                log_dir = load_config(args.config).log_dir if Path(args.config).is_file() else ROOT / "logs"
            except ConfigError:
                log_dir = ROOT / "logs"
        _setup_io(log_dir, args.verbose)
        return args.fn(args)
    except ConfigError as exc:
        print(f"[설정 오류] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
