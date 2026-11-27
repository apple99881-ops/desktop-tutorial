#!/usr/bin/env python3
"""공모주 당일매도 보드 자동 갱신.

38커뮤니케이션(일정·수요예측 결과·종목 상세)과 네이버 차트(지수)를 읽어
data/base.json(고정 통계) + template.html 로 index.html 을 다시 만든다.
수집이 부족하면 index.html 을 건드리지 않고 0 이 아닌 코드로 끝난다.

외부 패키지 없이 표준 라이브러리만 쓴다.
  python3 scripts/update.py            # 실제 수집
  IPO_FIXTURE_DIR=dir python3 ...      # 저장해 둔 페이지로 시험
  IPO_TODAY=2026-11-17                 # 기준일 고정(시험용)
"""
import datetime as dt
import html
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = dt.timezone(dt.timedelta(hours=9))
BASE38 = "https://www.38.co.kr/html/fund/index.htm"
FCHART = "https://fchart.stock.naver.com/sise.nhn?symbol={}&timeframe=day&count=300&requestType=0"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
FIX = os.environ.get("IPO_FIXTURE_DIR")
# 일반청약 경쟁률 추정식(기존 모형 계수, 절편은 기존 산출값에서 역산)
RC = {"b0": -1.5432, "log_comp": 1.167, "lockup": 0.012, "hot5": 0.492, "log_amt": -0.029}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 수집
def fixture_name(url):
    m = re.search(r"dart\.fss\.or\.kr/dsac001/mainG\.do\?selectDate=([\d.]+)&currentPage=(\d+)", url)
    if m:
        return f"dart_{m.group(1)}_{m.group(2)}.html"
    m = re.search(r"symbol=(\w+)", url)
    if m:
        return f"naver_fchart_{m.group(1).lower()}.xml"
    q = dict(re.findall(r"[?&](\w+)=([^&]*)", url))
    if q.get("o") == "v":
        return f"38_v_{q['no']}.html"
    p = q.get("page", "1")
    return f"38_{q['o']}{'' if p == '1' else '_p' + p}.html"


def fetch(url, enc="euc-kr"):
    if FIX:
        path = os.path.join(FIX, fixture_name(url))
        return open(path, "rb").read().decode(enc, "replace") if os.path.exists(path) else None
    ctx = ssl.create_default_context()
    if "38.co.kr" in url:
        ctx.set_ciphers("DEFAULT:!DH:!DHE")  # 서버의 약한 DH 키를 피한다(인증서 검증은 유지)
    err = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=ctx) as r:
                return r.read().decode(enc, "replace")
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 + 3 * i)
    log("수집 실패:", url, repr(err))
    return None


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def table_rows(doc, keep_empty):
    doc = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", doc, flags=re.S | re.I)
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", doc, re.S | re.I):
        if "<table" in tr.lower():
            continue
        cells = [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if not keep_empty:
            cells = [c for c in cells if c]
        if any(cells):
            m = re.search(r"o=v&(?:amp;)?no=(\d+)", tr)
            out.append((m.group(1) if m else None, cells))
    return out


def nums(s):
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", s or "")]


def num(s):
    n = nums(s)
    return n[0] if n else None


def pdate(s):
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s or "")
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def prange(s):
    """'2026.11.05 ~ 2026.11.06' 또는 '2026.12.03~12.09' → (시작, 끝)"""
    a = pdate(s)
    if not a:
        return None, None
    rest = (s or "").split("~", 1)[1] if "~" in (s or "") else ""
    b = pdate(rest)
    if not b:
        m = re.search(r"(\d{1,2})\.(\d{1,2})", rest)
        if m:
            b = dt.date(a.year + (1 if int(m.group(1)) < a.month else 0), int(m.group(1)), int(m.group(2)))
    return a, b or a


def dstr(d):
    return d.strftime("%Y.%m.%d") if d else None


