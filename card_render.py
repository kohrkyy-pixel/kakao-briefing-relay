#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""리포트 카드 PNG 렌더러.

Claude 가 메일에 담아 보낸 수치(JSON)를 받아 PC에서 직접 카드 이미지를 그립니다.
이미지 바이트가 클라우드를 거치지 않으므로 업로드 용량 문제가 없습니다.

필요: pip install pillow
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ── 색 (검증 통과 팔레트) ──────────────────────────────────
NAVY = (18, 48, 78)
GOLD = (185, 130, 42)
INK = (22, 32, 46)
SUB = (74, 90, 110)
MUTED = (132, 148, 166)
LINE = (227, 232, 239)
PAPER = (245, 247, 250)
WHITE = (255, 255, 255)
UP = (210, 69, 60)
DOWN = (43, 108, 176)
WARN = (180, 83, 26)
WARN_BG = (255, 243, 234)
HERO = (255, 139, 132)
HEAD_SUB = (157, 180, 206)
TRACK = (238, 241, 245)

W = 640
PAD = 18

_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf"),
    ("C:/Windows/Fonts/NanumGothic.ttf", "C:/Windows/Fonts/NanumGothicBold.ttf"),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
     "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
]


def _font_paths():
    for reg, bold in _FONT_CANDIDATES:
        if os.path.exists(reg):
            return reg, (bold if os.path.exists(bold) else reg)
    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. Windows 라면 맑은 고딕(malgun.ttf)이 있어야 합니다.")


class F:
    """자주 쓰는 크기를 미리 만들어 둡니다."""

    def __init__(self):
        reg, bold = _font_paths()
        self._reg, self._bold = reg, bold
        self._cache = {}

    def __call__(self, size, bold=False):
        key = (size, bold)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(self._bold if bold else self._reg, size)
        return self._cache[key]


def _hex(c, default=(46, 111, 217)):
    if not c:
        return default
    c = str(c).lstrip("#")
    if len(c) != 6:
        return default
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _won(n):
    try:
        return "{:,}".format(int(round(float(n))))
    except (TypeError, ValueError):
        return str(n)


def _wrap(draw, text, font, max_w):
    """글자 단위로 감싸기 (한국어는 공백이 드물어 단어 단위로는 부족)."""
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines


SECTION_COLORS = ["#2E6FD9", "#C07A1E", "#158A6E", "#7A4FBF", "#B4531A", "#2B6CB0"]


def render_news_card(data, out_path="card.png"):
    """섹션형 종합 뉴스 카드."""
    f = F()
    img = Image.new("RGB", (W, 4000), PAPER)
    d = ImageDraw.Draw(img)

    # ── 헤더 ─────────────────────────────────────────────
    head = data.get("headline")
    hh = 118 if head else 92
    d.rectangle([0, 0, W, hh], fill=NAVY)
    d.text((22, 20), "DAILY NEWS BRIEFING", font=f(10, True), fill=HEAD_SUB)
    d.text((22, 37), data.get("title", "종합 브리핑"), font=f(22, True), fill=WHITE)
    sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
    d.text((22, 68), sub, font=f(11), fill=HEAD_SUB)
    if head:
        for i, ln in enumerate(_wrap(d, head, f(12.5, True), W - 44)[:1]):
            d.text((22, 90), ln, font=f(12.5, True), fill=HERO)
    y = hh + 16

    for idx, sec in enumerate(data.get("sections") or []):
        name = sec.get("name", "")
        col = _hex(sec.get("color"), _hex(SECTION_COLORS[idx % len(SECTION_COLORS)]))
        items = sec.get("items") or []

        # 섹션 제목
        d.rectangle([PAD, y + 3, PAD + 4, y + 17], fill=col)
        d.text((PAD + 11, y), name, font=f(13, True), fill=INK)
        y += 26

        inner = W - 2 * PAD - 28
        blocks = []
        for it in items:
            if isinstance(it, (list, tuple)):
                head_t, body_t = (list(it) + [""])[:2]
            else:
                head_t, body_t = str(it), ""
            blocks.append((str(head_t), _wrap(d, body_t, f(11.5), inner) if body_t else []))

        box_h = 12
        for ht, lines in blocks:
            box_h += 18 + 16 * len(lines) + 9
        box_h = max(box_h - 3, 30)
        d.rounded_rectangle([PAD, y, W - PAD, y + box_h], 11, fill=WHITE, outline=LINE)

        ty = y + 11
        for i, (ht, lines) in enumerate(blocks):
            d.ellipse([PAD + 15, ty + 6, PAD + 21, ty + 12], fill=col)
            for ln in _wrap(d, ht, f(12.5, True), inner)[:1]:
                d.text((PAD + 28, ty), ln, font=f(12.5, True), fill=INK)
            ty += 18
            for ln in lines:
                d.text((PAD + 28, ty), ln, font=f(11.5), fill=SUB)
                ty += 16
            ty += 9
        y += box_h + 16

    y += 4
    d.line([PAD, y, W - PAD, y], fill=LINE)
    y += 8
    foot = data.get("footer") or "공개된 언론 보도를 정리한 참고 자료입니다."
    for ln in _wrap(d, foot, f(10), W - 2 * PAD)[:3]:
        d.text((PAD, y), ln, font=f(10), fill=MUTED); y += 14
    y += 12

    img = img.crop((0, 0, W, min(int(y), 4000)))
    img = img.convert("P", palette=Image.ADAPTIVE, colors=64)
    img.save(out_path, optimize=True)
    return out_path


