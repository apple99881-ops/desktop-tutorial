"""화면(콘솔) 출력 — 시험용, 그리고 다른 채널이 실패해도 기록이 남도록."""
from __future__ import annotations

from pathlib import Path

from ..models import Alert, now_kst
from .base import Notifier


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, clock=None, **kw):
        super().__init__(**kw)
        self.clock = clock

    def _stamp(self) -> str:
        now = self.clock.now() if self.clock else now_kst()
        return now.strftime("%H:%M:%S")

    def send(self, alert: Alert) -> bool:
        return self.send_text(alert.text())

    def send_text(self, text: str) -> bool:
        print(f"\n[{self._stamp()}] {text}", flush=True)
        return True

    def send_file(self, path: Path, caption: str = "") -> bool:
        print(f"\n[{self._stamp()}] 📎 {caption} → {path}", flush=True)
        return True
