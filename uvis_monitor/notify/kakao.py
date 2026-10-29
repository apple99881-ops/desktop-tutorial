"""카카오톡 '나에게 보내기' (카카오 메시지 API).

준비 (docs/03_메신저_설정.md 참고):
 1. developers.kakao.com → 내 애플리케이션 추가 → REST API 키 확인
 2. 카카오 로그인 활성화 + Redirect URI 등록 (예: http://localhost:8765/callback)
 3. 동의항목에서 '카카오톡 메시지 전송(talk_message)' 사용 설정
 4. python -m uvis_monitor kakao-auth  → 토큰 파일 생성 (이후 자동 갱신)

제약: 기본 텍스트 템플릿은 메시지당 약 200자 → 긴 메시지는 자동 분할.
      파일(엑셀) 첨부는 불가 → 보고서는 텔레그램/메일/공유폴더로 받는다.
"""
from __future__ import annotations

import http.server
import json
import logging
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

from .base import Notifier, split_text

log = logging.getLogger(__name__)

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


class KakaoAuthError(RuntimeError):
    pass


class KakaoNotifier(Notifier):
    name = "kakao"

    def __init__(self, rest_api_key: str, token_file: str | Path, client_secret: str = "",
                 link_url: str = "https://www.s1uvis.co.kr", button_title: str = "UVIS 열기",
                 max_len: int = 200, **kw):
        super().__init__(**kw)
        if not rest_api_key:
            raise ValueError("kakao.rest_api_key(KAKAO_REST_API_KEY)가 비어 있습니다.")
        self.key, self.secret = rest_api_key, client_secret
        self.token_file = Path(token_file)
        self.link_url, self.button_title, self.max_len = link_url, button_title, max_len
        self.session = requests.Session()

    # ------------------------------------------------------------ 토큰
    def _load(self) -> dict:
        if not self.token_file.is_file():
            raise KakaoAuthError(f"카카오 토큰 파일이 없습니다: {self.token_file}\n"
                                 f"  → python -m uvis_monitor kakao-auth 를 먼저 실행하세요.")
        return json.loads(self.token_file.read_text(encoding="utf-8"))

    def _save(self, tok: dict) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh(self, tok: dict) -> dict:
        data = {"grant_type": "refresh_token", "client_id": self.key, "refresh_token": tok["refresh_token"]}
        if self.secret:
            data["client_secret"] = self.secret
        resp = self.session.post(TOKEN_URL, data=data, timeout=10)
        if resp.status_code != 200:
            raise KakaoAuthError(f"카카오 토큰 갱신 실패({resp.status_code}): {resp.text[:200]}\n"
                                 f"  → python -m uvis_monitor kakao-auth 로 다시 인증하세요.")
        tok = {**tok, **_token_fields(resp.json())}
        self._save(tok)
        log.info("카카오 액세스 토큰 갱신 완료")
        return tok

    def _access_token(self, force: bool = False) -> str:
        tok = self._load()
        if force or time.time() >= tok.get("expires_at", 0) - 120:
            tok = self._refresh(tok)
        left = tok.get("refresh_token_expires_at", 0) - time.time()
        if 0 < left < 7 * 86400:
            log.warning("카카오 리프레시 토큰 만료 %.0f일 전 – kakao-auth 재실행 권장", left / 86400)
        return tok["access_token"]

    def refresh_days_left(self) -> float | None:
        try:
            tok = self._load()
        except KakaoAuthError:
            return None
        exp = tok.get("refresh_token_expires_at")
        return (exp - time.time()) / 86400 if exp else None

    # ------------------------------------------------------------ 발송
    def _post(self, text: str, token: str) -> requests.Response:
        template = {"object_type": "text", "text": text,
                    "link": {"web_url": self.link_url, "mobile_web_url": self.link_url},
                    "button_title": self.button_title}
        return self.session.post(SEND_URL, headers={"Authorization": f"Bearer {token}"},
                                 data={"template_object": json.dumps(template, ensure_ascii=False)}, timeout=10)

    def send_text(self, text: str) -> bool:
        ok = True
        try:
            token = self._access_token()
            for chunk in split_text(text, self.max_len):
                resp = self._post(chunk, token)
                if resp.status_code == 401:
                    token = self._access_token(force=True)
                    resp = self._post(chunk, token)
                if resp.status_code != 200 or resp.json().get("result_code") != 0:
                    log.error("카카오톡 발송 실패(%s): %s", resp.status_code, resp.text[:300])
                    ok = False
        except (KakaoAuthError, requests.RequestException) as exc:
            log.error("카카오톡 발송 오류: %s", exc)
            return False
        return ok


def _token_fields(data: dict) -> dict:
    now = time.time()
    out = {"access_token": data["access_token"], "expires_at": now + int(data.get("expires_in", 21599))}
    if data.get("refresh_token"):
        out["refresh_token"] = data["refresh_token"]
        out["refresh_token_expires_at"] = now + int(data.get("refresh_token_expires_in", 5183999))
    return out


def authorize(rest_api_key: str, redirect_uri: str, token_file: str | Path, client_secret: str = "",
              timeout_sec: int = 300) -> Path:
    """kakao-auth 명령: 브라우저로 카카오 로그인 → 인가코드 → 토큰 저장."""
    url = (f"{AUTH_URL}?response_type=code&client_id={rest_api_key}"
           f"&redirect_uri={quote(redirect_uri, safe='')}&scope=talk_message")
    parsed = urlparse(redirect_uri)
    code: dict[str, str] = {}

    if parsed.hostname in ("localhost", "127.0.0.1") and parsed.port:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                if "code" in qs:
                    code["code"] = qs["code"][0]
                    msg = "카카오 인증 완료! 이 창을 닫고 프로그램으로 돌아가세요."
                else:
                    msg = f"인증 실패: {qs.get('error_description', qs)}"
                body = f"<html><meta charset='utf-8'><body><h2>{msg}</h2></body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer((parsed.hostname, parsed.port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"\n브라우저에서 카카오 로그인 후 '동의하고 계속하기'를 누르세요.\n(창이 안 열리면 주소 복사: {url})\n")
        webbrowser.open(url)
        deadline = time.time() + timeout_sec
        while "code" not in code and time.time() < deadline:
            time.sleep(0.5)
        server.shutdown()
    if "code" not in code:
        print(f"\n아래 주소를 브라우저에 열고, 로그인 후 이동된 주소창 전체를 복사해 붙여넣으세요.\n{url}\n")
        pasted = input("이동된 주소(또는 code 값): ").strip()
        code["code"] = parse_qs(urlparse(pasted).query).get("code", [pasted])[0]

    data = {"grant_type": "authorization_code", "client_id": rest_api_key,
            "redirect_uri": redirect_uri, "code": code["code"]}
    if client_secret:
        data["client_secret"] = client_secret
    resp = requests.post(TOKEN_URL, data=data, timeout=10)
    if resp.status_code != 200:
        raise KakaoAuthError(f"토큰 발급 실패({resp.status_code}): {resp.text[:300]}")
    tok = _token_fields(resp.json())
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