def render_card(data, out_path="card.png"):
    """수치 dict → 카드 PNG. 저장 경로를 돌려줍니다.

    data["type"] == "news" 이면 섹션형 뉴스 카드로 그립니다.
    """
    if str(data.get("type", "")).lower() == "news" or data.get("sections"):
        return render_news_card(data, out_path)
    f = F()
    # 높이를 모르므로 넉넉히 그린 뒤 잘라냅니다
    img = Image.new("RGB", (W, 3000), PAPER)
    d = ImageDraw.Draw(img)

    holdings = data.get("holdings") or []
    market = data.get("market") or []
    checks = data.get("checkpoints") or []

    # ── 헤더 ─────────────────────────────────────────────
    hh = 152 if holdings else 130
    d.rectangle([0, 0, W, hh], fill=NAVY)
    y = 20
    d.text((22, y), "PORTFOLIO INTELLIGENCE REPORT", font=f(10, True), fill=HEAD_SUB)
    y += 17
    d.text((22, y), data.get("title", "투자 브리핑"), font=f(22, True), fill=WHITE)
    y += 30
    sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
    d.text((22, y), sub, font=f(11), fill=HEAD_SUB)
    y += 22

    pnl = data.get("pnl")
    if pnl is not None:
        d.text((22, y), "총 평가손익", font=f(11), fill=HEAD_SUB)
        sign = "+" if float(pnl) >= 0 else ""
        big = "%s%s" % (sign, _won(pnl))
        d.text((22, y + 15), big, font=f(29, True), fill=HERO)
        wpx = d.textlength(big, font=f(29, True))
        d.text((22 + wpx + 6, y + 27),
               "원 · %s%.2f%%" % (sign, float(data.get("pnl_pct", 0))),
               font=f(14, True), fill=HERO)
        if data.get("value") is not None:
            d.text((W - 22 - d.textlength("평가금액", font=f(11)), y),
                   "평가금액", font=f(11), fill=HEAD_SUB)
            vt = _won(data["value"]) + "원"
            d.text((W - 22 - d.textlength(vt, font=f(16, True)), y + 16),
                   vt, font=f(16, True), fill=WHITE)
    y = hh

    # ── 시장 지표 타일 ───────────────────────────────────
    if market:
        cw, ch = W / 3.0, 46
        for i, row in enumerate(market[:6]):
            k, v, chg, dirn = (list(row) + ["", ""])[:4]
            cx, cy = (i % 3) * cw, y + (i // 3) * ch
            d.rectangle([cx, cy, cx + cw, cy + ch], fill=WHITE, outline=LINE)
            d.text((cx + 11, cy + 7), str(k), font=f(10), fill=MUTED)
            d.text((cx + 11, cy + 21), str(v), font=f(14, True), fill=INK)
            vw = d.textlength(str(v), font=f(14, True))
            col = UP if str(dirn).lower() == "up" else DOWN
            d.text((cx + 15 + vw, cy + 24), str(chg), font=f(10, True), fill=col)
        y += ch * ((len(market[:6]) + 2) // 3)

    def section(title, yy):
        d.rectangle([PAD, yy + 3, PAD + 4, yy + 17], fill=GOLD)
        d.text((PAD + 11, yy), title, font=f(13, True), fill=INK)
        return yy + 26

    def card_box(yy, height):
        d.rounded_rectangle([PAD, yy, W - PAD, yy + height], 11, fill=WHITE, outline=LINE)

    # ── 포트폴리오 도넛 ──────────────────────────────────
    if holdings and any(h.get("weight") for h in holdings):
        y += 18
        y = section("포트폴리오", y)
        box_h = 168
        card_box(y, box_h)
        cx, cy, r, thick = PAD + 105, y + box_h / 2, 62, 26
        start = -90.0
        for h in holdings:
            wgt = float(h.get("weight") or 0)
            if wgt <= 0:
                continue
            ext = 360.0 * wgt / 100.0
            d.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + ext - 1.2,
                       fill=_hex(h.get("color")))
            start += ext
        d.ellipse([cx - r + thick, cy - r + thick, cx + r - thick, cy + r - thick], fill=WHITE)
        if data.get("value") is not None:
            t1 = "총 평가"
            d.text((cx - d.textlength(t1, font=f(10, True)) / 2, cy - 15), t1,
                   font=f(10, True), fill=SUB)
            t2 = _won(float(data["value"]) / 10000) + "만원"
            d.text((cx - d.textlength(t2, font=f(15, True)) / 2, cy - 1), t2,
                   font=f(15, True), fill=INK)
        lx, ly = PAD + 200, y + 26
        for h in holdings:
            d.rounded_rectangle([lx, ly + 3, lx + 10, ly + 13], 3, fill=_hex(h.get("color")))
            d.text((lx + 17, ly), str(h.get("name", "")), font=f(12, True), fill=INK)
            pct = "%.1f%%" % float(h.get("weight") or 0)
            d.text((W - PAD - 14 - d.textlength(pct, font=f(12, True)), ly), pct,
                   font=f(12, True), fill=SUB)
            ly += 26
        note = data.get("portfolio_note")
        if note:
            ly += 4
            d.line([lx, ly, W - PAD - 14, ly], fill=LINE)
            ly += 7
            for ln in _wrap(d, note, f(11), W - PAD - 20 - lx)[:2]:
                d.text((lx, ly), ln, font=f(11), fill=SUB); ly += 15
        y += box_h

    # ── 종목별 수익률 막대 ───────────────────────────────
    if holdings and any(h.get("pct") is not None for h in holdings):
        y += 18
        y = section("종목별 수익률", y)
        items = sorted([h for h in holdings if h.get("pct") is not None],
                       key=lambda x: -float(x["pct"]))
        rows = [(h.get("name", ""), float(h["pct"]), _hex(h.get("color"))) for h in items]
        if data.get("pnl_pct") is not None:
            rows.append(("전체", float(data["pnl_pct"]), NAVY))
        box_h = 20 + 34 * len(rows)
        card_box(y, box_h)
        x0, x1 = PAD + 92, W - PAD - 80
        vmax = max([abs(v) for _, v, _ in rows] + [0.01]) * 1.15
        by = y + 14
        for nm, v, col in rows:
            d.text((x0 - 10 - d.textlength(nm, font=f(11, True)), by + 1), nm,
                   font=f(11, True), fill=INK)
            bw = max((x1 - x0) * abs(v) / vmax, 4)
            d.rounded_rectangle([x0, by, x0 + bw, by + 15], 4, fill=col)
            lab = "%s%.2f%%" % ("+" if v >= 0 else "", v)
            d.text((x0 + bw + 8, by + 1), lab, font=f(11, True), fill=col)
            by += 34
        y += box_h

    # ── 보유 종목 ────────────────────────────────────────
    if holdings:
        y += 18
        y = section("보유 종목", y)
        box_h = 14 + 40 * len(holdings)
        card_box(y, box_h)
        ry = y + 12
        for i, h in enumerate(holdings):
            col = _hex(h.get("color"))
            d.rounded_rectangle([PAD + 14, ry + 5, PAD + 23, ry + 14], 3, fill=col)
            nx = PAD + 31
            d.text((nx, ry + 1), str(h.get("name", "")), font=f(13, True), fill=INK)
            nx += d.textlength(str(h.get("name", "")), font=f(13, True)) + 6
            if h.get("qty"):
                d.text((nx, ry + 4), "%s주" % h["qty"], font=f(10), fill=MUTED)
            hp = h.get("pnl")
            if hp is not None:
                sign = "+" if float(hp) >= 0 else ""
                lab = "%s%s원  %s%.2f%%" % (sign, _won(hp), sign, float(h.get("pct") or 0))
                d.text((W - PAD - 14 - d.textlength(lab, font=f(12, True)), ry + 2), lab,
                       font=f(12, True), fill=UP if float(hp) >= 0 else DOWN)
            sub2 = " · ".join(str(x) for x in (
                (_won(h["close"]) + "원") if h.get("close") else None, h.get("note")) if x)
            if sub2:
                d.text((PAD + 31, ry + 20), sub2, font=f(10.5), fill=SUB)
            if i < len(holdings) - 1:
                d.line([PAD + 14, ry + 36, W - PAD - 14, ry + 36], fill=LINE)
            ry += 40
        y += box_h

    # ── 핵심 체크포인트 ──────────────────────────────────
    if checks or data.get("risk"):
        y += 18
        y = section("핵심 체크포인트", y)
        inner_w = W - 2 * PAD - 28
        blocks = []
        for c in checks:
            head, body = (list(c) + [""])[:2] if isinstance(c, (list, tuple)) else (str(c), "")
            blocks.append((head, _wrap(d, body, f(12), inner_w) if body else []))
        risk_lines = _wrap(d, data["risk"], f(11.5), inner_w - 20) if data.get("risk") else []
        box_h = 14
        for head, lines in blocks:
            box_h += 19 + 17 * len(lines) + 8
        if risk_lines:
            box_h += 14 + 17 * len(risk_lines) + 14
        card_box(y, box_h)
        ty = y + 12
        for head, lines in blocks:
            d.text((PAD + 14, ty), head, font=f(12.5, True), fill=INK)
            ty += 19
            for ln in lines:
                d.text((PAD + 14, ty), ln, font=f(12), fill=SUB); ty += 17
            ty += 8
        if risk_lines:
            rh = 14 + 17 * len(risk_lines)
            d.rounded_rectangle([PAD + 14, ty, W - PAD - 14, ty + rh], 8, fill=WARN_BG)
            d.rectangle([PAD + 14, ty + 4, PAD + 17, ty + rh - 4], fill=WARN)
            ly2 = ty + 8
            for ln in risk_lines:
                d.text((PAD + 26, ly2), ln, font=f(11.5), fill=WARN); ly2 += 17
            ty += rh + 14
        y += box_h

    # ── 면책 ─────────────────────────────────────────────
    y += 16
    d.line([PAD, y, W - PAD, y], fill=LINE)
    y += 8
    foot = data.get("footer") or ("정보 제공 목적이며 투자 권유가 아닙니다. "
                                  "투자 판단과 그 결과에 대한 책임은 투자자 본인에게 있습니다.")
    for ln in _wrap(d, foot, f(10), W - 2 * PAD)[:3]:
        d.text((PAD, y), ln, font=f(10), fill=MUTED); y += 14
    y += 12

    img = img.crop((0, 0, W, min(int(y), 3000)))
    img = img.convert("P", palette=Image.ADAPTIVE, colors=64)
    img.save(out_path, optimize=True)
    return out_path


if __name__ == "__main__":
    import json
    src = sys.argv[1] if len(sys.argv) > 1 else None
    sample = {
        "title": "투자 브리핑", "date": "2026.08.17 (월)",
        "subtitle": "시세 08.14(금) KRX 종가",
        "pnl": 6672150, "pnl_pct": 11.37, "value": 65362500,
        "portfolio_note": "3종목 100% 반도체·전자부품 — 분산 효과 없음",
        "market": [["KOSPI", "6,977.94", "+2.42%", "up"],
                   ["S&P500", "7,785.76", "-0.17%", "down"],
                   ["SOX", "12,417.05", "-0.31%", "down"],
                   ["브렌트", "$88.52", "+1.67%", "up"],
                   ["WTI", "$82.40", "+1.42%", "up"],
                   ["원/달러", "1,418.3", "-1.1", "down"]],
        "holdings": [
            {"name": "삼성전자", "qty": 135, "close": 274500, "pnl": 1429515,
             "pct": 4.01, "weight": 56.7, "color": "#2E6FD9", "note": "고점 대비 -26.7%"},
            {"name": "삼성전기", "qty": 15, "close": 1558000, "pnl": 5189100,
             "pct": 28.54, "weight": 35.8, "color": "#C07A1E", "note": "손익 기여 77.8%"},
            {"name": "SK하이닉스", "qty": 3, "close": 1645000, "pnl": 53535,
             "pct": 1.10, "weight": 7.6, "color": "#158A6E", "note": "고점 대비 -44.9%"}],
        "checkpoints": [
            ["8/17 호르무즈 MOU 시한 만료",
             "이란은 \"연장할 휴전이 없었다\"며 부인, 중재측은 연장 합의를 전해 정면 충돌. 결렬 시 브렌트 $100 재돌파 가능."],
            ["8/18 한국장 연휴 후 개장", "8/15~17 휴장분이 갭으로 한 번에 반영될 수 있습니다."],
            ["8/26~27 엔비디아 실적", "AI 수요 최종 확인 이벤트로 3종목 전부에 직결됩니다."]],
        "risk": "삼성전기 단일 종목이 전체 수익의 77.8%. 이 종목 -20% 시 포트폴리오 수익의 약 30%가 증발합니다.",
    }
    data = json.load(open(src, encoding="utf-8")) if src else sample
    p = render_card(data, "card_preview.png")
    print("생성:", p, os.path.getsize(p) // 1024, "KB")