def list_rows(kind, pages):
    rows = []
    for p in pages:
        doc = fetch(f"{BASE38}?o={kind}" + (f"&page={p}" if p > 1 else ""))
        if not doc:
            continue
        rows += [(no, c) for no, c in table_rows(doc, True) if no and len(c) >= 6 and pdate(c[1])]
    return rows


# ---------------------------------------------------------------- 종목 상세
def parse_detail(no, doc):
    R = [c for _, c in table_rows(doc, False)]

    def get(label):
        for c in R:
            if label in c:
                i = c.index(label)
                return c[i + 1] if i + 1 < len(c) else None
        return None

    def starts(label):
        for c in R:
            if c[0] == label:
                return c
        return None

    it = {"no": no, "name": get("종목명"), "market": get("시장구분"), "code": get("종목코드"), "sector": get("업종")}
    if not it["name"]:
        return None
    it["spac"] = "스팩" in it["name"]
    it["sales_mn"] = num(get("매출액"))
    it["net_income_mn"] = num(get("순이익"))
    it["total_shares_offer"] = num(get("총공모주식수"))
    off = get("상장공모") or ""
    m = re.search(r"신주모집\s*:\s*([\d,]+)", off)
    it["new_shares"] = num(m.group(1)) if m else None
    m = re.search(r"구주매출\s*:\s*([\d,]+)", off)
    it["old_shares"] = num(m.group(1)) if m else None
    band = nums(get("희망공모가액"))
    it["band_lo"], it["band_hi"] = (band + [None, None])[:2] if len(band) != 1 else (band[0], band[0])
    it["offer"] = num(get("확정공모가"))
    it["amount_mn"] = num(get("공모금액"))
    comp = get("청약경쟁률") or ""
    m = re.search(r"([\d,.]+)\s*:\s*1", comp)
    it["retail_comp"] = num(m.group(1)) if m else None
    m = re.search(r"비례\s*([\d,.]+)", comp)
    it["prop_comp"] = num(m.group(1)) if m else None

    # 증권사별 물량
    uw = []
    for i, c in enumerate(R):
        if c[0] == "인수회사":
            for c2 in R[i + 1:]:
                if len(c2) < 3 or "주" not in c2[1]:
                    break
                sh, lim = nums(c2[1]), nums(c2[2])
                if sh:
                    uw.append({"broker": c2[0], "shares_lo": sh[0], "shares_hi": sh[-1],
                               "limit_lo": lim[0] if lim else None, "limit_hi": lim[-1] if lim else None,
                               "role": c2[3] if len(c2) > 3 else "인수"})
            break
    if not uw:
        c = starts("주간사")
        if c and len(c) >= 3:
            a, _, b = c[2].partition("청약한도")
            sh, lim = nums(a), nums(b)
            if sh:
                uw.append({"broker": c[1].split(",")[0].strip(), "shares_lo": sh[0], "shares_hi": sh[-1],
                           "limit_lo": lim[0] if lim else None, "limit_hi": lim[-1] if lim else None, "role": "대표주관"})
    it["underwriters"] = uw

    fs, fe = prange(get("수요예측일"))
    ss, se = prange(get("공모청약일"))
    it.update(fcst_start=dstr(fs), fcst_end=dstr(fe), sub_start=dstr(ss), sub_end=dstr(se),
              refund=dstr(pdate(get("환불일"))), listing=dstr(pdate(get("상장일"))))

    it["margin_rate"] = 100.0 if it["spac"] else 50.0
    for c in R:
        if "일반청약자" in c:
            i = c.index("일반청약자")
            sh = nums(re.sub(r"\(.*?\)", "", c[i + 1])) if i + 1 < len(c) else []
            it["retail_shares"] = sh[0] if sh else None
            it["retail_shares_hi"] = sh[-1] if len(sh) > 1 else None
            for x in c:
                if "청약증거금율" in x and num(x):
                    it["margin_rate"] = num(x)
            break

    # 수요예측 결과
    lk = {}
    for c in R:
        m = re.match(r"(6개월|3개월|1개월|15일) 확약$", c[0])
        if m and len(c) >= 2 and num(c[1]) is not None:
            lk[{"6개월": "m6", "3개월": "m3", "1개월": "m1", "15일": "d15"}[m.group(1)]] = num(c[1])
    it["lockup"] = None
    c = starts("총 수량 대비 비율(%)")
    if c and len(c) >= 2:
        it["lockup"] = num(c[1])
    it["inst_comp"] = None
    for i, c in enumerate(R):
        if "단순경쟁" in c and i + 1 < len(R):
            m = re.search(r"([\d,.]+)\s*:\s*1", R[i + 1][-1])
            if m:
                it["inst_comp"] = num(m.group(1))
                it["inst_participants"] = num(R[i + 1][0])
            break
    tot = sum(lk.values())
    it["lockup_d15_share"] = (lk.get("d15", 0) / tot) if tot else None
    top = 0.0
    seen = False
    for i, c in enumerate(R):
        if c[0] == "구분" and c[-1] == "비율(%)":
            for c2 in R[i + 1:]:
                if c2[0].startswith("합계") or len(c2) < 2:
                    break
                p = num(c2[-1]) if "%" in c2[-1] else None
                lab = c2[0]
                price = num(lab)
                if p is not None and "~" not in lab and "미제시" not in lab and "미만" not in lab \
                        and price is not None and it["band_hi"] and price >= it["band_hi"]:
                    top += p
                    seen = True
            break
    it["px_top_or_above_pct"] = round(top, 2) if seen else None

    c = next((c for c in R if c[0].startswith("PER") and "공모가" in c[0]), None)
    it["per_offer"] = num(c[1]) if c and len(c) > 1 else None
    it["float_pct"] = it["listed_shares"] = None
    for c in R:
        if c[0] == "합계" and len(c) >= 9 and c[2] == "100" and c[4] == "100":
            it["listed_shares"], it["float_pct"] = num(c[3]), num(c[8])
            break
    return it


