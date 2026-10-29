"""위치 데이터 소스 공통 인터페이스.

새 데이터 소스를 추가하려면 PositionSource 를 상속해 fetch() 만 구현하고
sources/__init__.py 의 create_source() 에 type 이름을 등록하면 된다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import VehiclePosition


class PositionSource(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, now: datetime) -> list[VehiclePosition]:
        """now 시점까지 새로 수집된 차량 위치 목록 (시간순 정렬 불필요, 중복 허용)."""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
