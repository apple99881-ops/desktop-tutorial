"""UVIS '노선관리'에 등록된 노선·정류장·배차로 routes.yaml 초안 만들기 (import-routes 명령).

UVIS 설정 → 노선관리(routeList.do) 화면이 쓰는 데이터 (docs/05_UVIS_실제구조_분석.md):
  routeList.json             노선 목록   rmIdx, rmName, rmFlag(1=출근 2=퇴근), rmStartName, rmStopName
  routeStationList.json      정류장 순서 rdOrder, siIdx, siName, siLatitude, siLongitude, rdArrTime(앞 정류장부터 분)
  routeStationTimeList.json  배차       rtStart/rtEnd("HHMM"), rtDayName(평일…), cmNumber(배정 차량)
노선 줄을 누를 때 화면이 부르는 selectedRow(rmIdx) 를 그대로 실행해 응답을 모은다 (조회만 하고 저장하지 않음).
"""
from __future__ import annotations

import html
import logging
import re
import time as _time
from datetime import datetime, timedelta
from urllib.parse import urljoin

from ..geo import haversine_m

log = logging.getLogger(__name__)

FLAG_NAMES = {"1": "출근", "2": "퇴근"}
WEEKDAY_KO = "월화수목금토일"
LIST_EP, STATION_EP, TIME_EP, INFO_EP = ("routeList.json", "routeStationList.json",
                                         "routeStationTimeList.json", "routeInfo.ajax")


def fetch_uvis_routes(source, flag: str | None = None, name_filter: str | None = None,
                      routes_url: str | None = None, per_route_sec: float = 10) -> list[dict]:
    """로그인된 UvisWebSource 로 노선관리 화면을 열어 노선별 원본 데이터를 모은다.
    flag: '1'(출근) / '2'(퇴근) / None(전체)."""
    if source.page is None:
        source._start()
    page = source.page
    captured: dict[tuple[str, int | None], object] = {}

    def on_response(resp):
        name = resp.url.split("?")[0].rsplit("/", 1)[-1]
        if name not in (LIST_EP, STATION_EP, TIME_EP, INFO_EP):
            return
        try:
            body = resp.json()
        except Exception:
            return
        m = re.search(r'"rmIdx"\s*:\s*"?(\d+)', resp.request.post_data or "")
        captured[(name, int(m.group(1)) if m else None)] = body

    page.on("response", on_response)
    url = routes_url or urljoin(source.monitor_url, "routeList.do")
    page.goto(url, wait_until="domcontentloaded")
    source._login_if_needed()
    if url.split("?")[0] not in page.url:
        page.goto(url, wait_until="domcontentloaded")
    _wait(page, lambda: (LIST_EP, None) in captured, per_route_sec * 2, "노선 목록(routeList.json)")
    rows = captured[(LIST_EP, None)]
    rows = rows if isinstance(rows, list) else (rows or {}).get("list") or []
    rows = [r for r in rows if (not flag or str(r.get("rmFlag")) == flag)
            and (not name_filter or name_filter in str(r.get("rmName", "")))]
    log.info("UVIS 노선 %d개 상세 조회", len(rows))
    out = []
    for i, r in enumerate(rows, 1):
        idx = int(r["rmIdx"])
        page.evaluate(f"selectedRow({idx})")
        try:
            _wait(page, lambda: (STATION_EP, idx) in captured and (TIME_EP, idx) in captured, per_route_sec,
                  f"노선 {r.get('rmName')} 상세")
        except TimeoutError as exc:
            log.warning("%s – 건너뜀", exc)
            continue
        info = captured.get((INFO_EP, idx)) or {}
        out.append({"route": r, "info": info.get("routeInfo") if isinstance(info, dict) else None,
                    "stations": captured[(STATION_EP, idx)] or [], "times": captured[(TIME_EP, idx)] or []})
        if i % 10 == 0:
            log.info("  %d/%d", i, len(rows))
        page.wait_for_timeout(300)  # UVIS 부하를 줄이려 천천히
    page.remove_listener("response", on_response)
    return out


def _wait(page, cond, sec: float, what: str) -> None:
    deadline = _time.monotonic() + sec
    while not cond():
        if _time.monotonic() > deadline:
            raise TimeoutError(f"{sec:.0f}초 안에 {what} 응답이 오지 않았습니다.")
        page.wait_for_timeout(200)


# ---------------------------------------------------------------------------- routes.yaml 변환
def _hhmm(text: str) -> datetime | None:
    try:
        return datetime.strptime(str(text).strip().zfill(4), "%H%M")
    except ValueError:
        return None


def _weekdays(day_name: str) -> tuple[list[str], str]:
    name = (day_name or "").strip()
    if name in ("평일", "주중"):
        return list(WEEKDAY_KO[:5]), ""
    if name in ("매일", "전체", "모든요일"):
        return list(WEEKDAY_KO), ""
    if name == "주말":
        return list(WEEKDAY_KO[5:]), ""
    days = [d for d in WEEKDAY_KO if d in name]
    if days:
        return days, ""
    return list(WEEKDAY_KO[:5]), f"운행요일 '{name}' 을(를) 해석하지 못해 평일로 둠"


