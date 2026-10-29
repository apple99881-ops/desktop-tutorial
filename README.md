# 🚌 UVIS 통근버스 출근 운행 감시 (uvis_monitor)

에스원 **UVIS(유비스)** 차량운행관리시스템의 통근버스 위치 데이터로 **매일 아침 노선별 정시 운행을 자동 감시**하는 프로그램입니다.

| 요구사항 | 구현 |
|---|---|
| 노선별 **기점 출발** 5분 이상 지연 → 이상 | 계획+5분에 미출발이면 **즉시 알림**, 출발 시 실제 지연 확정 알림. 조기 출발도 감지 |
| 노선별 **정차지** 10분 이상 지연 → 알림 | 계획+10분에 미도착이면 **즉시 알림**, 도착 시 확정 알림. 미경유·위치 미수신도 감지 |
| 실시간 데이터를 **카톡·메신저**로 전달 | 카카오톡 '나에게 보내기', 텔레그램(엑셀 수신, `/현황` 명령), 슬랙·디스코드·팀즈 |
| 매일 이상 내용을 **엑셀로 일목요연하게** | 일일 보고서(요약 대시보드·이상발생내역·노선별상세·알림이력) + 월간 집계(히트맵) |
| UVIS **전체 분석** | [docs/01_UVIS_시스템_분석.md](docs/01_UVIS_시스템_분석.md): 구성, 기능, 데이터 모델, 판정 로직, 연동 방식, 확장 아이디어 |

## 빠른 시작
```bat
:: 1) 설치 (Windows: scripts\install.bat 더블클릭)
pip install -r requirements.txt

:: 2) 시연 – UVIS 연결 없이 가상 노선 8개로 아침 전체 재현 → reports\demo\ 에 엑셀
python -m uvis_monitor demo
python -m uvis_monitor demo --speed 120      :: 2분 만에 아침 3시간 반을 '실시간처럼' 보기

:: 3) 실제 노선 입력 후 점검
python -m uvis_monitor check

:: 4) 어제 UVIS 엑셀 다운로드로 재분석 (샘플 파일로 바로 시험 가능)
python -m uvis_monitor replay samples\샘플_UVIS_운행경로_2026-10-28.xlsx

:: 5) 매일 아침 자동 실행: scripts\register_task.bat (관리자 권한) → 평일 05:50
```

## 문서
| 문서 | 내용 |
|---|---|
| [01_UVIS_시스템_분석](docs/01_UVIS_시스템_분석.md) | UVIS 전체 분석. 앞으로 개발할 때 기준이 되는 개념서 |
| [02_데이터연동_가이드](docs/02_데이터연동_가이드.md) | 엑셀 / 웹 자동조회(web-probe) / 에스원 API 연결, 에스원 요청 문안 |
| [03_메신저_설정](docs/03_메신저_설정.md) | 카카오톡·텔레그램·슬랙 설정, 메시지 예시 |
| [04_설치_운영_가이드](docs/04_설치_운영_가이드.md) | 설치, 작업 스케줄러 등록, 매일/매달 할 일, 문제 해결 |

`samples/` 폴더에 결과물 예시가 있습니다: **일일 보고서**, **월간 집계**(한 달 시뮬레이션), **UVIS 경로조회 엑셀 형태 예시**(실제 UVIS 양식과 다를 수 있음).

## 명령어
| 명령 | 설명 |
|---|---|
| `check` | 설정·노선 점검 (좌표 오류, 반경 겹침, 비현실적 속도) |
| `demo [--speed N] [--notify]` | 시뮬레이터 시연 (출발지연·조기출발·정차지연·미경유·위치미수신·미출발 재현) |
| `run [--force --minutes N]` | 오늘 실시간 감시 1회 (작업 스케줄러용) |
| `daemon` | 매일 자동 반복 감시 |
| `replay 파일... [--date] [--vehicle] [--notify]` | UVIS 엑셀/CSV로 지난 운행 재분석 |
| `report --date YYYY-MM-DD` | 일일 보고서 다시 만들기 |
| `monthly --month YYYY-MM [--send]` | 월간 집계 보고서 |
| `test-notify` | 메신저 시험 발송 |
| `kakao-auth` / `telegram-chatid` | 메신저 최초 설정 |
| `inspect-excel 파일` | UVIS 엑셀 구조·열 자동인식 확인 |
| `web-probe [--url]` | UVIS 웹 데이터 구조 기록 (본인 PC에서 직접 로그인) |

