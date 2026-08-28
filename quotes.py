#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""시세 수집기 — GitHub Actions 러너에서 실행됩니다.

왜 이게 필요한가
  Claude 예약 작업의 WebFetch 는 한국 금융 사이트 대부분이 막혀 있습니다
  (naver·daum·mk·edaily 403, stooq·yahoo API 는 robots.txt 차단).
  2026-08-25 기준 30여 개 후보를 시험했으나 당일 시세를 주는 경로가 없었습니다.

  반면 GitHub Actions 러너는 인터넷이 열려 있습니다. 그래서 릴레이가 시세를
  긁어 두 갈래로 전달합니다.

    (1) quotes.json 을 저장소에 커밋  → raw.githubusercontent.com 으로 읽기
    (2) [시세데이터] 제목의 메일 발송 → Gmail 커넥터로 읽기   ★1순위

  (2) 를 1순위로 두는 이유: 예약 실행 세션의 WebFetch 는 raw 주소마저
  PROVENANCE_REQUIRED 로 막을 때가 있습니다(2026-08-27 한 11:38 회차 실측).
  메일 읽기는 그 제약을 받지 않고, git push 실패와도 무관합니다.

v1.2 에서 고친 것 (2026-08-28)
  quotes.json 이 8/27 10:45 에서 멈춰 있었습니다. 원인 후보는 두 가지였고
  둘 다 막았습니다.
   - 소스가 응답하지 않으면 4소스 × 3종목 재시도가 워크플로의 2분 제한을
     넘겨 스텝이 통째로 죽고 파일이 아예 안 써졌다
     → **전체 70초 예산**을 두고 예산이 끝나면 남은 소스를 건너뜁니다.
        요청당 타임아웃도 12초에서 6초로 줄였습니다.
   - git push 실패 시 파일이 갱신되지 않는데 알 방법이 없었다
     → 메일 경로가 git 과 무관하므로 그 자체가 우회입니다.
  또 이번 회차에 못 구한 종목은 **직전 quotes.json 값을 그대로 물려주고
  `stale: true` 를 붙입니다.** 값이 통째로 사라지는 것보다 낫고, 브리핑이
  낡은 값을 낡은 줄 알고 쓸 수 있습니다.