# ---------------------------------------------------------------- 점수
def score_parts(it, comp, lock, offer, hot5):
    amt = (it["total_shares_offer"] * offer / 1e6) if it.get("total_shares_offer") else (it.get("amount_mn") or 0)
    lock = lock or 0
    return {
        "확약률": 4 if lock >= 20 else 3 if lock >= 10 else 2 if lock >= 5 else 1 if lock >= 1 else 0,
        "기관경쟁률": 3 if comp >= 1000 else 2 if comp >= 500 else 1 if comp >= 200 else 0,
        "공모가 위치": 1 if offer >= it["band_hi"] else (-2 if offer < it["band_lo"] else 0),
        "공모규모": 1 if amt <= 30000 else (-1 if amt >= 100000 else 0),
        "시장분위기(직전5건)": 1 if hot5 >= 0.6 else (-1 if hot5 < 0.2 else 0),
    }


def finish_item(it, today, hot5):
    L, se, fe = pdate(it["listing"]), pdate(it["sub_end"]), pdate(it["fcst_end"])
    it["offer_fixed"] = bool(it["offer"])
    it["calc_offer"] = it["offer"] or it["band_hi"]
    if se and se < today:
        it["stage"] = "상장 대기"
    elif it["inst_comp"] or it["offer"]:
        it["stage"] = "청약 대기"
    elif fe and fe < today:
        it["stage"] = "결과 발표 대기"
    else:
        it["stage"] = "수요예측 대기"
    it["score"] = it["score_parts"] = None
    flags = []
    if it["spac"]:
        it["flags"] = flags
        return it
    if it["inst_comp"] and it["offer"] and it["band_hi"] and it["band_lo"]:
        it["score_parts"] = score_parts(it, it["inst_comp"], it["lockup"], it["offer"], hot5)
        it["score"] = sum(it["score_parts"].values())
        if it["amount_mn"]:
            x = (RC["b0"] + RC["log_comp"] * math.log(it["inst_comp"]) + RC["lockup"] * (it["lockup"] or 0)
                 + RC["hot5"] * hot5 + RC["log_amt"] * math.log(it["amount_mn"]))
            it["retail_comp_pred"] = round(math.exp(x), 1)
    elif it["band_hi"] and it["band_lo"]:
        cols = [("1% 미만", 0.5), ("1~5%", 3), ("5~10%", 7.5), ("10~20%", 15), ("20% 이상", 25)]
        rws = [("200 미만", 100), ("200~500", 350), ("500~1000", 750), ("1000 이상", 1500)]
        it["scen"] = {"cols": [c for c, _ in cols], "rows": [
            {"comp": r, "cells": [{"score": sum(score_parts(it, cv, lv, it["band_hi"], hot5).values())} for _, lv in cols]}
            for r, cv in rws]}
    # 주의·호재 표시
    if it.get("lockup_d15_share") is not None and it["lockup_d15_share"] >= 0.7 and (it["lockup"] or 0) >= 1:
        flags.append(["warn", f"확약의 {it['lockup_d15_share'] * 100:.0f}%가 15일짜리라 실질 잠금 효과가 약함"])
    if it.get("px_top_or_above_pct") is not None and it["px_top_or_above_pct"] >= 95:
        flags.append(["good", "기관 95% 이상이 밴드 상단 이상 가격 제시"])
    if it["offer"] and it["band_lo"] and it["offer"] < it["band_lo"]:
        flags.append(["bad", "공모가가 희망가 하단 아래로 확정 — 기관 수요 부진"])
    if it["retail_comp"] and it["retail_comp"] >= 1000:
        flags.append(["good", f"일반청약 경쟁률 {it['retail_comp']:,.0f}:1 — 1,000:1 이상은 종가 손실 7%, 시초가 중앙값 +121%"])
    if it["float_pct"] is not None:
        if it["float_pct"] <= 25:
            flags.append(["good", f"상장일 유통가능 약 {it['float_pct']:.0f}% — 가벼운 편"])
        elif it["float_pct"] >= 35:
            flags.append(["warn", f"상장일 유통가능 약 {it['float_pct']:.0f}% — 물량 부담"])
    if it["per_offer"] and it["per_offer"] > 40:
        flags.append(["warn", f"공모가 기준 PER {it['per_offer']:.0f}배 — 고평가 의심"])
    if it["net_income_mn"] is not None and it["net_income_mn"] < 0:
        flags.append(["warn", "직전 사업연도 순손실 — 현행 제도에서 적자는 시초가 중앙값 +55% (흑자 +74%), 종가 손실 31% (흑자 21%)"])
    if it["amount_mn"] and it["amount_mn"] >= 100000:
        flags.append(["warn", f"공모규모 {it['amount_mn'] / 100:,.0f}억 — 대형은 첫날 상승폭이 작은 경향"])
    if it["calc_offer"] and it["calc_offer"] >= 30000:
        flags.append(["warn", "공모가 3만원 이상 — 과거 손실 확률 상승 구간"])
    if it["old_shares"] and it["total_shares_offer"] and it["old_shares"] / it["total_shares_offer"] >= 0.2:
        flags.append(["warn", f"구주매출 {it['old_shares'] / it['total_shares_offer'] * 100:.0f}% — 공모자금 일부가 기존 주주에게 감"])
    it["flags"] = flags
    return it


