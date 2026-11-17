"""수집 가능 여부 시험: 원본 페이지를 probe/ 에 저장한다."""
import os, ssl, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
URLS = {
    "38_r1.html": "https://www.38.co.kr/html/fund/index.htm?o=r1",
    "38_r.html": "https://www.38.co.kr/html/fund/index.htm?o=r",
    "38_k.html": "https://www.38.co.kr/html/fund/index.htm?o=k",
    "38_nw.html": "https://www.38.co.kr/html/fund/index.htm?o=nw",
    "38_v_2349.html": "https://www.38.co.kr/html/fund/index.htm?o=v&no=2349",
    "38_v_2338.html": "https://www.38.co.kr/html/fund/index.htm?o=v&no=2338",
    "38_k_p2.html": "https://www.38.co.kr/html/fund/index.htm?o=k&page=2",
    "38_r1_p2.html": "https://www.38.co.kr/html/fund/index.htm?o=r1&page=2",
    "38_nw_p2.html": "https://www.38.co.kr/html/fund/index.htm?o=nw&page=2",
    "38_v_2321.html": "https://www.38.co.kr/html/fund/index.htm?o=v&no=2321",
    "naver_fchart_kosdaq.xml": "https://fchart.stock.naver.com/sise.nhn?symbol=KOSDAQ&timeframe=day&count=300&requestType=0",
    "naver_fchart_kospi.xml": "https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI&timeframe=day&count=300&requestType=0",
    "naver_api_kosdaq.json": "https://api.stock.naver.com/chart/domestic/index/KOSDAQ?periodType=dayCandle",
    "naver_m_kosdaq.json": "https://m.stock.naver.com/api/index/KOSDAQ/price?pageSize=60&page=1",
}
os.makedirs("probe", exist_ok=True)
log = []
for name, url in URLS.items():
    try:
        body, how = None, None
        for label, ciphers in (("default", None), ("no-dh", "DEFAULT:!DH:!DHE"), ("seclevel1", "DEFAULT:@SECLEVEL=1"), ("seclevel0", "DEFAULT:@SECLEVEL=0")):
            ctx = ssl.create_default_context()
            if ciphers: ctx.set_ciphers(ciphers)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=ctx) as r:
                    body, how, st, ct = r.read(), label, r.status, r.headers.get('Content-Type')
                break
            except Exception as e:
                err = e
        if body is None: raise err
        open(os.path.join("probe", name), "wb").write(body)
        log.append(f"{name}\t{st}\t{len(body)}\t{ct}\t{how}")
    except Exception as e:
        log.append(f"{name}\tERR\t{e!r}")
open("probe/_log.txt", "w").write("\n".join(log) + "\n")
print("\n".join(log))
