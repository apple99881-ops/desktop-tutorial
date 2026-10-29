"""텔레그램 봇 알림 — 설정이 가장 쉽고 엑셀 파일 전송·명령(/현황) 응답까지 가능.

준비: 텔레그램에서 @BotFather → /newbot → 봇 토큰 발급 → 내 봇에게 아무 말이나 보낸 뒤
      python -m uvis_monitor telegram-chatid  → chat_id 확인
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

import requests

from ..models import Alert
from .base import Notifier, map_link, split_text

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str | int, **kw):
        super().__init__(**kw)
        if not bot_token or not chat_id:
            raise ValueError("telegram.bot_token / chat_id(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)가 비어 있습니다.")
        self.token, self.chat_id = bot_token, str(chat_id)
        self.session = requests.Session()
        self._offset: int | None = None

    def _call(self, method: str, **kw) -> dict | None:
        try:
            resp = self.session.post(API.format(token=self.token, method=method), timeout=30, **kw)
            data = resp.json()
            if not data.get("ok"):
                log.error("텔레그램 %s 실패: %s", method, data.get("description"))
                return None
            return data
        except (requests.RequestException, ValueError) as exc:
            log.error("텔레그램 %s 오류: %s", method, exc)
            return None

    def send(self, alert: Alert) -> bool:
        lines = alert.text().split("\n")
        body = f"<b>{html.escape(lines[0])}</b>" + "".join(f"\n{html.escape(x)}" for x in lines[1:])
        link = map_link(alert)
        if link:
            body += f'\n<a href="{html.escape(link)}">📍 지도에서 위치 보기</a>'
        return self._call("sendMessage", json={"chat_id": self.chat_id, "text": body, "parse_mode": "HTML",
                                               "disable_web_page_preview": True}) is not None

    def send_text(self, text: str) -> bool:
        ok = True
        for chunk in split_text(text, 4000):
            ok &= self._call("sendMessage", json={"chat_id": self.chat_id, "text": chunk,
                                                  "disable_web_page_preview": True}) is not None
        return ok

    def send_file(self, path: Path, caption: str = "") -> bool:
        with open(path, "rb") as fh:
            return self._call("sendDocument", data={"chat_id": self.chat_id, "caption": caption[:1000]},
                              files={"document": (Path(path).name, fh)}) is not None

    def poll_commands(self) -> list[str]:
        """내 채팅방에서 온 명령(/현황, /보고서 등)만 가져온다."""
        params = {"timeout": 0, "allowed_updates": '["message"]'}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            data = self.session.get(API.format(token=self.token, method="getUpdates"), params=params, timeout=15).json()
        except (requests.RequestException, ValueError):
            return []
        cmds = []
        for upd in data.get("result", []):
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == self.chat_id and msg.get("text", "").startswith("/"):
                cmds.append(msg["text"].strip())
        return cmds


def find_chat_ids(bot_token: str) -> list[tuple[str, str]]:
    data = requests.get(API.format(token=bot_token, method="getUpdates"), timeout=15).json()
    if not data.get("ok"):
        raise RuntimeError(f"텔레그램 오류: {data.get('description')}")
    out = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            out[str(chat["id"])] = name or chat.get("username", "")
    return list(out.items())
