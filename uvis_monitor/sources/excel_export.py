"""UVIS 엑셀/CSV 다운로드 파일 읽기.

UVIS 웹의 '운행경로 조회 / 위치 이력' 화면에서 엑셀 다운로드한 파일을 읽어
차량 위치로 변환한다. 실제 열 이름을 몰라도 아래 별칭으로 자동 인식하며,
인식이 안 되면 config.yaml 의 source.excel.columns 로 직접 지정한다.

지원 형식: .xlsx/.xlsm, .csv(UTF-8/CP949), .xls(진짜 엑셀은 xlrd 필요,
한국 웹사이트에서 흔한 'HTML 표를 .xls 로 저장한 파일'은 그대로 읽음), .html
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from openpyxl.utils import get_column_letter

from ..models import VehiclePosition
from .base import PositionSource
from .mapping import records_to_positions

log = logging.getLogger(__name__)

ALIASES: dict[str, list[str]] = {
    "vehicle_id": ["차량번호", "차번", "차량명", "차량", "차량no", "차량id", "차량코드", "단말기번호", "단말번호",
                   "단말기", "vehicle", "vehicleid", "carno", "car"],
    "timestamp": ["수신일시", "수신시간", "수신시각", "발생일시", "발생시간", "발생시각", "gps일시", "gps시간", "gps시각",
                  "위치일시", "위치시간", "운행일시", "기록일시", "일시", "날짜시간", "일자시간", "시간", "시각",
                  "datetime", "timestamp", "time"],
    "date": ["일자", "날짜", "운행일자", "기록일자", "date"],
    "lat": ["위도", "gps위도", "lat", "latitude", "y좌표", "y"],
    "lon": ["경도", "gps경도", "lon", "lng", "longitude", "x좌표", "x"],
    "speed": ["속도", "속도kmh", "주행속도", "현재속도", "speed"],
    "address": ["주소", "위치", "현재위치", "위치정보", "address"],
}
VEHICLE_IN_NAME = re.compile(r"([가-힣]{0,2}\d{2,3}[가-힣]\d{4})")


def _norm(text) -> str:
    return re.sub(r"[\s_\-/()\[\].:]", "", str(text or "")).lower()


def detect_columns(header: list) -> dict[str, int]:
    """헤더 행에서 필드별 열 번호를 찾는다 (정확히 일치하는 별칭만)."""
    normed = [_norm(h) for h in header]
    found: dict[str, int] = {}
    for fld, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normed:
                idx = normed.index(alias)
                if idx not in found.values():
                    found[fld] = idx
                    break
    return found


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_rows(path: Path) -> list[list]:
    """파일 형식에 상관없이 2차원 행 목록으로 읽는다 (첫 번째로 데이터가 있는 시트)."""
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    head = raw[:512].lstrip().lower()
    if suffix in (".html", ".htm") or (suffix == ".xls" and (head.startswith(b"<") or b"<table" in head)):
        parser = _TableParser()
        parser.feed(_decode(raw))
        return parser.rows
    if suffix in (".csv", ".txt"):
        text = _decode(raw)
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;") if text.strip() else csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect)]
    if suffix == ".xls":
        try:
            import xlrd  # type: ignore
        except ImportError as exc:
            raise RuntimeError(f"{path.name}: 구형 .xls 파일입니다. 'pip install xlrd' 후 다시 실행하거나, "
                               f"엑셀에서 .xlsx 로 다른 이름 저장해 주세요.") from exc
        book = xlrd.open_workbook(file_contents=raw)
        for sheet in book.sheets():
            if sheet.nrows:
                rows = []
                for r in range(sheet.nrows):
                    row = []
                    for c in range(sheet.ncols):
                        cell = sheet.cell(r, c)
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                        else:
                            row.append(cell.value)
                    rows.append(row)
                return rows
        return []
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if any(any(c not in (None, "") for c in r) for r in rows):
                return rows
    finally:
        wb.close()
    return []


def find_header(rows: list[list], columns: dict | None = None) -> tuple[int, dict[str, int]]:
    """제목/조회조건 줄을 건너뛰고 실제 헤더 행을 찾는다."""
    for i, row in enumerate(rows[:40]):
        header = [str(c) if c is not None else "" for c in row]
        if columns:
            mapping = {}
            normed = [_norm(h) for h in header]
            for fld, col in columns.items():
                if isinstance(col, int):
                    mapping[fld] = col
                elif _norm(col) in normed:
                    mapping[fld] = normed.index(_norm(col))
            if {"lat", "lon"} <= mapping.keys() and ("timestamp" in mapping):
                return i, mapping
            continue
        found = detect_columns(header)
        if {"lat", "lon", "timestamp"} <= found.keys():
            return i, found
    raise ValueError("헤더 행(위도/경도/시간 열)을 찾지 못했습니다. "
                     "'python -m uvis_monitor inspect-excel 파일' 로 열 이름을 확인한 뒤 "
                     "config.yaml 의 source.excel.columns 에 지정하세요.")


def load_positions(path: str | Path, columns: dict | None = None, default_vehicle: str | None = None,
                   service_date: date | None = None, ts_format: str = "auto") -> list[VehiclePosition]:
    path = Path(path)
    rows = read_rows(path)
    hdr, mapping = find_header(rows, columns)
    if "vehicle_id" not in mapping and not default_vehicle:
        m = VEHICLE_IN_NAME.search(path.stem)
        default_vehicle = m.group(1) if m else None
        if not default_vehicle:
            raise ValueError(f"{path.name}: 차량 열이 없고 파일명에서도 차량번호를 찾지 못했습니다. "
                             f"source.excel.default_vehicle 을 지정하세요.")
    records = []
    for row in rows[hdr + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        records.append({fld: (row[idx] if idx < len(row) else None) for fld, idx in mapping.items()})
    fields = {f: f for f in mapping}
    positions = records_to_positions(records, fields, ts_format=ts_format, service_date=service_date,
                                     default_vehicle=default_vehicle)
    log.info("%s: %d행 중 위치 %d건 인식", path.name, len(records), len(positions))
    return positions


def inspect(path: str | Path) -> str:
    """inspect-excel 명령용: 파일 구조와 자동 인식 결과를 설명한다."""
    path = Path(path)
    rows = read_rows(path)
    lines = [f"파일: {path}", f"전체 행 수: {len(rows)}", "", "[앞부분 8행]"]
    for i, row in enumerate(rows[:8], 1):
        lines.append(f"  {i:>2}행: " + " | ".join("" if c is None else str(c) for c in row[:14]))
    try:
        hdr, mapping = find_header(rows)
        header = rows[hdr]
        lines += ["", f"[헤더 행] {hdr + 1}행", "[자동 인식된 열]"]
        for fld, idx in mapping.items():
            lines.append(f"  {fld:<10} ← {get_column_letter(idx + 1)}열 '{header[idx]}'")
        positions = load_positions(path)
        lines.append("")
        lines.append(f"[인식된 위치] {len(positions)}건")
        vehicles: dict[str, int] = {}
        for p in positions:
            vehicles[p.vehicle_id] = vehicles.get(p.vehicle_id, 0) + 1
        for v, cnt in sorted(vehicles.items()):
            lines.append(f"  차량 '{v}': {cnt}건")
        if positions:
            first = min(positions, key=lambda p: p.ts)
            last = max(positions, key=lambda p: p.ts)
            lines.append(f"  기간: {first.ts:%Y-%m-%d %H:%M:%S} ~ {last.ts:%Y-%m-%d %H:%M:%S}")
            lines.append(f"  예시: {first.vehicle_id} {first.ts:%H:%M:%S} ({first.lat:.6f}, {first.lon:.6f})")
        lines.append("\n→ routes.yaml 의 vehicle_id 를 위 '차량' 값과 똑같이 적어야 매칭됩니다.")
    except ValueError as exc:
        lines += ["", f"[자동 인식 실패] {exc}"]
    return "\n".join(lines)


class ExcelSource(PositionSource):
    """파일(들) 또는 폴더를 읽어 now 이전의 새 위치를 순서대로 내보낸다.

    - files: 지정 파일들 (지난 데이터 재분석 replay 용)
    - watch_dir: 폴더의 엑셀을 매 주기 다시 확인 (자동 다운로드와 조합해 준실시간 감시)
    """

    name = "excel"

    def __init__(self, files: list[str | Path] | None = None, watch_dir: str | Path | None = None,
                 pattern: str = "*.xls*", columns: dict | None = None, default_vehicle: str | None = None,
                 service_date: date | None = None, ts_format: str = "auto"):
        self.files = [Path(f) for f in files or []]
        self.watch_dir = Path(watch_dir) if watch_dir else None
        self.pattern = pattern
        self.columns = columns
        self.default_vehicle = default_vehicle
        self.service_date = service_date
        self.ts_format = ts_format
        self._mtimes: dict[Path, float] = {}
        self._records: dict[tuple[str, datetime], VehiclePosition] = {}
        self._cursor: dict[str, datetime] = {}  # 차량별 마지막으로 내보낸 시각
        if not self.files and not self.watch_dir:
            raise ValueError("excel 소스에는 files 또는 watch_dir 이 필요합니다.")

    def _candidates(self) -> list[Path]:
        out = list(self.files)
        if self.watch_dir and self.watch_dir.is_dir():
            for pat in self.pattern.split(";"):
                out += sorted(self.watch_dir.glob(pat.strip()))
            out += sorted(self.watch_dir.glob("*.csv"))
        return [p for p in dict.fromkeys(out) if not p.name.startswith("~$")]

    def _reload(self) -> None:
        for path in self._candidates():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(path) == mtime:
                continue
            try:
                for p in load_positions(path, self.columns, self.default_vehicle, self.service_date, self.ts_format):
                    self._records[(p.vehicle_id, p.ts)] = p
                self._mtimes[path] = mtime
            except Exception as exc:  # 다운로드 중인 파일 등은 다음 주기에 재시도
                log.warning("%s 읽기 실패: %s", path.name, exc)

    def all_positions(self) -> list[VehiclePosition]:
        self._reload()
        return sorted(self._records.values(), key=lambda p: p.ts)

    def time_range(self) -> tuple[datetime, datetime] | None:
        ps = self.all_positions()
        return (ps[0].ts, ps[-1].ts) if ps else None

    def fetch(self, now: datetime) -> list[VehiclePosition]:
        self._reload()
        out = [p for p in self._records.values()
               if p.ts <= now and (p.vehicle_id not in self._cursor or p.ts > self._cursor[p.vehicle_id])]
        for p in out:
            if p.vehicle_id not in self._cursor or p.ts > self._cursor[p.vehicle_id]:
                self._cursor[p.vehicle_id] = p.ts
        return out