def _text(v) -> str:
    return html.unescape(str(v or "")).strip()  # UVIS 는 괄호 등을 &#40; 처럼 보낸다


def _radii(points: list[dict], max_m: float, min_m: float) -> list[float | None]:
    """이웃 정류장과 반경이 겹치지 않게 (거리의 절반) 줄인 반경. 기본값(max_m) 그대로면 None."""
    out = []
    for i, p in enumerate(points):
        near = [haversine_m(p["lat"], p["lon"], q["lat"], q["lon"])
                for q in (points[i - 1] if i else None, points[i + 1] if i + 1 < len(points) else None) if q]
        r = max(min_m, min([max_m] + [d / 2 for d in near]) // 10 * 10)
        out.append(None if r >= max_m else r)
    return out


def build_routes(raw_routes: list[dict], merge_m: float = 30, radius_m: float = 150,
                 min_radius_m: float = 60) -> tuple[list[dict], list[str]]:
    """fetch_uvis_routes 결과 → routes.yaml 의 routes 목록과 경고 목록.

    배차 1건 = 노선 1개(운행 1회). 정류장 계획시각 = 배차 출발시각(rtStart) + rdArrTime 누적(분).
    같은 정류장이 연달아 등록된 경우(UVIS 에 흔함)는 하나로 합친다.
    UVIS 정류장은 200~300m 간격인 곳이 많아, 이웃과 겹치지 않게 반경을 줄인다 (radius_m 상한, min_radius_m 하한).
    """
    routes, warnings = [], []
    for item in raw_routes:
        r, stations, times = item["route"], item.get("stations") or [], item.get("times") or []
        rname = _text(r.get("rmName"))
        flag = FLAG_NAMES.get(str(r.get("rmFlag")), str(r.get("rmFlag")))
        if not times:
            warnings.append(f"[{rname}] 배차(운행시각)가 없어 건너뜀")
            continue
        merged: list[tuple[dict, int]] = []  # (정류장, 기점부터 누적 분)
        cum = 0
        for st in sorted(stations, key=lambda s: int(s.get("rdOrder") or 0)):
            cum += int(str(st.get("rdArrTime") or "0").strip() or 0) if merged else 0
            try:
                lat, lon = float(st["siLatitude"]), float(st["siLongitude"])
            except (KeyError, TypeError, ValueError):
                warnings.append(f"[{rname}] 정류장 '{st.get('siName')}' 좌표 없음 – 제외")
                continue
            if merged:
                prev = merged[-1][0]
                if st.get("siIdx") == prev.get("siIdx") or haversine_m(lat, lon, prev["lat"], prev["lon"]) < merge_m:
                    continue  # 같은 정류장 중복 – 앞의 것만 남기고 소요분은 누적 유지
            merged.append(({"siIdx": st.get("siIdx"), "name": _text(st.get("siName")),
                            "lat": round(lat, 6), "lon": round(lon, 6)}, cum))
        if len(merged) < 2:
            warnings.append(f"[{rname}] 정류장이 2개 미만이라 건너뜀")
            continue
        radii = _radii([p for p, _ in merged], radius_m, min_radius_m)
        for k, t in enumerate(times, 1):
            start, end = _hhmm(t.get("rtStart")), _hhmm(t.get("rtEnd"))
            if start is None:
                warnings.append(f"[{rname}] 배차 출발시각 '{t.get('rtStart')}' 해석 불가 – 건너뜀")
                continue
            notes = []
            last = start + timedelta(minutes=merged[-1][1])
            if last.date() != start.date():
                warnings.append(f"[{rname}] 운행이 자정을 넘어가 건너뜀")
                continue
            if end is not None and abs((last - end).total_seconds()) > 5 * 60:
                notes.append(f"정류장 소요분 합계로 계산한 종점 {last:%H:%M} ≠ 배차 종료 {end:%H:%M} – 실제 운행으로 확인 필요")
            weekdays, wnote = _weekdays(_text(t.get("rtDayName")))
            if wnote:
                notes.append(wnote)
            vehicle = str(t.get("cmNumber") or "").strip()
            pts = [{"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                    "time": (start + timedelta(minutes=m)).strftime("%H:%M"), **({"radius_m": rr} if rr else {})}
                   for (p, m), rr in zip(merged, radii)]
            route = {
                "id": f"U{r['rmIdx']}" + (f"-{k}" if len(times) > 1 else ""),
                "name": f"[{flag}] {rname}" + (f" {start:%H:%M}" if len(times) > 1 else ""),
                "vehicle_id": vehicle or "미배정",
                "weekdays": weekdays,
                "origin": pts[0], "stops": pts[1:-1], "destination": pts[-1],
                "uvis": {"rmIdx": r["rmIdx"], "rtKey": t.get("rtKey"), "flag": flag,
                         "rtStart": t.get("rtStart"), "rtEnd": t.get("rtEnd"), "day": _text(t.get("rtDayName"))},
            }
            if not vehicle:
                route["enabled"] = False
                notes.append("UVIS 배차에 차량이 없어 꺼 둠 (vehicle_id 입력 후 enabled 삭제)")
            if notes:
                route["note"] = " / ".join(notes)
                warnings.extend(f"[{rname}] {n}" for n in notes)
            routes.append(route)
    return routes, warnings
