"""알림 채널 공통 인터페이스와 메시지 도우미.

새 메신저를 추가하려면 Notifier 를 상속해 send_text() 를 구현하고
notify/__init__.py 의 create_hub() 에 type 이름을 등록한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import quote

from ..models import Alert, Level

ALL_LEVELS = {lv.value for lv in Level}


def map_link(alert: Alert) -> str | None:
    if alert.lat is None or alert.lon is None:
        return None
    name = quote((alert.place or "위치").replace(",", " "))
    return f"https://map.kakao.com/link/map/{name},{alert.lat:.6f},{alert.lon:.6f}"


def split_text(text: str, max_len: int) -> list[str]:
    """줄 단위로 max_len 이하 조각으로 나눈다 (여러 조각이면 (1/3) 표시)."""
    if len(text) <= max_len:
        return [text]
    budget = max_len - 7  # "(1/9) " 머리말 자리
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > budget:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:budget])
            line = line[budget:]
        if cur and len(cur) + 1 + len(line) > budget:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    total = len(chunks)
    return [f"({i}/{total}) {c}" for i, c in enumerate(chunks, 1)]


class Notifier(ABC):
    name = "base"

    def __init__(self, levels: list[str] | None = None, summary: bool = True):
        self.levels = set(levels) if levels else set(ALL_LEVELS)
        self.summary = summary  # 일일 요약 / 실시간 현황판 수신 여부

    def wants(self, alert: Alert) -> bool:
        return alert.level.value in self.levels

    def send(self, alert: Alert) -> bool:
        link = map_link(alert)
        return self.send_text(alert.text() + (f"\n📍 {link}" if link else ""))

    @abstractmethod
    def send_text(self, text: str) -> bool:
        ...

    def send_file(self, path: Path, caption: str = "") -> bool:
        return False  # 파일 전송 미지원 채널

    def poll_commands(self) -> list[str]:
        return []