# ---------------------------------------------------------------- 철회·일정 변경
# 38커뮤니케이션은 공모를 철회한 종목을 설명 없이 일정표에서 지운다.
# 그래서 직전 보드에 있던 종목이 사라지면 DART 기타법인 공시 목록에서 철회신고서를 찾아 이유를 붙인다.
DART_LIST = "https://dart.fss.or.kr/dsac001/mainG.do?selectDate={}&currentPage={}"
EVENTS = os.path.join(ROOT, "data", "events.json")
GONE_KEEP_DAYS = 14     # 철회·사라짐을 보드에 남겨 두는 기간
CHANGE_KEEP_DAYS = 7    # 일정 변경 표시를 남겨 두는 기간
_dart_days = {}


def norm_name(s):
    return re.sub(r"\(.*?\)|㈜|주식회사|\s", "", s or "")


def same_corp(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def dart_day(d):
    """그날 DART 기타법인(비상장) 공시 목록. 공모 준비 회사는 상장 전이라 여기에 나온다."""
    if d in _dart_days:
        return _dart_days[d]
    out = []
    for page in range(1, 8):
        doc = fetch(DART_LIST.format(d.strftime("%Y.%m.%d"), page), enc="utf-8")
        if not doc:
            break
        n = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", doc, re.S | re.I):
            c = [clean(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            m = re.search(r"rcpNo=(\d+)", tr)
            if len(c) >= 5 and m:
                n += 1
                out.append({"time": c[0], "corp": re.sub(r"^[유코넥기]\s+", "", c[1]), "report": c[2],
                            "rcp": m.group(1), "rm": c[5] if len(c) > 5 else ""})
        if n < 100:
            break
        if not FIX:
            time.sleep(0.3)
    _dart_days[d] = out
    return out


def find_withdrawal(name, start, end):
    """start~end 사이 DART 공시에서 그 회사의 철회신고서(또는 '철회됨' 표시)를 찾는다."""
    marked = None
    d = end
    while d >= start:
        if d.weekday() < 5:
            for r in dart_day(d):
                if not same_corp(r["corp"], name):
                    continue
                if "철회신고서" in r["report"]:
                    return {"kind": "철회", "date": d.strftime("%Y.%m.%d"), "time": r["time"], "rcp": r["rcp"]}
                if "철" in r["rm"] and not marked:
                    marked = {"kind": "철회", "date": None, "time": None, "rcp": r["rcp"]}
        d -= dt.timedelta(days=1)
    return marked


def load_events():
    try:
        ev = json.load(open(EVENTS, encoding="utf8"))
    except Exception:
        ev = {}
    return {"gone": ev.get("gone", []), "changes": ev.get("changes", [])}


def prev_board():
    try:
        s = open(os.path.join(ROOT, "index.html"), encoding="utf8").read()
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', s, re.S)
        return json.loads(m.group(1)) if m else {}
    except Exception as e:  # noqa: BLE001
        log("이전 보드를 읽지 못했습니다:", repr(e))
        return {}


def track_events(items, listed_nos, today):
    ev, prev = load_events(), prev_board()
    prev_items = {p["no"]: p for p in prev.get("items", []) if p.get("no")}
    cur = {it["no"]: it for it in items}
    known = {g["no"] for g in ev["gone"]}

    def ended(p):
        L, se = pdate(p.get("listing")), pdate(p.get("sub_end"))
        return (L and L < today) or (se and (today - se).days > 25)

    vanished = [p for no, p in prev_items.items() if no not in cur and no not in listed_nos and no not in known and not ended(p)]
    if len(vanished) > 4:
        log("한 번에 사라진 종목이 너무 많아 수집 문제로 보고 기록하지 않습니다:", [p["name"] for p in vanished])
        vanished = []
    for p in vanished:
        log("일정표에서 사라짐:", p["name"])
        ev["gone"].append({"no": p["no"], "name": p["name"], "since": today.isoformat(), "kind": "확인 필요",
                           "sub_start": p.get("sub_start"), "sub_end": p.get("sub_end")})
    for g in ev["gone"]:
        if g.get("kind") != "철회" or not g.get("date"):
            since = pdate(g["since"]) or today
            hit = find_withdrawal(g["name"], max(since - dt.timedelta(days=7), today - dt.timedelta(days=GONE_KEEP_DAYS + 7)), today)
            if hit and (hit.get("date") or g.get("kind") != "철회"):
                g.update(hit)
                log("DART 철회 확인:", g["name"], hit)
    ev["gone"] = [g for g in ev["gone"] if g["no"] not in cur and (today - pdate(g["since"])).days <= GONE_KEEP_DAYS]

    for no, it in cur.items():
        p = prev_items.get(no)
        if p and p.get("sub_start") and it.get("sub_start") and p["sub_start"] != it["sub_start"]:
            ch = {"no": no, "name": it["name"], "what": "청약일", "from": p["sub_start"], "to": it["sub_start"], "since": today.isoformat()}
            if not any(c["no"] == no and c["to"] == ch["to"] for c in ev["changes"]):
                ev["changes"].append(ch)
                log("일정 변경:", it["name"], ch["from"], "→", ch["to"])
    ev["changes"] = [c for c in ev["changes"] if c["no"] in cur and (today - pdate(c["since"])).days <= CHANGE_KEEP_DAYS]
    for c in ev["changes"]:
        cur[c["no"]]["changed"] = c
    json.dump(ev, open(EVENTS, "w", encoding="utf8"), ensure_ascii=False, indent=1)
    return ev


# ---------------------------------------------------------------- 시장
def index_series(symbol):
    doc = fetch(FCHART.format(symbol))
    out = []
    for d, _o, _h, _l, c, _v in re.findall(r'data="(\d{8})\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^"]*)"', doc or ""):
        try:
            out.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)))
        except ValueError:
            pass
    return out


