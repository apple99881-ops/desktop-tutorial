"""시험용 가짜 UVIS 사이트 (로그인 → 위치관제 화면 → 위치 JSON).

실제 UVIS 구조를 흉내 낸 것일 뿐이며, uvis_web 소스의 로그인·데이터 가로채기·표 읽기
동작을 검증하는 데 쓴다.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

LOGIN_HTML = """<html><meta charset="utf-8"><body>
<form method="post" action="/login">
  <input id="userId" name="userId"><input id="userPw" name="userPw" type="password">
  <button type="submit">로그인</button>
</form></body></html>"""

MONITOR_HTML = """<html><meta charset="utf-8"><body>
<h1>실시간 위치관제</h1>
<button id="btnRefresh" onclick="load()">새로고침</button>
<table id="carList"><thead><tr><th>차량</th><th>수신일시</th><th>위도</th><th>경도</th></tr></thead><tbody></tbody></table>
<script>
async function load() {
  const res = await fetch('/api/vehicle/location.do', {method: 'POST'});
  const js = await res.json();
  const tb = document.querySelector('#carList tbody');
  tb.innerHTML = js.data.list.map(v =>
    `<tr><td>${v.carNo}</td><td>${v.gpsDt}</td><td>${v.lat}</td><td>${v.lng}</td></tr>`).join('');
}
load();
</script></body></html>"""


class MockUvis:
    USER, PASSWORD = "tester", "secret"

    def __init__(self):
        self.vehicles = [
            {"carNo": "경기70아1001", "gpsDt": "20261029063000", "lat": "37266400", "lng": "127000100", "spd": 0},
            {"carNo": "경기70아1002", "gpsDt": "20261029063010", "lat": "37200100", "lng": "127096000", "spd": 12},
        ]
        self.login_count = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return "SESSION=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/monitor"):
                    if not self._authed():
                        return self._send(302, "", headers={"Location": "/login"})
                    return self._send(200, MONITOR_HTML)
                return self._send(200, LOGIN_HTML)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                if self.path == "/login":
                    form = parse_qs(body)
                    if form.get("userId") == [outer.USER] and form.get("userPw") == [outer.PASSWORD]:
                        outer.login_count += 1
                        return self._send(302, "", headers={"Location": "/monitor",
                                                            "Set-Cookie": "SESSION=ok; Path=/"})
                    return self._send(200, LOGIN_HTML)
                if self.path.startswith("/api/vehicle/location"):
                    if not self._authed():
                        return self._send(401, "{}", "application/json")
                    return self._send(200, json.dumps({"result": "ok", "data": {"list": outer.vehicles}},
                                                      ensure_ascii=False), "application/json; charset=utf-8")
                return self._send(404, "")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
