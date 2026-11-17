"""수집 가능 여부 시험: 원본 페이지를 probe/ 에 저장한다."""
import os, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
URLS = {
    "38_r1.html": "https://www.38.co.kr/html/fund/index.htm?o=r1",
    "38_r.html": "https://www.38.co.kr/html/fund/index.htm?o=r",
    "38_k.html": "https://www.38.co.kr/html/fund/index.htm?o=k",
    "38_nw.html": "https://www.38.co.kr/html/fund/index.htm?o=nw",
    "38_v_2349.html": "https://www.38.co.kr/html/fund/index.htm?o=v&no=2349",
    "38_v_2338.html": "https://www.38.co.kr/html/fund/index.htm?o=v&no=2338",
    "naver_fchart_kosdaq.xml": "https://fchart.stock.naver.com/sise.nhn?symbol=KOSDAQ&timeframe=day&count=300&requestType=0",
    "naver_fchart_kospi.xml": "https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI&timeframe=day&count=300&requestType=0",
    "naver_api_kosdaq.json": "https://api.stock.naver.com/chart/domestic/index/KOSDAQ?periodType=dayCandle",
    "naver_m_kosdaq.json": "https://m.stock.naver.com/api/index/KOSDAQ/price?pageSize=60&page=1",
}
os.makedirs("probe", exist_ok=True)
log = []
for name, url in URLS.items():
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            body = r.read()
            open(os.path.join("probe", name), "wb").write(body)
            log.append(f"{name}\t{r.status}\t{len(body)}\t{r.headers.get('Content-Type')}")
    except Exception as e:
        log.append(f"{name}\tERR\t{e!r}")
open("probe/_log.txt", "w").write("\n".join(log) + "\n")
print("\n".join(log))