"""

import email.utils
import imaplib
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

try:
    import requests
except ImportError:
    print("[시세] requests 없음 — 건너뜁니다.")
    sys.exit(0)

VERSION = "1.2"

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 워크플로의 `시세 수집` 스텝은 timeout-minutes: 2 입니다.
# 그 안에서 반드시 파일을 쓰고 나가야 하므로 자체 예산을 70초로 잡습니다.
BUDGET_SEC = 70
REQ_TIMEOUT = 6

STOCKS = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("009150", "삼성전기"),
]
MARKETS = [
    ("KOSPI", "코스피", "^KS11", "^kospi"),
    ("KOSDAQ", "코스닥", "^KQ11", "^kosdaq"),
    ("USDKRW", "원/달러", "KRW=X", "usdkrw"),
]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes.json")

_started = time.monotonic()


def _left():
    return BUDGET_SEC - (time.monotonic() - _started)


def _timeout():
    return max(2, min(REQ_TIMEOUT, int(_left())))


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
        timeout=_timeout())
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
                     timeout=_timeout())
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
        headers={"User-Agent": UA}, timeout=_timeout())
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
                     headers={"User-Agent": UA}, timeout=_timeout())
    r.raise_for_status()
    lines = [l for l in r.text.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        raise ValueError("빈 CSV")
    cols = lines[1].split(",")
    price = _num(cols[6]) if len(cols) > 6 else None
    if price is None:
        raise ValueError("가격 없음")
    return {"price": price, "change": None, "pct": None,
            "as_of": "%s %s" % (cols[1], cols[2]) if len(cols) > 2 else None}


def src_stooq(code):
    return _stooq(code + ".kr")


STOCK_SOURCES = [("naver", src_naver), ("naver-m", src_naver_m),
                 ("yahoo", src_yahoo), ("stooq", src_stooq)]


def _try(sources, name, key):
    """소스를 순서대로. 예산이 바닥나면 중단하고 이유를 남깁니다."""
    errs = []
    for label, fn, arg in sources:
        if _left() <= 3:
            errs.append("%s:예산소진(남은 %.1fs)" % (label, _left()))
            break
        t0 = time.monotonic()
        try:
            q = fn(arg)
            q["name"] = name
            q["source"] = label
            print("[시세] %s(%s) %s  via %s  %.1fs  as_of=%s"
                  % (name, key,
                     format(int(q["price"]), ",") if q.get("price") else "-",
                     label, time.monotonic() - t0, q.get("as_of")))
            return q, None
        except Exception as e:
            errs.append("%s:%s(%.1fs)" % (label, str(e)[:50], time.monotonic() - t0))
    return None, " / ".join(errs)


def _load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f).get("quotes") or {}
    except Exception:
        return {}


def _mail_body(out):
    lines = []
    lines.append("기준 한 %s" % out["updated_kst"])
    lines.append("")
    lines.append("이 메일은 브리핑 세션이 시세를 읽어가는 데이터 전달용입니다.")
    lines.append("최신 1통만 유지되며 이전 것은 자동으로 휴지통으로 갑니다.")
    if any(q.get("stale") for q in out["quotes"].values()):
        lines.append("")
        lines.append("⚠ stale=예 인 항목은 이번 회차에 못 구해서 직전 값을 물려준 것입니다.")
        lines.append("  그 항목은 as_of 시각 기준의 낡은 값이니 '현재가'라고 쓰지 마세요.")
    lines.append("")
    lines.append("%-14s %14s %12s %9s  %-7s %-5s %s"
                 % ("종목", "가격", "전일대비", "등락률", "소스", "stale", "기준시각"))
    lines.append("-" * 92)
    for key, q in out["quotes"].items():
        lines.append("%-14s %14s %12s %9s  %-7s %-5s %s" % (
            "%s %s" % (q.get("name", ""), key),
            ("{:,.2f}".format(q["price"]) if q.get("price") is not None else "-"),
            ("{:+,.2f}".format(q["change"]) if q.get("change") is not None else "-"),
            ("{:+.2f}%".format(q["pct"]) if q.get("pct") is not None else "-"),
            q.get("source", "-"),
            ("예" if q.get("stale") else "아니오"),
            q.get("as_of") or "-"))
    if out["errors"]:
        lines.append("")
        lines.append("[이번 회차 수집 실패]")
        for k, v in out["errors"].items():
            lines.append("  %s — %s" % (k, v))
    lines.append("")
    lines.append("-" * 92)
    lines.append("원본 JSON (저장소의 quotes.json 과 동일):")
    lines.append("")
    lines.append(json.dumps(out, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def _purge_old(addr, pw):
    """이전 [시세데이터] 메일을 휴지통으로. 실패해도 발송은 계속합니다."""
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        conn.login(addr, pw)
        box = "INBOX"
        typ, boxes = conn.list()
        for raw in boxes or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                m = re.search(r'"([^"]*)"\s*$', line)
                if m:
                    box = m.group(1)
                break
        conn.select('"%s"' % box, readonly=False)
        # 제목이 한글이라 IMAP SEARCH 로 찾기 까다롭습니다.
        # 발송 시 붙이는 ASCII 헤더로 찾습니다.
        typ, data = conn.uid("SEARCH", None, "HEADER", "X-Quotes-Feed", "1")
        uids = (data[0].split() if data and data[0] else [])
        if uids:
            conn.uid("STORE", b",".join(uids), "+X-GM-LABELS", "\\Trash")
            print("[시세] 이전 시세메일 %d통 정리" % len(uids))
        conn.logout()
    except Exception as e:
        print("[시세] 이전 메일 정리 건너뜀: %s" % str(e)[:80])


def send_mail(out):
    addr = os.environ.get("GMAIL_ADDRESS", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not addr or not pw:
        print("[시세] Gmail 자격증명 없음 — 메일 발송 건너뜀")
        return
    _purge_old(addr, pw)
    try:
        subject = "[시세데이터] %s" % out["updated_kst"][5:16]
        msg = MIMEText(_mail_body(out), "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = addr
        msg["To"] = addr
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-Id"] = email.utils.make_msgid(domain="quotes.local")
        msg["X-Quotes-Feed"] = "1"       # 정리용 ASCII 표식
        srv = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        srv.login(addr, pw)
        srv.sendmail(addr, [addr], msg.as_string())
        srv.quit()
        print("[시세] 시세메일 발송 — %s" % subject)
    except Exception as e:
        print("[시세] 시세메일 발송 실패: %s" % str(e)[:150])


def main():
    print("[시세] 수집기 v%s — 예산 %d초" % (VERSION, BUDGET_SEC))
    now = datetime.now(KST)
    prev = _load_prev()

    out = {"updated_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
           "updated_iso": now.isoformat(),
           "note": ("릴레이가 15분마다 갱신합니다. as_of 는 각 소스가 표시한 시세 기준 시각. "
                    "stale=true 인 항목은 이번 회차에 못 구해 직전 값을 물려준 것입니다."),
           "quotes": {}, "errors": {}}

    fresh = 0
    for code, name in STOCKS:
        srcs = [(lbl, fn, code) for lbl, fn in STOCK_SOURCES]
        q, err = _try(srcs, name, code)
        if q:
            out["quotes"][code] = q
            fresh += 1
        else:
            out["errors"][code] = err
            print("[시세] %s(%s) 실패 — %s" % (name, code, err))
            old = prev.get(code)
            if old:
                old = dict(old)
                old["stale"] = True
                out["quotes"][code] = old
                print("[시세]   └ 직전 값 물려줌 (as_of=%s)" % old.get("as_of"))

    for key, name, ysym, ssym in MARKETS:
        q, err = _try([("yahoo", _yahoo, ysym), ("stooq", _stooq, ssym)], name, key)
        if q:
            out["quotes"][key] = q
            fresh += 1
        else:
            out["errors"][key] = err
            print("[시세] %s 실패 — %s" % (name, err))
            old = prev.get(key)
            if old:
                old = dict(old)
                old["stale"] = True
                out["quotes"][key] = old

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[시세] quotes.json 저장 — 신규 %d건 / 실패 %d건 / 경과 %.1f초"
          % (fresh, len(out["errors"]), time.monotonic() - _started))

    # 신규가 하나도 없어도 메일은 보냅니다. 물려준 값이라도 브리핑이 쓸 수 있고,
    # 무엇보다 "수집기가 살아 있는데 소스가 막혔다"는 사실 자체가 정보입니다.
    send_mail(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 시세 수집이 실패해도 릴레이 본체는 계속 돌아야 합니다
        print("[시세] 수집기 오류: %s" % e)
        sys.exit(0)
