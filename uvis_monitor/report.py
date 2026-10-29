"""엑셀 보고서 — 일일(요약/이상발생내역/노선별상세/알림이력/판정기준)과 월간 집계.

DB(storage.Store)에 쌓인 결과로 만들기 때문에, 언제든 날짜를 지정해 다시 만들 수 있다:
    python -m uvis_monitor report --date 2026-10-29
    python -m uvis_monitor monthly --month 2026-10
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import KST, Rules
from .storage import parse_iso

FONT = "맑은 고딕"
NAVY, BLUE, INK, MUTED = "0D366B", "2A78D6", "1F1F1F", "6B6B6B"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
GROUP_FILL = PatternFill("solid", fgColor="DCE8F7")
TILE_FILL = PatternFill("solid", fgColor="F3F6FA")
THIN = Side(style="thin", color="C8CDD3")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

# 판정 색상: 색만으로 구분하지 않도록 기호(●▲■)와 글자를 함께 쓴다
STATUS_STYLE = {
    "정상": ("● 정상", "0CA30C", "FFFFFF"),
    "주의": ("▲ 주의", "FAB219", "0B0B0B"),
    "이상": ("■ 이상", "D03B3B", "FFFFFF"),
    "미확인": ("? 미확인", "8C8C8C", "FFFFFF"),
    "대기": ("… 대기", "D9D9D9", "0B0B0B"),
}
KIND_TINT = {"출발지연": "FBE3E3", "조기출발": "FBE3E3", "정차지연": "FDEFD9", "미경유": "EEE6F8", "위치미수신": "EDEDED"}
LEVEL_KO = {"critical": "긴급", "warning": "경고", "ok": "해소", "info": "정보", "summary": "요약", "digest": "현황"}
WEEKDAY_KO = "월화수목금토일"


def _font(size=10, bold=False, color=INK):
    return Font(name=FONT, size=size, bold=bold, color=color)


def _t(iso: str | None):
    """ISO 문자열 → 엑셀 시각(시:분)용 naive time."""
    dt = parse_iso(iso)
    return dt.astimezone(KST).replace(tzinfo=None).time() if dt else None


def _dt(iso: str | None):
    dt = parse_iso(iso)
    return dt.astimezone(KST).replace(tzinfo=None) if dt else None


def _trunc(v: float | None):
    return int(v) if v is not None else None


def _header(ws, row: int, headers: list[str], widths: list[float] | None = None) -> None:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font, cell.fill, cell.alignment, cell.border = _font(10, True, "FFFFFF"), HEADER_FILL, CENTER, BORDER
        if widths:
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    ws.row_dimensions[row].height = 30


def _title(ws, text: str, sub: str, last_col: int) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    ws["A1"].value, ws["A1"].font, ws["A1"].alignment = text, _font(16, True, NAVY), LEFT
    ws["A2"].value, ws["A2"].font, ws["A2"].alignment = sub, _font(9, False, MUTED), LEFT
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 30


def _status_cell(cell, status: str) -> None:
    label, fill, fg = STATUS_STYLE.get(status, (status, "FFFFFF", INK))
    cell.value = label
    cell.fill = PatternFill("solid", fgColor=fill)
    cell.font = _font(10, True, fg)
    cell.alignment = CENTER


def _body(cell, value=None, fmt: str | None = None, align=CENTER, bold=False):
    if value is not None:
        cell.value = value
    cell.font = _font(10, bold)
    cell.alignment = align
    cell.border = BORDER
    if fmt:
        cell.number_format = fmt
    return cell


def _page(ws, landscape=True) -> None:
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.sheet_view.showGridLines = False


def _criteria(rules: Rules) -> str:
    early = f" · 조기출발 {rules.early_departure_min:g}분 이상 = 이상" if rules.early_departure_min is not None else ""
    return (f"판정기준: 기점 출발 {rules.departure_delay_min:g}분 이상 지연 = 이상(주의 {rules.departure_caution_min:g}분)"
            f" · 정차지/종점 {rules.stop_delay_min:g}분 이상 지연 = 이상(주의 {rules.stop_caution_min:g}분){early}"
            f" · 지연(분)은 소수점 이하 버림")


# ====================================================================== 일일 보고서
def build_daily_report(store, service_date: date, rules: Rules, out_dir: Path,
                       source_name: str = "", generated_at: datetime | None = None) -> Path:
    days = store.route_days(service_date)
    points = store.point_results(service_date)
    anomalies = store.anomalies(service_date)
    alerts = store.alerts(service_date)
    by_route: dict[str, list[dict]] = defaultdict(list)
    for p in points:
        by_route[p["route_id"]].append(p)
    gen = (generated_at or datetime.now(KST)).astimezone(KST)
    wd = WEEKDAY_KO[service_date.weekday()]

    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    _summary_sheet(ws, service_date, wd, gen, rules, days, by_route, source_name)
    _anomaly_sheet(wb.create_sheet("이상발생내역"), service_date, wd, anomalies, rules)
    _detail_sheet(wb.create_sheet("노선별상세"), service_date, wd, days, by_route)
    _alert_sheet(wb.create_sheet("알림이력"), service_date, wd, alerts)
    _criteria_sheet(wb.create_sheet("판정기준"), rules, source_name, gen)
    wb.calculation.fullCalcOnLoad = True

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{service_date:%Y-%m-%d}_출근운행점검.xlsx"
    wb.save(path)
    return path


def _summary_sheet(ws, d, wd, gen, rules, days, by_route, source_name) -> None:
    headers = ["No", "노선ID", "노선명", "차량", "계획출발", "실제출발", "출발편차(분)", "최대정차지연(분)",
               "최대지연(분)", "종점계획", "종점도착", "이상건수", "판정", "이상 요약"]
    widths = [5, 9, 22, 13, 9, 9, 10, 11, 10, 9, 9, 8, 10, 46]
    last_col = len(headers)
    _title(ws, f"통근버스 출근 운행 일일점검 — {d:%Y-%m-%d}({wd})",
           f"{_criteria(rules)}\n생성 {gen:%Y-%m-%d %H:%M} · 데이터: {source_name or '-'}", last_col)

    first = 9
    last = first + max(len(days), 1) - 1
    rng = lambda col: f"{col}{first}:{col}{last}"  # noqa: E731
    tiles = [  # (시작열, 끝열, 제목, 수식, 표시형식)
        (1, 2, "운행 노선", f"=COUNTA({rng('B')})", "0"),
        (3, 3, "● 정상", f'=COUNTIF({rng("M")},"*정상")', "0"),
        (4, 4, "▲ 주의", f'=COUNTIF({rng("M")},"*주의")', "0"),
        (5, 6, "■ 이상 노선", f'=COUNTIF({rng("M")},"*이상")', "0"),
        (7, 8, "출발지연·조기출발(건)", "=COUNTIF(이상발생내역!E:E,\"출발지연\")+COUNTIF(이상발생내역!E:E,\"조기출발\")", "0"),
        (9, 10, "정차지연(건)", "=COUNTIF(이상발생내역!E:E,\"정차지연\")", "0"),
        (11, 12, "미경유·위치미수신(건)", "=COUNTIF(이상발생내역!E:E,\"미경유\")+COUNTIF(이상발생내역!E:E,\"위치미수신\")", "0"),
        (13, 14, "정시운행률(이상 없는 노선)", '=IF(A5=0,"-",(C5+D5)/A5)', "0.0%"),
    ]
    for c1, c2, label, formula, fmt in tiles:
        for r in (4, 5):
            if c2 > c1:
                ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
            for c in range(c1, c2 + 1):
                ws.cell(row=r, column=c).fill = TILE_FILL
                ws.cell(row=r, column=c).border = BORDER
        lab = ws.cell(row=4, column=c1, value=label)
        lab.font, lab.alignment = _font(9, True, MUTED), CENTER
        val = ws.cell(row=5, column=c1, value=formula)
        val.font, val.alignment, val.number_format = _font(18, True, NAVY), CENTER, fmt
    ws.row_dimensions[4].height = 22
    ws.row_dimensions[5].height = 34
    ws["E5"].font = _font(18, True, "D03B3B")

    ws.cell(row=7, column=1, value="■ 노선별 운행 현황").font = _font(11, True, NAVY)
    _header(ws, 8, headers, widths)
    if not days:
        ws.merge_cells(start_row=first, start_column=1, end_row=first, end_column=last_col)
        _body(ws.cell(row=first, column=1), "해당 날짜의 운행 데이터가 없습니다.")
    chart_max = 0
    for i, rd in enumerate(days):
        r = first + i
        pts = by_route.get(rd["route_id"], [])
        origin = pts[0] if pts else {}
        dest = pts[-1] if pts else {}
        stops = [p for p in pts[1:] if p["delay_min"] is not None]  # 미도착(진행중 지연) 포함
        max_stop = max([0] + [_trunc(p["delay_min"]) for p in stops]) if stops else None
        dep_actual = _t(origin.get("actual")) or (origin.get("note") or "-")
        dest_actual = _t(dest.get("actual")) or ("미도착" if dest.get("status") == "이상" else "-")
        values = [i + 1, rd["route_id"], rd["route_name"], rd["vehicle_id"], _t(origin.get("planned")), dep_actual,
                  _trunc(origin.get("delay_min")), max_stop,
                  f"=MAX(0,N(G{r}),N(H{r}))", _t(dest.get("planned")), dest_actual,
                  f"=COUNTIF(이상발생내역!B:B,B{r})", None, rd["summary"] or ""]
        for c, v in enumerate(values, 1):
            cell = _body(ws.cell(row=r, column=c), v, align=LEFT if c in (3, 14) else CENTER)
            if c in (5, 6, 10, 11) and not isinstance(v, str):
                cell.number_format = "hh:mm"
            elif c in (7, 8, 9, 12):
                cell.number_format = "0;-0;0"
        _status_cell(ws.cell(row=r, column=13), rd["status"])
        ws.cell(row=r, column=13).border = BORDER
        ws.row_dimensions[r].height = 20 if len(rd["summary"] or "") < 45 else 34
        chart_max = max(chart_max, values[6] or 0, max_stop or 0)

    red, amber = _font(10, True, "C00000"), _font(10, True, "9C5700")
    ws.conditional_formatting.add(f"G{first}:G{last}", CellIsRule(operator="greaterThanOrEqual",
                                  formula=[str(rules.departure_delay_min)], font=red))
    ws.conditional_formatting.add(f"G{first}:G{last}", CellIsRule(operator="greaterThanOrEqual",
                                  formula=[str(rules.departure_caution_min)], font=amber))
    if rules.early_departure_min is not None:
        ws.conditional_formatting.add(f"G{first}:G{last}", CellIsRule(operator="lessThanOrEqual",
                                      formula=[str(-rules.early_departure_min)], font=red))
    ws.conditional_formatting.add(f"H{first}:H{last}", CellIsRule(operator="greaterThanOrEqual",
                                  formula=[str(rules.stop_delay_min)], font=red))
    ws.conditional_formatting.add(f"H{first}:H{last}", CellIsRule(operator="greaterThanOrEqual",
                                  formula=[str(rules.stop_caution_min)], font=amber))
    ws.freeze_panes = "D9"
    ws.auto_filter.ref = f"A8:{get_column_letter(last_col)}{last}"

    if days:
        chart = BarChart()
        chart.type = "bar"
        chart.style = 2
        chart.title = "노선별 최대 지연(분)"
        if chart_max > 30:  # 미출발 등 큰 값 하나 때문에 나머지 막대가 안 보이지 않도록
            chart.y_axis.scaling.max = 30
            chart.title = "노선별 최대 지연(분, 30분 초과는 잘림)"
        chart.y_axis.title = "분"
        chart.y_axis.majorGridlines = None
        chart.x_axis.scaling.orientation = "maxMin"  # 표와 같은 순서(위→아래)
        chart.legend = None
        data = Reference(ws, min_col=9, min_row=8, max_row=last)
        cats = Reference(ws, min_col=3, min_row=first, max_row=last)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        s = chart.series[0]
        s.graphicalProperties.solidFill = BLUE
        s.graphicalProperties.line.solidFill = BLUE
        chart.gapWidth = 60
        chart.width = 16
        chart.height = max(7.5, 0.65 * len(days) + 3)
        ws.add_chart(chart, "P4")
    _page(ws)


def _anomaly_sheet(ws, d, wd, anomalies, rules) -> None:
    headers = ["No", "노선ID", "노선명", "차량", "구분", "지점", "계획", "실제", "지연(분)", "최초감지", "상태", "비고"]
    widths = [5, 9, 22, 13, 11, 22, 8, 8, 9, 9, 8, 52]
    _title(ws, f"이상 발생 내역 — {d:%Y-%m-%d}({wd})",
           _criteria(rules) + "\n상태: 확정=실제 시각 확인 / 진행중=감시 종료까지 미출발·미도착", len(headers))
    _header(ws, 4, headers, widths)
    if not anomalies:
        ws.merge_cells("A5:L5")
        _body(ws["A5"], "✅ 이상 발생 없음 — 모든 노선 정상 운행")
        ws["A5"].font = _font(11, True, "0CA30C")
        ws.row_dimensions[5].height = 26
    for i, a in enumerate(anomalies):
        r = 5 + i
        tint = PatternFill("solid", fgColor=KIND_TINT.get(a["kind"], "FFFFFF"))
        actual = _t(a["actual"])
        if actual is None:
            actual = "미출발" if a["kind"] == "출발지연" else ("미도착" if a["kind"] == "정차지연" else "-")
        delay = _trunc(a["delay_min"])
        values = [i + 1, a["route_id"], a["route_name"], a["vehicle_id"], a["kind"], a["point_name"],
                  _t(a["planned"]), actual, delay, _t(a["detected_at"]), a["status"], a["note"]]
        for c, v in enumerate(values, 1):
            cell = _body(ws.cell(row=r, column=c), v, align=LEFT if c in (3, 6, 12) else CENTER, bold=(c == 5))
            cell.fill = tint
            if c in (7, 8, 10) and not isinstance(v, str):
                cell.number_format = "hh:mm"
            if c == 9:
                cell.number_format = "0;-0;0"
        ws.row_dimensions[r].height = 20 if len(a["note"] or "") < 50 else 34
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:L{4 + max(1, len(anomalies))}"
    _page(ws)


def _detail_sheet(ws, d, wd, days, by_route) -> None:
    headers = ["노선", "순번", "구분", "지점명", "계획", "실제", "편차(분)", "판정", "비고"]
    widths = [9, 6, 8, 26, 9, 9, 9, 10, 46]
    _title(ws, f"노선별 운행 상세 — {d:%Y-%m-%d}({wd})", "노선 머리줄 왼쪽의 [-] 를 누르면 접을 수 있습니다.", len(headers))
    _header(ws, 4, headers, widths)
    r = 5
    for rd in days:
        label, _, _ = STATUS_STYLE.get(rd["status"], (rd["status"], "", ""))
        driver = f" · 기사 {rd['driver']}" if rd.get("driver") else ""
        summary = f" ({rd['summary']})" if rd["summary"] else ""
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
        head = ws.cell(row=r, column=1, value=f"[{rd['route_id']}] {rd['route_name']} · {rd['vehicle_id']}{driver}"
                                              f"  —  판정 {label}{summary}")
        head.font, head.fill, head.alignment = _font(10, True, NAVY), GROUP_FILL, LEFT
        ws.row_dimensions[r].height = 22
        r += 1
        start = r
        for p in by_route.get(rd["route_id"], []):
            actual = _t(p["actual"]) or ("-" if p["status"] in ("대기", "미확인") else
                                         ("미출발" if p["idx"] == 0 else "미도착") if p["status"] == "이상" else "-")
            values = [rd["route_id"], p["idx"], p["kind"], p["point_name"], _t(p["planned"]), actual,
                      _trunc(p["delay_min"]), None, p["note"]]
            for c, v in enumerate(values, 1):
                cell = _body(ws.cell(row=r, column=c), v, align=LEFT if c in (4, 9) else CENTER)
                if c in (5, 6) and not isinstance(v, str):
                    cell.number_format = "hh:mm"
                if c == 7:
                    cell.number_format = "0;-0;0"
            _status_cell(ws.cell(row=r, column=8), p["status"])
            ws.cell(row=r, column=8).border = BORDER
            r += 1
        if r > start:
            ws.row_dimensions.group(start, r - 1, outline_level=1)
    ws.freeze_panes = "A5"
    _page(ws)


def _alert_sheet(ws, d, wd, alerts) -> None:
    headers = ["발송시각", "등급", "노선ID", "제목", "내용", "발송채널", "성공"]
    widths = [10, 7, 9, 30, 60, 22, 6]
    _title(ws, f"알림 발송 이력 — {d:%Y-%m-%d}({wd})", "메신저로 실제 발송된 알림과 결과", len(headers))
    _header(ws, 4, headers, widths)
    for i, a in enumerate(alerts):
        r = 5 + i
        values = [_dt(a["sent_at"]), LEVEL_KO.get(a["level"], a["level"]), a["route_id"], a["title"], a["body"],
                  a["channels"], "O" if a["ok"] else "X"]
        for c, v in enumerate(values, 1):
            cell = _body(ws.cell(row=r, column=c), v, align=LEFT if c in (4, 5, 6) else CENTER)
            if c == 1:
                cell.number_format = "hh:mm:ss"
        lines = (a["body"] or "").count("\n") + 1
        ws.row_dimensions[r].height = min(15 * lines + 4, 160)
    ws.freeze_panes = "A5"
    _page(ws)


def _criteria_sheet(ws, rules: Rules, source_name: str, gen: datetime) -> None:
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 60
    _title(ws, "판정 기준 (config.yaml 의 rules)", "값을 바꾸려면 config/config.yaml 의 rules 를 수정하세요. "
                                                 "노선별로 다르게 하려면 routes.yaml 의 노선에 rules 를 적습니다.", 3)
    _header(ws, 4, ["항목", "값", "설명"])
    early = rules.early_departure_min if rules.early_departure_min is not None else "미사용"
    rows = [
        ("기점 출발 지연 → 이상 (분)", rules.departure_delay_min, "계획 출발시각 + N분이 지나도 미출발이면 즉시 알림"),
        ("기점 출발 지연 → 주의 (분)", rules.departure_caution_min, "보고서에만 '주의' 표시 (알림 없음)"),
        ("조기 출발 → 이상 (분)", early, "계획보다 N분 이상 일찍 출발하면 미탑승자 발생 가능"),
        ("정차지/종점 지연 → 이상 (분)", rules.stop_delay_min, "계획 도착시각 + N분이 지나도 미도착이면 즉시 알림"),
        ("정차지/종점 지연 → 주의 (분)", rules.stop_caution_min, "보고서에만 '주의' 표시 (알림 없음)"),
        ("위치 미수신 알림 (분)", rules.no_signal_min, "운행 중 N분 이상 위치가 안 들어오면 알림"),
        ("지점 도착 판정 반경 기본값 (m)", rules.default_radius_m, "지점별 radius_m 으로 개별 지정 가능"),
        ("출발 확정 거리 (m)", rules.departure_confirm_m, "기점 반경을 벗어나 이 거리 이상 멀어지면 출발로 확정"),
        ("기점 감시 시작 (계획출발 N분 전)", rules.pre_departure_window_min, "이보다 이른 위치는 출발 판정에 쓰지 않음"),
        ("데이터 소스", source_name or "-", "simulator / excel / http_api / uvis_web"),
        ("보고서 생성 시각", gen.strftime("%Y-%m-%d %H:%M"), ""),
    ]
    for i, (k, v, desc) in enumerate(rows):
        r = 5 + i
        _body(ws.cell(row=r, column=1), k, align=LEFT, bold=True)
        _body(ws.cell(row=r, column=2), v)
        _body(ws.cell(row=r, column=3), desc, align=LEFT)
    _page(ws, landscape=False)


# ====================================================================== 월간 보고서
KINDS = ["출발지연", "조기출발", "정차지연", "미경유", "위치미수신"]


def build_monthly_report(store, month: str, rules: Rules, out_dir: Path) -> Path:
    days = store.month_route_days(month)
    anomalies = store.anomalies(month=month)
    routes: dict[str, str] = {}
    run_days: dict[str, int] = defaultdict(int)
    bad_days: dict[str, int] = defaultdict(int)
    dates = sorted({rd["service_date"] for rd in days})
    for rd in days:
        routes[rd["route_id"]] = rd["route_name"]
        run_days[rd["route_id"]] += 1
        if rd["status"] == "이상":
            bad_days[rd["route_id"]] += 1
    route_ids = sorted(routes)

    wb = Workbook()
    ws = wb.active
    ws.title = "월간요약"
    detail_name = "월간이상내역"
    headers = ["노선ID", "노선명", "운행일수", "이상발생일수"] + KINDS + ["이상 합계", "정시운행률"]
    widths = [9, 24, 9, 11, 10, 10, 10, 9, 11, 10, 11]
    _title(ws, f"통근버스 출근 운행 월간 집계 — {month}",
           _criteria(rules) + "\n정시운행률 = (운행일수 - 이상발생일수) / 운행일수", len(headers))
    _header(ws, 4, headers, widths)
    first = 5
    for i, rid in enumerate(route_ids):
        r = first + i
        _body(ws.cell(row=r, column=1), rid)
        _body(ws.cell(row=r, column=2), routes[rid], align=LEFT)
        _body(ws.cell(row=r, column=3), run_days[rid], "0")
        _body(ws.cell(row=r, column=4), bad_days[rid], "0")
        for j, kind in enumerate(KINDS):
            col = 5 + j
            letter = get_column_letter(col)
            _body(ws.cell(row=r, column=col),
                  f"=COUNTIFS({detail_name}!$C:$C,$A{r},{detail_name}!$F:$F,{letter}$4)", "0")
        _body(ws.cell(row=r, column=10), f"=SUM(E{r}:I{r})", "0", bold=True)
        _body(ws.cell(row=r, column=11), f'=IF(C{r}=0,"-",(C{r}-D{r})/C{r})', "0.0%", bold=True)
    last = first + max(len(route_ids), 1) - 1
    tr = last + 1
    _body(ws.cell(row=tr, column=1), "합계", bold=True)
    _body(ws.cell(row=tr, column=2), "")
    for col in range(3, 11):
        letter = get_column_letter(col)
        _body(ws.cell(row=tr, column=col), f"=SUM({letter}{first}:{letter}{last})", "0", bold=True)
    _body(ws.cell(row=tr, column=11), f'=IF(C{tr}=0,"-",(C{tr}-D{tr})/C{tr})', "0.0%", bold=True)
    for col in range(1, 12):
        ws.cell(row=tr, column=col).fill = GROUP_FILL
    ws.conditional_formatting.add(f"K{first}:K{last}", ColorScaleRule(
        start_type="num", start_value=0.8, start_color="FBE3E3", end_type="num", end_value=1, end_color="FFFFFF"))
    ws.freeze_panes = "C5"
    _page(ws)

    # 일별 매트릭스 (노선 × 날짜, 값 = 이상 건수)
    mx = wb.create_sheet("일별매트릭스")
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for a in anomalies:
        counts[(a["route_id"], a["service_date"])] += 1
    ran = {(rd["route_id"], rd["service_date"]) for rd in days}
    mx_headers = ["노선ID", "노선명"] + [f"{date.fromisoformat(x):%m/%d}\n({WEEKDAY_KO[date.fromisoformat(x).weekday()]})"
                                       for x in dates] + ["합계"]
    _title(mx, f"노선 × 날짜 이상 건수 — {month}", "빈칸 = 운행 기록 없음, 0 = 정상 운행. 색이 진할수록 이상 건수가 많음", len(mx_headers))
    _header(mx, 4, mx_headers, [9, 22] + [8] * len(dates) + [8])
    for i, rid in enumerate(route_ids):
        r = 5 + i
        _body(mx.cell(row=r, column=1), rid)
        _body(mx.cell(row=r, column=2), routes[rid], align=LEFT)
        for j, ds in enumerate(dates):
            v = counts.get((rid, ds), 0) if (rid, ds) in ran else None
            _body(mx.cell(row=r, column=3 + j), v, "0")
        tc = 3 + len(dates)
        _body(mx.cell(row=r, column=tc),
              f"=SUM({get_column_letter(3)}{r}:{get_column_letter(tc - 1)}{r})" if dates else 0, "0", bold=True)
    if dates and route_ids:
        area = f"C5:{get_column_letter(2 + len(dates))}{4 + len(route_ids)}"
        # 진한 칸에서도 검은 글자가 읽히도록 끝 색을 중간 밝기로 둔다
        mx.conditional_formatting.add(area, ColorScaleRule(start_type="num", start_value=0, start_color="FFFFFF",
                                                           mid_type="num", mid_value=1, mid_color="CDE2FB",
                                                           end_type="max", end_color="5598E7"))
    mx.freeze_panes = "C5"
    _page(mx)

    # 월간 이상 내역 (월간요약 수식이 참조)
    dt = wb.create_sheet(detail_name)
    d_headers = ["운행일", "요일", "노선ID", "노선명", "차량", "구분", "지점", "계획", "실제", "지연(분)", "상태", "비고"]
    _title(dt, f"월간 이상 내역 — {month}", "해제(기준 미만으로 확인된 건)는 제외", len(d_headers))
    _header(dt, 4, d_headers, [11, 6, 9, 22, 13, 11, 22, 8, 8, 9, 8, 50])
    for i, a in enumerate(anomalies):
        r = 5 + i
        sd = date.fromisoformat(a["service_date"])
        actual = _t(a["actual"]) or ("미출발" if a["kind"] == "출발지연" else "미도착" if a["kind"] == "정차지연" else "-")
        values = [sd, WEEKDAY_KO[sd.weekday()], a["route_id"], a["route_name"], a["vehicle_id"], a["kind"],
                  a["point_name"], _t(a["planned"]), actual, _trunc(a["delay_min"]), a["status"], a["note"]]
        tint = PatternFill("solid", fgColor=KIND_TINT.get(a["kind"], "FFFFFF"))
        for c, v in enumerate(values, 1):
            cell = _body(dt.cell(row=r, column=c), v, align=LEFT if c in (4, 7, 12) else CENTER, bold=(c == 6))
            cell.fill = tint
            if c == 1:
                cell.number_format = "yyyy-mm-dd"
            elif c in (8, 9) and not isinstance(v, str):
                cell.number_format = "hh:mm"
            elif c == 10:
                cell.number_format = "0;-0;0"
    dt.freeze_panes = "A5"
    dt.auto_filter.ref = f"A4:L{4 + max(1, len(anomalies))}"
    _page(dt)

    wb.calculation.fullCalcOnLoad = True
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{month}_월간운행집계.xlsx"
    wb.save(path)
    return path
