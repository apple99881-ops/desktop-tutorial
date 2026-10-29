"""위치 데이터 소스 모음과 생성 함수."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..config import AppConfig
from ..models import Route
from .base import PositionSource


def create_source(cfg: AppConfig, routes: list[Route], service_date: date) -> PositionSource:
    src = cfg.source or {}
    kind = src.get("type", "simulator")
    opts = src.get(kind) or {}
    if kind == "simulator":
        from .simulator import SimulatorSource
        return SimulatorSource(routes, service_date, seed=int(opts.get("seed", 42)),
                               noise_m=float(opts.get("noise_m", 8)))
    if kind == "excel":
        from .excel_export import ExcelSource
        def resolve(p):
            return Path(p) if Path(p).is_absolute() else cfg.base_dir / p
        files = [resolve(f) for f in opts.get("files") or []]
        watch = opts.get("watch_dir")
        return ExcelSource(files=files, watch_dir=resolve(watch) if watch else None,
                           pattern=opts.get("pattern", "*.xls*"), columns=opts.get("columns"),
                           default_vehicle=opts.get("default_vehicle"), service_date=service_date,
                           ts_format=opts.get("ts_format", "auto"))
    if kind == "http_api":
        from .http_api import HttpApiSource
        return HttpApiSource(url=opts.get("url", ""), fields=opts.get("fields") or {},
                             method=opts.get("method", "GET"), headers=opts.get("headers"),
                             params=opts.get("params"), json_body=opts.get("json"), form=opts.get("form"),
                             records_path=opts.get("records_path", ""), ts_format=opts.get("ts_format", "auto"),
                             timeout=float(opts.get("timeout", 15)), verify=bool(opts.get("verify", True)))
    if kind == "uvis_web":
        from .uvis_web import UvisWebSource
        return UvisWebSource(opts, state_dir=cfg.database.parent)
    raise ValueError(f"알 수 없는 source.type: {kind} (simulator / excel / http_api / uvis_web)")


__all__ = ["PositionSource", "create_source"]
