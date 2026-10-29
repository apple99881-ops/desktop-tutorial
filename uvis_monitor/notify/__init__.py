"""알림 채널 생성과 일괄 발송(NotifierHub)."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import AppConfig
from ..models import Alert
from .base import Notifier
from .console import ConsoleNotifier

log = logging.getLogger(__name__)


class NotifierHub:
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def send(self, alert: Alert) -> list[tuple[str, bool]]:
        results = []
        for n in self.notifiers:
            if n.wants(alert):
                results.append((n.name, self._safe(n.send, alert)))
        return results

    def send_text(self, text: str, summary: bool = False) -> list[tuple[str, bool]]:
        return [(n.name, self._safe(n.send_text, text)) for n in self.notifiers if not summary or n.summary]

    def send_file(self, path: Path, caption: str = "") -> list[tuple[str, bool]]:
        return [(n.name, self._safe(n.send_file, path, caption)) for n in self.notifiers if n.summary]

    def poll_commands(self) -> list[tuple[Notifier, str]]:
        out = []
        for n in self.notifiers:
            out += [(n, c) for c in self._safe(n.poll_commands) or []]
        return out

    @staticmethod
    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # 알림 실패가 감시를 멈추게 하지 않는다
            log.exception("알림 오류(%s): %s", getattr(fn, "__qualname__", fn), exc)
            return False


def create_hub(cfg: AppConfig, clock=None, console_only: bool = False) -> NotifierHub:
    notifiers: list[Notifier] = []
    for ch in (cfg.notify or {}).get("channels") or [{"type": "console"}]:
        ch = dict(ch)
        kind = ch.pop("type", "console")
        if ch.pop("enabled", True) is False:
            continue
        common = {"levels": ch.pop("levels", None), "summary": ch.pop("summary", True)}
        try:
            if kind == "console":
                notifiers.append(ConsoleNotifier(clock=clock, **common))
            elif console_only:
                continue
            elif kind == "kakao":
                from .kakao import KakaoNotifier
                token_file = Path(ch.pop("token_file", "config/kakao_token.json"))
                if not token_file.is_absolute():
                    token_file = cfg.base_dir / token_file
                ch.pop("redirect_uri", None)
                notifiers.append(KakaoNotifier(token_file=token_file, **ch, **common))
            elif kind == "telegram":
                from .telegram import TelegramNotifier
                notifiers.append(TelegramNotifier(**ch, **common))
            elif kind == "webhook":
                from .webhook import WebhookNotifier
                notifiers.append(WebhookNotifier(**ch, **common))
            else:
                log.error("알 수 없는 알림 채널: %s", kind)
        except (TypeError, ValueError) as exc:
            log.error("알림 채널 '%s' 설정 오류 – 이 채널은 건너뜁니다: %s", kind, exc)
    return NotifierHub(notifiers)