def regime_of(series):
    """강세장: 종가>120일선 & 60일 수익률>0 / 약세장: 종가<120일선 & 60일 수익률<0 / 그 외 횡보. 급락: 20일 수익률 −10% 이하."""
    c = [v for _, v in series]
    last, ma = c[-1], sum(c[-120:]) / len(c[-120:])
    r20, r60 = last / c[-21] - 1, last / c[-61] - 1
    state = "강세장" if last > ma and r60 > 0 else "약세장" if last < ma and r60 < 0 else "횡보"
    return {"state": state, "r20": r20, "r60": r60, "ma120": ma, "crash": r20 <= -0.10}


# ---------------------------------------------------------------- 실행
def main():
    now = dt.datetime.now(KST)
    today = pdate(os.environ.get("IPO_TODAY")) or now.date()
    base = json.load(open(os.path.join(ROOT, "data", "base.json"), encoding="utf8"))

    kq, kp = index_series("KOSDAQ"), index_series("KOSPI")
    if len(kq) < 130 or len(kp) < 130:
        sys.exit("지수 데이터를 충분히 받지 못했습니다. index.html 을 바꾸지 않습니다.")
    rq, rp = regime_of(kq), regime_of(kp)

    r1 = {no: c for no, c in list_rows("r1", [1, 2, 3])}
    nw = list_rows("nw", [1, 2])
    # 청약이 끝난 지 3주 넘은 종목은 이미 상장했으므로 상세를 읽지 않는다
    sched = [(no, c) for no, c in list_rows("k", [1, 2]) if (prange(c[1])[1] - today).days >= -21] + list_rows("r", [1])
    if not nw or not sched:
        sys.exit("일정 목록을 받지 못했습니다. index.html 을 바꾸지 않습니다.")

    # 최근 상장 실적(스팩 제외) → 시장분위기
    listed = []
    for no, c in nw:
        d, offer, opn = pdate(c[1]), num(c[4]), num(c[6])
        if "스팩" in c[0] or not d or d > today or not offer or not opn:
            continue
        cl = num(c[8]) if len(c) > 8 and re.fullmatch(r"[\d,]+", c[8] or "") else None
        listed.append({"no": no, "name": re.sub(r"\((유가|코스닥)\)$", "", c[0]), "d": d, "offer": offer,
                       "r_open": opn / offer - 1, "r_close1": (cl / offer - 1) if cl else None})
    listed.sort(key=lambda x: x["d"], reverse=True)
    if len(listed) < 5:
        sys.exit("최근 상장 실적이 5건 미만입니다. index.html 을 바꾸지 않습니다.")

    def hot_at(i):
        seg = listed[i:i + 5]
        return sum(x["r_open"] for x in seg) / len(seg) if len(seg) >= 3 else 0.3

    hot5 = hot_at(0)
    recent = []
    for i, x in enumerate(listed[:12]):
        row = {"name": x["name"], "date": f"{x['d'].month:02d}/{x['d'].day:02d}", "offer": x["offer"], "comp": None,
               "lockup": None, "r_open": x["r_open"], "r_close1": x["r_close1"], "score": None}
        c = r1.get(x["no"])
        if c:
            band, comp = nums(c[2]), num(c[5])
            row["comp"], row["lockup"] = comp, num(c[6])
            if comp and len(band) >= 2:
                fake = {"total_shares_offer": None, "amount_mn": num(c[4]), "band_lo": band[0], "band_hi": band[1]}
                row["score"] = sum(score_parts(fake, comp, row["lockup"], x["offer"], hot_at(i + 1)).values())
        recent.append(row)
    closed = [x for x in listed if x["r_close1"] is not None][:10]

    # 대기 종목
    nos = []
    for no, c in sched + [(no, c) for no, c in nw if (pdate(c[1]) or today) >= today]:
        if no not in nos:
            nos.append(no)
    items, fail = [], 0
    for no in nos:
        doc = fetch(f"{BASE38}?o=v&no={no}")
        it = parse_detail(no, doc) if doc else None
        if not it or not it["band_hi"] or not it["underwriters"]:
            fail += 1
            log("상세 해석 실패:", no)
            continue
        L, se = pdate(it["listing"]), pdate(it["sub_end"])
        if (L and L < today) or (not L and se and (today - se).days > 30):
            continue
        items.append(finish_item(it, today, hot5))
        if not FIX:
            time.sleep(0.4)
    if not items or (not FIX and fail > len(nos) / 2):
        sys.exit(f"종목 상세를 충분히 받지 못했습니다({fail}/{len(nos)} 실패). index.html 을 바꾸지 않습니다.")
    far = dt.date(2999, 1, 1)
    items.sort(key=lambda it: (pdate(it["listing"]) or far, pdate(it["sub_start"]) or far))

    ev = track_events(items, {x["no"] for x in listed}, today)

    data = dict(base)
    data["gone"] = ev["gone"]
    data["generated"] = today.isoformat()
    data["updated_at"] = now.strftime("%Y-%m-%d %H:%M")
    m = dict(base["market"])
    m.update(kosdaq_last=kq[-1][1], kosdaq_date=kq[-1][0], kosdaq_20d=rq["r20"], kosdaq_5d=kq[-1][1] / kq[-6][1] - 1,
             kosdaq_60=[list(x) for x in kq[-60:]], hot5=hot5, recent_n=len(closed),
             recent_below_offer_close=sum(1 for x in closed if x["r_close1"] < 0),
             regime_kospi=rp["state"], regime_kosdaq=rq["state"], weak_regime=bool(rq["state"] == "약세장" or rq["crash"]))
    data["market"], data["recent"], data["items"] = m, recent, items

    tpl = open(os.path.join(ROOT, "template.html"), encoding="utf8").read()
    if tpl.count("/*DATA*/") != 1:
        sys.exit("template.html 에 /*DATA*/ 자리가 정확히 하나 있어야 합니다.")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">" \
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"></head><body>\n" \
           + tpl.replace("/*DATA*/", blob) + "\n</body></html>\n"
    open(os.path.join(ROOT, "index.html"), "w", encoding="utf8").write(page)
    log(f"완료: 종목 {len(items)}개(상세 실패 {fail}) · 최근 상장 {len(recent)}건 · 코스닥 {kq[-1]} · "
        f"{'약세장' if m['weak_regime'] else '보통장'} 모드 · hot5 {hot5:.3f}")
    for it in items:
        log(f"  {it['name']}: {it['stage']} · 점수 {it['score']} · 청약 {it['sub_start']}~{it['sub_end']} · 상장 {it['listing']}")
    for g in ev["gone"]:
        log(f"  [사라짐] {g['name']}: {g['kind']} {g.get('date') or ''} {g.get('rcp') or ''}")


if __name__ == "__main__":
    main()