## 동작 방식
```
 UVIS ──(웹 자동조회 / 엑셀 / API)──▶ sources ──▶ tracker(노선별 판정) ──▶ notify(카톡·텔레그램)
                                                       │
                                                       └──▶ storage(SQLite) ──▶ report(엑셀)
```
- **지오펜스**: 기점·정차지·종점을 반경 원(기본 150m)으로 보고 진입/이탈로 도착·출발을 판정
- **선분 보간**: 위치가 1분 이상 간격이어도 직전→현재 이동선이 정류장 반경을 지나면 통과 시각을 계산
- **오탐 방지**: 기점에서 300m 이상 멀어져야 출발 확정, 공차 회송 중 통과는 무시, 기준 미만으로 확인되면 알림 해제
- **재시작 복구**: 감시 중 PC가 재부팅돼도 DB의 위치로 상태를 복원하고, 같은 알림은 다시 보내지 않음

## 구조와 확장
```
uvis_monitor/
  models.py        도메인 모델 (Route, RoutePoint, VehiclePosition, Anomaly, Alert, Rules)
  tracker.py       ★ 판정 엔진 (노선 1회 운행 상태 기계)
  geo.py           거리·선분-원 교차 계산
  monitor.py       하루 감시 루프, 재시작 복구, 현황판·일일 요약
  report.py        일일/월간 엑셀 보고서
  storage.py       SQLite 저장소 (positions, point_results, anomalies, alerts, route_days)
  config.py        config.yaml / routes.yaml 로딩 (${환경변수} 치환)
  sources/         simulator · excel_export · uvis_web(Playwright) · http_api · mapping
  notify/          console · kakao · telegram · webhook
  cli.py           명령어
```
- **새 데이터 소스**: `sources/base.py`의 `PositionSource`를 상속해 `fetch(now)`를 구현하고 `sources/__init__.py`에 등록
- **새 메신저**: `notify/base.py`의 `Notifier`를 상속해 `send_text()`를 구현하고 `notify/__init__.py`에 등록
- **새 판정 규칙**: `tracker.py`의 `evaluate()`(시간 경과형) 또는 `_arrive()`(도착 시점형)에 추가하고 `AnomalyKind`에 종류 추가
- **다른 분석**: `data/uvis_monitor.db`에 모든 위치·결과가 쌓이므로 SQL·pandas로 바로 분석 가능

## 테스트
```bat
python -m unittest discover -s tests
```
판정 엔진(정상·출발지연·조기출발·정차지연·미경유·위치미수신·공차회송·드문 위치), 엑셀/CSV/HTML-xls 읽기, 카카오 토큰 갱신·분할 발송, 텔레그램, 보고서, 재시작 복구, 그리고 **가짜 UVIS 사이트를 상대로 한 실제 브라우저 자동조회**까지 37개 테스트가 있습니다.

## 주의
- UVIS는 공개 API가 확인되지 않아, 실제 연결(웹 자동조회 주소·필드 이름)은 **본인 계정으로 web-probe 후 설정**해야 합니다. 문서의 주소·필드 이름은 예시입니다.
- UVIS 계정 자동조회, 위치정보 메신저 전송이 **회사 보안정책·에스원 약관·위치정보법** 범위 안인지 확인하세요 ([분석 문서 6장](docs/01_UVIS_시스템_분석.md#6-운영상-주의점)).
- `.env`(비밀번호·토큰), `data/`(운행 데이터)는 외부에 공유하지 마세요.
