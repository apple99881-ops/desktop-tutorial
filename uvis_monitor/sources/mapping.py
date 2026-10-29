"""원시 레코드(JSON/엑셀 행) → VehiclePosition 변환 공통 도구.

UVIS 화면/엑셀/API 마다 필드 이름이 다르므로 설정의 fields 매핑으로 맞춘다.
    fields: {vehicle_id: carNo, timestamp: gpsDt, lat: lat, lon: lng, speed: spd, address: addr}
값에는 점(.) 경로를 쓸 수 있다: "gps.lat", "list.0.carNo"
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from ..models import KST, VehiclePosition

log = logging.getLogger(__name__)

TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d %H%M%S", "%Y%m%d%H%M",
    "%Y-%m-%d %p %I:%M:%S", "%Y-%m-%d %p %I:%M", "%Y.%m.%d %p %I:%M:%S",
    "%Y. %m. %d. %p %I:%M:%S", "%Y. %m. %d. %H:%M:%S",
)
TIME_ONLY_FORMATS = ("%H:%M:%S", "%H:%M", "%p %I:%M:%S", "%p %I:%M")
EXCEL_EPOCH = datetime(1899, 12, 30)


def get_path(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def _from_number(num: float, fmt: str) -> datetime | None:
    if fmt == "epoch_ms" or (fmt == "auto" and 1e12 < num < 1e14):
        return datetime.fromtimestamp(num / 1000, KST)
    if fmt == "epoch" or (fmt == "auto" and 1e9 < num < 1e11):
        return datetime.fromtimestamp(num, KST)
    if fmt == "excel" or (fmt == "auto" and 20000 < num < 80000):  # 엑셀 날짜 일련번호
        return _aware(EXCEL_EPOCH + timedelta(days=num))
    return None


def parse_timestamp(value: Any, fmt: str | None = "auto", service_date: date | None = None) -> datetime | None:
    """여러 형식의 시각 값을 KST datetime 으로. 실패하면 None.

    fmt: auto(자동) | epoch | epoch_ms | excel | strptime 형식(예: "%Y%m%d%H%M%S")
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, date):
        return _aware(datetime.combine(value, datetime.min.time()))
    fmt = fmt or "auto"
    if isinstance(value, (int, float)):
        return _from_number(float(value), fmt)
    text = str(value).strip()
    if fmt in ("epoch", "epoch_ms", "excel"):
        try:
            return _from_number(float(text), fmt)
        except ValueError:
            return None
    if fmt == "auto" and re.fullmatch(r"\d+(\.\d+)?", text) and len(text.split(".")[0]) not in (12, 14):
        return _from_number(float(text), "auto")
    text = text.replace("오전", "AM").replace("오후", "PM")
    if fmt != "auto":
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            return None
        if dt.year == 1900 and service_date:
            dt = datetime.combine(service_date, dt.time())
        return _aware(dt)
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        pass
    for f in TS_FORMATS:
        try:
            return _aware(datetime.strptime(text, f))
        except ValueError:
            continue
    if service_date is not None:
        for f in TIME_ONLY_FORMATS:
            try:
                return _aware(datetime.combine(service_date, datetime.strptime(text, f).time()))
            except ValueError:
                continue
    return None


def normalize_coord(value: Any, kind: str = "") -> float | None:
    """위도/경도 값 정규화. 37123456 처럼 정수로 오는 값은 180 이하가 되도록 자릿수를 맞춘다.

    위도/경도 열이 뒤바뀐 경우를 바로잡을 수 있도록 여기서는 위도도 180 기준으로 맞추고,
    최종 검증과 교정은 records_to_positions 에서 한다.
    """
    if value is None or value == "":
        return None
    try:
        v = float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    if v == 0:
        return None
    k = 0
    while abs(v) / 10 ** k > 180.0:
        k += 1
    return round(v / 10 ** k, 7) if k else v


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace(",", "").replace("km/h", ""))
    except ValueError:
        return None


def records_to_positions(records: Iterable[Any], fields: dict, ts_format: str = "auto",
                         service_date: date | None = None, default_vehicle: str | None = None,
                         swap_xy: bool = False) -> list[VehiclePosition]:
    """dict 레코드들을 VehiclePosition 목록으로. 해석할 수 없는 행은 건너뛴다."""
    out: list[VehiclePosition] = []
    skipped = 0
    for rec in records or []:
        vid = get_path(rec, fields.get("vehicle_id")) if fields.get("vehicle_id") else None
        vid = str(vid).strip() if vid not in (None, "") else default_vehicle
        ts_raw = get_path(rec, fields.get("timestamp"))
        if fields.get("date") and ts_raw is not None and not isinstance(ts_raw, datetime):
            d_raw = get_path(rec, fields["date"])
            d_val = parse_timestamp(d_raw, "auto")
            ts = parse_timestamp(ts_raw, ts_format, d_val.date() if d_val else service_date)
        else:
            ts = parse_timestamp(ts_raw, ts_format, service_date)
        lat = normalize_coord(get_path(rec, fields.get("lat")), "lat")
        lon = normalize_coord(get_path(rec, fields.get("lon")), "lon")
        if swap_xy and lat is not None and lon is not None:
            lat, lon = lon, lat
        if not vid or ts is None or lat is None or lon is None:
            skipped += 1
            continue
        if lat > lon:  # 위도/경도 열이 뒤바뀐 경우 (한국: 위도 33~39 < 경도 124~132)
            lat, lon = lon, lat
        if abs(lat) > 90:
            skipped += 1
            continue
        speed = _float(get_path(rec, fields.get("speed"))) if fields.get("speed") else None
        addr = get_path(rec, fields.get("address")) if fields.get("address") else ""
        out.append(VehiclePosition(vehicle_id=vid, ts=ts, lat=lat, lon=lon, speed_kmh=speed,
                                   address=str(addr or "").strip()))
    if skipped:
        log.debug("해석 불가 레코드 %d건 건너뜀", skipped)
    return out
