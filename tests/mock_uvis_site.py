"""시험용 가짜 UVIS 사이트 – 2026-10 실제 UVIS(www.u-vis.com/uvis) 의 흐름과 데이터 형식을 흉내 낸다.

  /uvis/login.do  ──(AJAX loginProc.ajax)──▶ passResetMonth.do('다음에 변경')
      ──▶ noMask.do('간편 접속' + 확인창) ──▶ login.do ──302──▶ mainFrame.do
  mainFrame.do 안의 iframe carLocation.do 가 getDataLoad() 로 carLocationVehicleList.ajax 를 부른다.
  지도 SDK 가 막힌 환경처럼 첫 조회를 스스로 하지 않으므로 refresh: "js:getDataLoad(...)" 가 필요하다.
실제 구조는 docs/05_UVIS_실제구조_분석.md 참고. uvis_web 소스의 로그인·프레임·가로채기·표 읽기 검증용.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGIN_HTML = """<html><meta charset="utf-8"><body>
<form id="loginForm" onsubmit="return false;">
  <input type="text" id="iUserid" name="iUserid"><input type="password" id="nPasswd" name="nPasswd">
  <button type="button" class="btn-login">로그인</button>
</form><p id="loginMsg"></p>
<form id="hrefForm" method="post"><input type="hidden" name="formParams"></form>
<script>
document.querySelector('.btn-login').onclick = async () => {
  const res = await fetch('/uvis/loginProc.ajax', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({iUserid: iUserid.value, nPasswd: nPasswd.value})});
  const data = await res.json();
  if (data.result) { hrefForm.action = '/uvis' + data.params.url; hrefForm.submit(); }
  else { loginMsg.textContent = data.resultMap.errMsg; }
};
</script></body></html>"""

PASS_RESET_HTML = """<html><meta charset="utf-8"><body>
<input type="password" id="oPasswd"><input type="password" id="nPasswd"><input type="password" id="nPasswd2">
<button onclick="location.href='/uvis/noMask.do'">다음에 변경</button><button>비밀번호 변경</button>
</body></html>"""

NO_MASK_HTML = """<html><meta charset="utf-8"><body>
<h2>간편 접속</h2><button id="btnCancel" onclick="if (confirm('간편 접속하시겠습니까?')) quick();">확인</button>
<h2>OTP 인증 접속</h2><input type="text" id="iUserid" value="tester" readonly><input id="phoneNumber">
<script>
async function quick() {  // 실제 UVIS 는 서버 세션에 기록 – 여기서는 별도 요청으로 흉내
  await fetch('/uvis/quickAccess', {method: 'POST'});
  location.href = '/uvis/login.do';
}
</script></body></html>"""

MAIN_HTML = """<html><meta charset="utf-8"><body><h1>차량운행관리 시스템</h1>
<iframe src="/uvis/carLocation.do" width="1000" height="600"></iframe></body></html>"""

CAR_LOCATION_HTML = """<html><meta charset="utf-8"><body>
<label><input type="checkbox" id="chkUseGroup" checked>그룹별</label>
<table id="carList"><tbody></tbody></table>
<script>
function getDataLoad(gubun, opFlag, pGrpId, grpId, listInit, refreshType) {
  const useGroup = document.querySelector('#chkUseGroup').checked ? 'Y' : 'N';
  fetch('/uvis/carLocationVehicleList.ajax', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({useGroup: useGroup, carLimit: '300', addrYn: 'Y'})})
    .then(r => r.json()).then(js => {
      document.querySelector('#carList tbody').innerHTML = js.carList.map(v =>
        `<tr><td>${v.cmNumber}</td><td>${v.biDate} ${v.biTime}</td><td>${v.biXPosition}</td><td>${v.biYPosition}</td></tr>`).join('');
    });
}
</script></body></html>"""


def _car(no, group, date, time, lat, lon, speed, onoff):
    return {"cmNumber": no, "tidId": "T" + no[-4:], "grpName": group, "biDate": date, "biTime": time,
            "biXPosition": lat, "biYPosition": lon, "biGpsSpeed": speed, "biTurnOnoff": onoff,
            "biOpFlag": "13" if onoff == "ON" else "8", "dbAddress": "경기도 수원시 팔달구"}


class MockUvis:
    USER, PASSWORD = "tester", "secret"

    def __init__(self):
        self.vehicles = [
            _car("경기70아1001", "1그룹", "20261029", "063000", "37.266400", "127.000100", 0, "ON"),
            _car("경기70아1002", "1그룹", "20261029", "063010", "37.200100", "127.096000", 12, "ON"),
            _car("경기70아1003", "2그룹", "20261027", "203639", "37.268644", "126.999616", 0, "OFF"),
        ]
        self.login_count = 0
        self.sessions: dict[str, str] = {}  # 세션 → pending(로그인만) / quick(간편 접속 선택) / ok
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

            def _sid(self):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "JSESSIONID":
                        return v
                return ""

            def _state(self):
                return outer.sessions.get(self._sid(), "")

            def _json(self, obj):
                return self._send(200, json.dumps(obj, ensure_ascii=False), "application/json;charset=UTF-8")

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/uvis/login.do":
                    if self._state() in ("quick", "ok"):  # 간편 접속 → 메인으로
                        outer.sessions[self._sid()] = "ok"
                        return self._send(302, "", headers={"Location": "/uvis/mainFrame.do"})
                    return self._send(200, LOGIN_HTML)
                if path == "/uvis/noMask.do":
                    return self._send(200, NO_MASK_HTML)
                if path in ("/uvis/mainFrame.do", "/uvis/carLocation.do"):
                    if self._state() != "ok":
                        return self._send(302, "", headers={"Location": "/uvis/login.do"})
                    return self._send(200, MAIN_HTML if path.endswith("mainFrame.do") else CAR_LOCATION_HTML)
                return self._send(404, "")

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                path = self.path.split("?")[0]
                if path == "/uvis/loginProc.ajax":
                    data = json.loads(body or "{}")
                    if data.get("iUserid") == outer.USER and data.get("nPasswd") == outer.PASSWORD:
                        outer.login_count += 1
                        sid = f"S{outer.login_count}"
                        outer.sessions[sid] = "pending"
                        self.send_response(200)
                        out = json.dumps({"result": True, "resultMap": {"errCode": "01"},
                                          "params": {"url": "/passResetMonth.do"}}).encode()
                        self.send_header("Content-Type", "application/json;charset=UTF-8")
                        self.send_header("Content-Length", str(len(out)))
                        self.send_header("Set-Cookie", f"JSESSIONID={sid}; Path=/")
                        self.end_headers()
                        return self.wfile.write(out)
                    return self._json({"result": False, "resultMap": {"errMsg": "아이디 또는 비밀번호를 다시 확인하세요."}})
                if path == "/uvis/quickAccess" and self._state() == "pending":
                    outer.sessions[self._sid()] = "quick"
                    return self._json({"result": True})
                if path == "/uvis/passResetMonth.do":
                    return self._send(200, PASS_RESET_HTML)
                if path == "/uvis/carLocationVehicleList.ajax":
                    if self._state() != "ok":
                        return self._send(302, "", headers={"Location": "/uvis/login.do"})
                    use_group = json.loads(body or "{}").get("useGroup") == "Y"
                    cars = [v for v in outer.vehicles if not use_group or v["grpName"] == "1그룹"]
                    return self._json({"result": True, "errCode": "", "carList": cars,
                                       "countView": {"totalCount": len(outer.vehicles)}})
                return self._send(404, "")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
