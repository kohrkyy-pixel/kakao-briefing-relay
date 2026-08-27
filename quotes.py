#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""시세 수집기 — GitHub Actions 러너에서 실행됩니다.

왜 이게 필요한가
  Claude 예약 작업의 WebFetch 는 한국 금융 사이트 대부분이 막혀 있습니다
  (naver·daum·mk·edaily·reuters 403, stooq·yahoo API 는 robots.txt 차단).
  2026-08-25 기준 30여 개 후보를 시험했으나 당일 시세를 주는 경로가 없었습니다.

  반면 GitHub Actions 러너는 인터넷이 열려 있습니다. 그래서 릴레이가 시세를
  긁어 quotes.json 으로 저장소에 올려두면, Claude 는 raw.githubusercontent.com
  으로 그 파일만 읽으면 됩니다. raw 는 WebFetch 로 정상 접근됩니다.

  릴레이가 15분마다 도니 시세도 최대 15분 지연입니다.

여러 소스를 순서대로 시도하고 먼저 성공한 것을 씁니다.
어느 소스가 먹혔는지 로그와 JSON 에 남기므로, 한 곳이 막혀도 바로 알 수 있습니다.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("[시세] requests 없음 — 건너뜁니다.")
    sys.exit(0)

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 종목코드 → 표시 이름
STOCKS = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("009150", "삼성전기"),
]
# 지수·환율 (야후/스투크 심볼)
MARKETS = [
    ("KOSPI", "코스피", "^KS11", "^kospi"),
    ("KOSDAQ", "코스닥", "^KQ11", "^kosdaq"),
    ("USDKRW", "원/달러", "KRW=X", "usdkrw"),
]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes.json")


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── 소스 1: 네이버 실시간 폴링 API ────────────────────────
def src_naver(code):
    r = requests.get(
        "https://polling.finance.naver.com/api/realtime/domestic/stock/%s" % code,
        headers={"User-Agent": UA, "Referer": "https://finance.naver.com/"},
        timeout=10)
    r.raise_for_status()
    d = r.json()
    items = (d.get("datas") or d.get("result", {}).get("areas", [{}])[0].get("datas") or [])
    if not items:
        raise ValueError("빈 응답")
    it = items[0]
    price = _num(it.get("closePrice") or it.get("nv"))
    chg = _num(it.get("compareToPreviousClosePrice") or it.get("cv"))
    pct = _num(it.get("fluctuationsRatio") or it.get("cr"))
    if price is None:
        raise ValueError("가격 없음")
    return {"price": price, "change": chg, "pct": pct,
            "as_of": it.get("localTradedAt") or it.get("tradeTime")}


# ── 소스 2: 네이버 모바일 API ─────────────────────────────
def src_naver_m(code):
    r = requests.get("https://m.stock.naver.com/api/stock/%s/basic" % code,
                     headers={"User-Agent": UA, "Referer": "https://m.stock.naver.com/"},
                     timeout=10)
    r.raise_for_status()
    d = r.json()
    price = _num(d.get("closePrice"))
    if price is None:
        raise ValueError("가격 없음")
    return {"price": price,
            "change": _num(d.get("compareToPreviousClosePrice")),
            "pct": _num(d.get("fluctuationsRatio")),
            "as_of": d.get("localTradedAt")}


# ── 소스 3: 야후 차트 API ─────────────────────────────────
def _yahoo(symbol):
    r = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/%s" % symbol,
        params={"interval": "1m", "range": "1d"},
        headers={"User-Agent": UA}, timeout=12)
    r.raise_for_status()
    m = r.json()["chart"]["result"][0]["meta"]
    price = _num(m.get("regularMarketPrice"))
    prev = _num(m.get("chartPreviousClose") or m.get("previousClose"))
    if price is None:
        raise ValueError("가격 없음")
    ts = m.get("regularMarketTime")
    as_of = (datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d %H:%M:%S")
             if ts else None)
    chg = (price - prev) if prev else None
    pct = ((price / prev - 1) * 100) if prev else None
    return {"price": price, "change": chg, "pct": pct, "as_of": as_of}


def src_yahoo(code):
    return _yahoo(code + ".KS")


# ── 소스 4: 스투크 CSV ────────────────────────────────────
def _stooq(sym):
    r = requests.get("https://stooq.com/q/l/",
                     params={"s": sym, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                     headers={"User-Agent": UA}, timeout=12)
    r.raise_for_status()
    lines = [l for l in r.text.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        raise ValueError("빈 CSV")
    cols = lines[1].split(",")
    price = _num(cols[6]) if len(cols) > 6 else None
    op = _num(cols[3]) if len(cols) > 3 else None
    if price is None:
        raise ValueError("가격 없음")
    return {"price": price, "change": None, "pct": None,
            "as_of": "%s %s" % (cols[1], cols[2]) if len(cols) > 2 else None,
            "open": op}


def src_stooq(code):
    return _stooq(code + ".kr")


STOCK_SOURCES = [("naver", src_naver), ("naver-m", src_naver_m),
                 ("yahoo", src_yahoo), ("stooq", src_stooq)]


def fetch_one(code, name):
    errs = []
    for label, fn in STOCK_SOURCES:
        try:
            q = fn(code)
            q["name"] = name
            q["source"] = label
            return q, None
        except Exception as e:
            errs.append("%s:%s" % (label, str(e)[:60]))
    return None, " / ".join(errs)


def fetch_market(key, name, ysym, ssym):
    for label, fn, arg in (("yahoo", _yahoo, ysym), ("stooq", _stooq, ssym)):
        try:
            q = fn(arg)
            q["name"] = name
            q["source"] = label
            return q, None
        except Exception as e:
            last = "%s:%s" % (label, str(e)[:60])
    return None, last


def main():
    now = datetime.now(KST)
    out = {"updated_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
           "updated_iso": now.isoformat(),
           "note": "GitHub Actions 러너가 15분마다 갱신합니다. as_of 는 각 소스가 표시한 시세 기준 시각입니다.",
           "quotes": {}, "errors": {}}

    ok = 0
    for code, name in STOCKS:
        q, err = fetch_one(code, name)
        if q:
            out["quotes"][code] = q; ok += 1
            print("[시세] %s(%s) %s  via %s  as_of=%s"
                  % (name, code, format(int(q["price"]), ","), q["source"], q.get("as_of")))
        else:
            out["errors"][code] = err
            print("[시세] %s(%s) 실패 — %s" % (name, code, err))

    for key, name, ysym, ssym in MARKETS:
        q, err = fetch_market(key, name, ysym, ssym)
        if q:
            out["quotes"][key] = q; ok += 1
            print("[시세] %s %s  via %s" % (name, q["price"], q["source"]))
        else:
            out["errors"][key] = err
            print("[시세] %s 실패 — %s" % (name, err))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[시세] quotes.json 저장 — 성공 %d건 / 실패 %d건" % (ok, len(out["errors"])))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 시세 수집이 실패해도 릴레이 본체는 계속 돌아야 합니다
        print("[시세] 수집기 오류: %s" % e)
        sys.exit(0)
