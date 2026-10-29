"""UVIS 웹 화면 자동 조회 (Playwright 브라우저 자동화).

사용자 본인의 UVIS 계정으로 로그인해 '실시간 위치관제' 화면을 띄워 두고,
화면이 서버에서 받아오는 위치 데이터(JSON)를 가로채거나(network 모드)
화면의 차량 목록 표를 읽어서(table 모드) 위치를 얻는다.

기본값은 2026-10 실제 UVIS(www.u-vis.com/uvis) 로 확인한 구조다 (docs/05_UVIS_실제구조_분석.md):
  로그인 #iUserid/#nPasswd/.btn-login → (비밀번호 변경 안내 '다음에 변경') → (OTP 화면 '간편 접속')
  → mainFrame.do 안의 carLocation.do 프레임이 carLocationVehicleList.ajax 로 전체 차량 위치를 받는다.
⚠ 화면 개편 시 달라질 수 있으므로 'python -m uvis_monitor web-probe' 로 구조를 다시 확인한다.
⚠ 회사 보안정책과 에스원 이용약관을 확인하고, 조회 주기는 60초 이상을 권장한다.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import datetime
from pathlib import Path

from ..models import VehiclePosition
from .base import PositionSource
from .mapping import get_path, records_to_positions

log = logging.getLogger(__name__)

DEFAULT_SELECTORS = {"username": "#iUserid", "password": "#nPasswd", "submit": ".btn-login", "message": "#loginMsg"}
# 로그인 직후 거치는 안내 화면. url_contains 화면이면 click 을 누르고, accept_dialog 문구가 든 확인창은 수락한다.
DEFAULT_AFTER_LOGIN = [
    {"url_contains": "passResetMonth", "click": "button:has-text('다음에 변경')"},  # 비밀번호 주기 변경 안내
    {"url_contains": "noMask", "click": "#btnCancel", "accept_dialog": "간편 접속"},  # OTP 인증 대신 간편 접속
]


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright 가 필요합니다: pip install playwright && python -m playwright install chromium") from exc
    return sync_playwright


class UvisWebSource(PositionSource):
    name = "uvis_web"

    def __init__(self, cfg: dict, state_dir: Path):
        self.cfg = cfg
        self.login_url = cfg.get("login_url") or cfg.get("monitor_url")
        self.monitor_url = cfg.get("monitor_url") or self.login_url
        if not self.monitor_url:
            raise ValueError("source.uvis_web.monitor_url(실시간 위치관제 화면 주소)이 필요합니다.")
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.sel = {**DEFAULT_SELECTORS, **(cfg.get("selectors") or {})}
        self.after_login: list[dict] = cfg.get("after_login", DEFAULT_AFTER_LOGIN) or []
        self.frame_hint = cfg.get("frame_url_contains", "")  # 데이터·표가 들어 있는 프레임 주소 일부
        self.mode = cfg.get("mode", "network")
        self.net = cfg.get("network") or {}
        self.table = cfg.get("table") or {}
        self.refresh = cfg.get("refresh", "reload")
        self.headless = bool(cfg.get("headless", True))
        self.wait_sec = float(cfg.get("wait_sec", 20))
        self.state_file = state_dir / "uvis_web_state.json"  # 로그인 쿠키 (재로그인 최소화)
        self._pw = self._browser = self._context = self.page = None
        self._captured: list = []
        self._seq = 0
        self._failed_nav: str | None = None

    # ------------------------------------------------------------ 브라우저
    def _start(self) -> None:
        sync_playwright = _require_playwright()
        self._pw = sync_playwright().start()
        launch = {"headless": self.headless}
        if self.cfg.get("executable_path"):
            launch["executable_path"] = self.cfg["executable_path"]
        self._browser = self._pw.chromium.launch(**launch)
        ctx = {"locale": "ko-KR", "timezone_id": "Asia/Seoul"}
        if self.state_file.is_file():
            ctx["storage_state"] = str(self.state_file)
        self._context = self._browser.new_context(**ctx)
        self.page = self._context.new_page()
        self.page.on("response", self._on_response)  # 프레임 안에서 오는 응답도 포함
        self.page.on("dialog", self._on_dialog)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.goto(self.monitor_url, wait_until="domcontentloaded")
        self._recover_http()
        self._login_if_needed()

    def _on_request_failed(self, req) -> None:
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            self._failed_nav = req.url

    def _recover_http(self) -> None:
        """실제 UVIS 는 세션 만료·간편 접속 때 http:// 주소로 돌려보낸다. http 가 막힌 환경(사내망·클라우드)에서
        그 이동이 실패하면 같은 주소를 https 로 다시 연다."""
        failed, self._failed_nav = self._failed_nav, None
        if failed and failed.startswith("http://") and self.page.url.startswith("chrome-error"):
            log.info("http 이동 실패 → https 로 다시 열기: %s", failed)
            self.page.goto("https://" + failed[len("http://"):], wait_until="domcontentloaded")

    def _on_dialog(self, dialog) -> None:
        accept = any(s.get("accept_dialog") and s["accept_dialog"] in dialog.message for s in self.after_login)
        log.info("UVIS 대화상자 %s: %s", "수락" if accept else "닫음", dialog.message[:80])
        try:
            dialog.accept() if accept else dialog.dismiss()
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        pattern = self.net.get("url_contains")
        if pattern and pattern in resp.url:
            self._seq += 1
            self._captured.append((self._seq, resp))
            del self._captured[:-5]

    def _on_login_page(self) -> bool:
        page = self.page
        return all(page.locator(self.sel[k]).count() > 0 and page.locator(self.sel[k]).first.is_visible()
                   for k in ("username", "password"))

    def _settle(self, before_url: str) -> None:
        """클릭 후 화면이 바뀌고 안정될 때까지 기다린다 (안 바뀌면 그대로 둔다: 로그인 실패 등)."""
        timeout = self.wait_sec * 1000
        try:
            self.page.wait_for_url(lambda u: u != before_url, timeout=timeout)
        except Exception:
            pass
        self._recover_http()
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    def _pass_after_login_pages(self) -> None:
        for _ in range(len(self.after_login) + 1):
            step = next((s for s in self.after_login if s.get("url_contains") and s["url_contains"] in self.page.url), None)
            if not step:
                return
            log.info("로그인 후 안내 화면 통과: %s → %s", step["url_contains"], step.get("click"))
            before = self.page.url
            try:
                self.page.click(step["click"], timeout=self.wait_sec * 1000)
            except Exception as exc:
                raise RuntimeError(f"로그인 후 안내 화면({step['url_contains']}) 처리 실패: {exc}") from exc
            self._settle(before)

    def _login_if_needed(self) -> None:
        page = self.page
        self._pass_after_login_pages()
        if not self._on_login_page():
            return
        if not self.username or not self.password:
            raise RuntimeError("UVIS 로그인이 필요하지만 UVIS_ID/UVIS_PW 환경변수(.env)가 비어 있습니다.")
        log.info("UVIS 로그인 시도")
        page.fill(self.sel["username"], self.username)
        page.fill(self.sel["password"], self.password)
        before = page.url
        page.click(self.sel["submit"])
        self._settle(before)
        self._pass_after_login_pages()
        if self._on_login_page():
            msg = ""
            if self.sel.get("message") and page.locator(self.sel["message"]).count():
                msg = " (" + " ".join(page.locator(self.sel["message"]).first.inner_text().split())[:120] + ")"
            raise RuntimeError(f"UVIS 로그인 실패{msg} – 아이디/비밀번호 또는 selectors 설정을 확인하세요.")
        # 실제 UVIS 는 간편 접속 뒤 http:// 주소로 보내는데, 그 주소가 막힌 환경도 있어 감시 화면을 직접 다시 연다
        if page.url.split("?")[0].rstrip("/") != self.monitor_url.split("?")[0].rstrip("/"):
            page.goto(self.monitor_url, wait_until="domcontentloaded")
            self._recover_http()
            try:
                page.wait_for_load_state("networkidle", timeout=self.wait_sec * 1000)
            except Exception:
                pass
        if self._on_login_page():
            raise RuntimeError("UVIS 로그인 후에도 로그인 화면으로 돌아옵니다 – 로그인 후 안내 화면(after_login) 설정을 확인하세요.")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.state_file))

    def _target(self):
        """데이터·표가 있는 프레임 (frame_url_contains 미지정 시 페이지 자체)."""
        if not self.frame_hint:
            return self.page
        for fr in self.page.frames:
            if self.frame_hint in fr.url:
                return fr
        raise RuntimeError(f"'{self.frame_hint}' 프레임을 찾지 못했습니다 (감시 화면 주소/구성을 확인하세요).")

    def _do_refresh(self) -> None:
        if self.refresh == "reload":
            self.page.reload(wait_until="domcontentloaded")
        elif self.refresh.startswith("click:"):
            self._target().click(self.refresh[len("click:"):])
        elif self.refresh.startswith("js:"):  # 화면 자체의 새로고침 함수 호출 (UVIS: getDataLoad)
            self._login_if_needed()
            self._target().evaluate("() => {" + self.refresh[len("js:"):] + "\n}")
            return
        # "none": 화면이 스스로 주기적으로 갱신하는 경우 기다리기만 한다
        self._login_if_needed()

    # ------------------------------------------------------------ 수집
    def fetch(self, now: datetime) -> list[VehiclePosition]:
        for attempt in (1, 2):
            try:
                if self.page is None:
                    self._start()
                return self._fetch_network(now) if self.mode == "network" else self._fetch_table(now)
            except Exception as exc:
                log.warning("UVIS 웹 조회 실패(%d회차): %s", attempt, exc)
                self.close()
                if attempt == 2:
                    raise
        return []

    def _fetch_network(self, now: datetime) -> list[VehiclePosition]:
        if not self.net.get("url_contains"):
            raise ValueError("source.uvis_web.network.url_contains 가 필요합니다 (web-probe 결과 참고).")
        seq0 = self._seq
        self._do_refresh()
        deadline = _time.monotonic() + self.wait_sec
        while self._seq == seq0 and _time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        if self._seq == seq0:
            raise TimeoutError(f"{self.wait_sec:.0f}초 안에 '{self.net['url_contains']}' 응답이 오지 않았습니다.")
        resp = self._captured[-1][1]
        try:
            data = resp.json()
        except Exception:
            data = json.loads(resp.text())
        records = get_path(data, self.net.get("records_path"))
        if isinstance(records, dict):
            records = [records]
        return records_to_positions(records or [], self.net.get("fields") or {},
                                    self.net.get("ts_format", "auto"), service_date=now.date())

    def _fetch_table(self, now: datetime) -> list[VehiclePosition]:
        row_sel = self.table.get("row_selector")
        cols: dict = self.table.get("columns") or {}
        if not row_sel or not cols:
            raise ValueError("source.uvis_web.table.row_selector / columns 가 필요합니다.")
        self._do_refresh()
        target = self._target()
        target.wait_for_selector(row_sel, timeout=self.wait_sec * 1000)
        rows = target.eval_on_selector_all(
            row_sel, "rows => rows.map(r => Array.from(r.querySelectorAll('td,th')).map(c => c.innerText.trim()))")
        records = [{f: (r[i] if i < len(r) else None) for f, i in cols.items()} for r in rows]
        return records_to_positions(records, {f: f for f in cols}, self.table.get("ts_format", "auto"),
                                    service_date=now.date())

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self.page = None
        self._captured.clear()


def web_probe(url: str, out_dir: Path, minutes: float = 10, executable_path: str | None = None) -> Path:
    """본인 PC 에서 브라우저를 띄워 사용자가 직접 로그인·화면 이동하는 동안
    서버와 주고받는 JSON 데이터를 기록한다. 비밀번호는 기록하지 않는다.
    결과(out_dir/summary.md)를 보고 network.url_contains / fields 를 정한다.
    """
    sync_playwright = _require_playwright()
    out_dir.mkdir(parents=True, exist_ok=True)
    hits: list[dict] = []
    geo_keys = ("lat", "lng", "lon", "위도", "경도", "latitude", "longitude", "xpos", "ypos", "gpsx", "gpsy")

    def on_response(resp):
        ctype = (resp.headers or {}).get("content-type", "")
        if not any(t in ctype for t in ("json", "javascript", "text/plain")):
            return
        try:
            body = resp.text()
        except Exception:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        idx = len(hits) + 1
        flat = json.dumps(data, ensure_ascii=False)
        found = sorted({k for k in geo_keys if f'"{k}' in flat.lower()})
        req = resp.request
        post = req.post_data or ""
        for secret in ("pw", "pass", "pwd", "password", "비밀번호"):
            if secret in post.lower():
                post = "(로그인 요청 – 내용 기록 안 함)"
                break
        fname = out_dir / f"{idx:03d}.json"
        fname.write_text(json.dumps({"url": resp.url, "method": req.method, "status": resp.status,
                                     "request_body": post, "body": data}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        hits.append({"idx": idx, "url": resp.url, "method": req.method, "geo": found, "size": len(body)})

    with sync_playwright() as pw:
        launch = {"headless": False}
        if executable_path:
            launch["executable_path"] = executable_path
        browser = pw.chromium.launch(**launch)
        page = browser.new_context(locale="ko-KR").new_page()
        page.on("response", on_response)
        page.goto(url)
        print(f"\n브라우저에서 직접 로그인한 뒤 '실시간 위치관제/차량위치' 화면을 열어 1~2분 그대로 두세요.\n"
              f"최대 {minutes:.0f}분 기록하며, 브라우저 창을 닫으면 바로 끝납니다.\n")
        deadline = _time.monotonic() + minutes * 60
        try:
            while _time.monotonic() < deadline and browser.is_connected() and not page.is_closed():
                page.wait_for_timeout(1000)
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    summary = out_dir / "summary.md"
    lines = ["# UVIS web-probe 결과", "", "좌표로 보이는 키(lat/lng/위도/경도 등)가 들어 있는 응답이 위치 데이터 후보입니다.", "",
             "| 번호 | 방식 | 좌표키 | 크기 | URL |", "|---|---|---|---|---|"]
    for h in sorted(hits, key=lambda h: (not h["geo"], h["idx"])):
        lines.append(f"| {h['idx']:03d} | {h['method']} | {', '.join(h['geo']) or '-'} | {h['size']} | {h['url']} |")
    lines += ["", "다음 단계: 후보 응답 파일(번호.json)을 열어 차량번호·시각·위도·경도 필드 이름을 확인하고",
              "config.yaml 의 source.uvis_web.network 에 url_contains / records_path / fields 로 적습니다."]
    summary.write_text("\n".join(lines), encoding="utf-8")
    return summary
