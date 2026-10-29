"""HTTP(JSON) API 소스 — 에스원에서 데이터 연동 API 를 제공받았거나,
web-probe 로 찾아낸 UVIS 내부 조회 주소를 직접 호출할 때 사용한다.

config.yaml 예:
  source:
    type: http_api
    http_api:
      url: https://example/api/vehicles/locations
      method: GET
      headers: {Authorization: "Bearer ${UVIS_API_TOKEN}"}
      params: {}
      records_path: data.list
      fields: {vehicle_id: carNo, timestamp: gpsDt, lat: lat, lon: lng, speed: speed, address: addr}
      ts_format: auto
"""
from __future__ import annotations

from datetime import datetime

import requests

from ..models import VehiclePosition
from .base import PositionSource
from .mapping import get_path, records_to_positions


class HttpApiSource(PositionSource):
    name = "http_api"

    def __init__(self, url: str, fields: dict, method: str = "GET", headers: dict | None = None,
                 params: dict | None = None, json_body: dict | None = None, form: dict | None = None,
                 records_path: str = "", ts_format: str = "auto", timeout: float = 15, verify: bool = True):
        if not url:
            raise ValueError("http_api.url 이 필요합니다.")
        self.url, self.method, self.fields = url, method.upper(), fields
        self.params, self.json_body, self.form = params, json_body, form
        self.records_path, self.ts_format = records_path, ts_format
        self.timeout, self.verify = timeout, verify
        self.session = requests.Session()
        self.session.headers.update(headers or {})

    def fetch(self, now: datetime) -> list[VehiclePosition]:
        resp = self.session.request(self.method, self.url, params=self.params, json=self.json_body,
                                    data=self.form, timeout=self.timeout, verify=self.verify)
        resp.raise_for_status()
        records = get_path(resp.json(), self.records_path)
        if isinstance(records, dict):
            records = [records]
        return records_to_positions(records or [], self.fields, self.ts_format, service_date=now.date())

    def close(self) -> None:
        self.session.close()
