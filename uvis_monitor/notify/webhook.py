"""웹훅 알림 — 슬랙 / 디스코드 / MS Teams(워크플로) / 기타(JSON {"text": ...})."""
from __future__ import annotations

import logging

import requests

from .base import Notifier, split_text

log = logging.getLogger(__name__)
LIMITS = {"slack": 3500, "discord": 1900, "teams": 3500, "generic": 3500}


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, url: str, format: str = "slack", **kw):  # noqa: A002
        super().__init__(**kw)
        if not url:
            raise ValueError("webhook.url 이 비어 있습니다.")
        if format not in LIMITS:
            raise ValueError(f"webhook.format 은 {', '.join(LIMITS)} 중 하나여야 합니다.")
        self.url, self.format = url, format
        self.name = f"webhook:{format}"

    def _payload(self, text: str) -> dict:
        if self.format == "discord":
            return {"content": text}
        if self.format == "teams":
            return {"type": "message", "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {"type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "body": [{"type": "TextBlock", "text": text.replace("\n", "\n\n"), "wrap": True}]}}]}
        return {"text": text}

    def send_text(self, text: str) -> bool:
        ok = True
        for chunk in split_text(text, LIMITS[self.format]):
            try:
                resp = requests.post(self.url, json=self._payload(chunk), timeout=10)
                if resp.status_code >= 300:
                    log.error("웹훅(%s) 실패 %s: %s", self.format, resp.status_code, resp.text[:200])
                    ok = False
            except requests.RequestException as exc:
                log.error("웹훅(%s) 오류: %s", self.format, exc)
                ok = False
        return ok
