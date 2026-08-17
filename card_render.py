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

# 섹션 이름 → (색, 아이콘). 이름이 안 맞으면 순번 색 + 기본 마커.
SECTION_PRESET = (
    (("국제", "세계", "글로벌", "해외"), "#2E6FD9", "globe"),
    (("정치", "외교", "안보"), "#7A4FBF", "gov"),
    (("경제", "금융", "산업", "시장"), "#158A6E", "chart"),
    (("스포츠", "체육"), "#B4531A", "ball"),
    (("연예", "문화", "엔터"), "#C07A1E", "star"),
    (("캄보디아", "프놈펜", "현지"), "#2B6CB0", "pin"),
)


def _preset(name, idx):
    n = str(name or "")
    for keys, col, icon in SECTION_PRESET:
        if any(k in n for k in keys):
            return _hex(col), icon
    return _hex(SECTION_COLORS[idx % len(SECTION_COLORS)]), "dot"


def _tint(rgb, ratio, base=WHITE):
    """base 위에 얹은 옅은 색. ratio 0=base, 1=원색."""
    return tuple(int(round(b + (c - b) * ratio)) for c, b in zip(rgb, base))


def _icon(d, kind, cx, cy, r, col=WHITE, bg=NAVY):
    """배지 안에 들어가는 단순 픽토그램."""
    lw = max(2, int(round(r / 5.0)))
    if kind == "globe":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=lw)
        d.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r], outline=col, width=max(1, lw - 1))
        d.line([cx - r, cy, cx + r, cy], fill=col, width=max(1, lw - 1))
    elif kind == "gov":          # 깃발
        d.rectangle([cx - r * 0.62, cy - r * 0.95,
                     cx - r * 0.62 + max(2, lw * 0.9), cy + r * 0.95], fill=col)
        d.polygon([(cx - r * 0.34, cy - r * 0.88), (cx + r * 0.85, cy - r * 0.45),
                   (cx - r * 0.34, cy - r * 0.02)], fill=col)
    elif kind == "chart":
        bw = r * 0.46
        for i, hgt in enumerate((0.5, 0.78, 1.0)):
            x0 = cx - r * 0.92 + i * (bw + r * 0.24)
            d.rectangle([x0, cy + r * 0.8 - r * 1.7 * hgt, x0 + bw, cy + r * 0.8], fill=col)
    elif kind == "ball":         # 트로피
        d.polygon([(cx - r * 0.62, cy - r * 0.9), (cx + r * 0.62, cy - r * 0.9),
                   (cx + r * 0.4, cy + r * 0.1), (cx - r * 0.4, cy + r * 0.1)], fill=col)
        d.rectangle([cx - r * 0.14, cy + r * 0.05, cx + r * 0.14, cy + r * 0.55], fill=col)
        d.rectangle([cx - r * 0.55, cy + r * 0.55, cx + r * 0.55, cy + r * 0.9], fill=col)
    elif kind == "star":
        pts = []
        for i in range(10):
            rad = math.radians(-90 + i * 36)
            rr = r if i % 2 == 0 else r * 0.44
            pts.append((cx + rr * math.cos(rad), cy + rr * math.sin(rad)))
        d.polygon(pts, fill=col)
    elif kind == "pin":
        d.ellipse([cx - r * 0.78, cy - r * 0.95, cx + r * 0.78, cy + r * 0.45], fill=col)
        d.polygon([(cx - r * 0.4, cy + r * 0.2), (cx + r * 0.4, cy + r * 0.2),
                   (cx, cy + r)], fill=col)
        d.ellipse([cx - r * 0.26, cy - r * 0.5, cx + r * 0.26, cy + r * 0.02], fill=bg)
    else:
        d.ellipse([cx - r * 0.5, cy - r * 0.5, cx + r * 0.5, cy + r * 0.5], fill=col)


def _delta_mark(d, x, y, s, direction, col):
    """▲ ▼ ─ 를 폰트에 의존하지 않고 직접 그립니다."""
    if direction == "up":
        d.polygon([(x + s / 2.0, y), (x, y + s), (x + s, y + s)], fill=col)
    elif direction == "down":
        d.polygon([(x, y), (x + s, y), (x + s / 2.0, y + s)], fill=col)
    else:
        d.rectangle([x, y + s * 0.38, x + s, y + s * 0.62], fill=col)


def _clip(lines, limit):
    """줄 수 제한. 잘렸으면 말줄임표를 붙입니다."""
    if len(lines) <= limit:
        return lines
    out = list(lines[:limit])
    if out and len(out[-1]) > 1:
        out[-1] = out[-1][:-1] + "…"
    return out


def render_news_card(data, out_path="card.png"):
    """섹션형 종합 뉴스 카드 (인포그래픽)."""
    f = F()
    w, pad = 720, 20
    img = Image.new("RGB", (w, 6000), PAPER)
    d = ImageDraw.Draw(img)

    sections = data.get("sections") or []
    metrics = data.get("metrics") or []
    total_items = sum(len(s.get("items") or []) for s in sections)
    inner_w = w - 2 * pad

    # ── 헤더 ─────────────────────────────────────────────
    head = str(data.get("headline") or "").strip()
    head_lines = _clip(_wrap(d, head, f(14, True), inner_w - 30), 2) if head else []
    hh = 102 + (len(head_lines) * 21 + 16 if head_lines else 0)
    d.rectangle([0, 0, w, hh], fill=NAVY)
    d.rectangle([0, 0, w, 4], fill=GOLD)
    d.text((pad + 2, 22), "DAILY NEWS BRIEFING", font=f(10.5, True), fill=GOLD)
    d.text((pad + 2, 39), data.get("title", "종합 브리핑"), font=f(25, True), fill=WHITE)
    sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
    if sub:
        d.text((pad + 2, 75), sub, font=f(11.5), fill=HEAD_SUB)
    if sections:
        badge = "%d개 분야 · %d건" % (len(sections), total_items)
        bw = d.textlength(badge, font=f(11, True)) + 24
        d.rounded_rectangle([w - pad - bw, 34, w - pad, 60], 13, fill=(31, 68, 103))
        d.text((w - pad - bw + 12, 40), badge, font=f(11, True), fill=WHITE)
    if head_lines:
        hy = 98
        d.rectangle([pad + 2, hy + 4, pad + 6, hy + len(head_lines) * 21 - 3], fill=HERO)
        for i, ln in enumerate(head_lines):
            d.text((pad + 17, hy + i * 21), ln, font=f(14, True), fill=HERO)
    y = hh + 16

    # ── 오늘의 지표 스트립 ───────────────────────────────
    if metrics:
        m = list(metrics)[:4]
        gap, th = 10, 70
        tw_ = (inner_w - gap * (len(m) - 1)) / float(len(m))
        for i, mt in enumerate(m):
            if not isinstance(mt, dict):
                mt = {"label": str(mt)}
            x0 = pad + i * (tw_ + gap)
            d.rounded_rectangle([x0, y, x0 + tw_, y + th], 10, fill=WHITE, outline=LINE)
            delta = str(mt.get("delta") or "")
            dirn = str(mt.get("dir") or "").lower()
            if not dirn:
                dirn = "up" if delta.startswith("+") else ("down" if delta.startswith("-") else "flat")
            col = UP if dirn == "up" else DOWN if dirn == "down" else MUTED
            d.rounded_rectangle([x0, y + 13, x0 + 3, y + th - 13], 2, fill=col)
            d.text((x0 + 14, y + 10), str(mt.get("label") or ""), font=f(10.5), fill=MUTED)
            d.text((x0 + 14, y + 25), str(mt.get("value") or ""), font=f(18, True), fill=INK)
            if delta:
                _delta_mark(d, x0 + 14, y + 53, 8, dirn, col)
                d.text((x0 + 26, y + 50), delta, font=f(11, True), fill=col)
        y += th + 16

    # ── 섹션 ─────────────────────────────────────────────
    for idx, sec in enumerate(sections):
        name = str(sec.get("name", "") or "")
        col, icon = _preset(name, idx)
        if sec.get("color"):
            col = _hex(sec["color"], col)
        icon = str(sec.get("icon") or icon)

        norm = []
        for it in (sec.get("items") or []):
            if isinstance(it, dict):
                norm.append((str(it.get("head") or ""), str(it.get("body") or ""),
                             str(it.get("tag") or "")[:3], bool(it.get("key"))))
            elif isinstance(it, (list, tuple)):
                a = list(it) + ["", "", ""]
                norm.append((str(a[0] or ""), str(a[1] or ""), str(a[2] or "")[:3], False))
            else:
                norm.append((str(it), "", "", False))

        hf, bf, tf = f(13.5, True), f(11.5), f(9.5, True)
        tx = pad + 62
        tw = w - pad - 18 - tx
        rows = []
        for h_t, b_t, tag, key in norm:
            hl = _clip(_wrap(d, h_t, hf, tw), 2)
            bl = _clip(_wrap(d, b_t, bf, tw), 3) if b_t else []
            rh = 11 + len(hl) * 19 + (len(bl) * 17 + 3 if bl else 0) + 12
            rows.append((hl, bl, tag, key, rh))

        hdr_h = 42
        total_h = hdr_h + sum(r[4] for r in rows) + 2

        d.rounded_rectangle([pad, y, w - pad, y + total_h], 12, fill=WHITE, outline=LINE)
        band = _tint(col, 0.10)
        d.rounded_rectangle([pad + 1, y + 1, w - pad - 1, y + hdr_h], 12, fill=band)
        d.rectangle([pad + 1, y + hdr_h - 12, w - pad - 1, y + hdr_h], fill=band)
        d.line([pad + 1, y + hdr_h, w - pad - 1, y + hdr_h], fill=_tint(col, 0.30))

        cy = y + hdr_h / 2.0
        d.ellipse([pad + 16, cy - 15, pad + 46, cy + 15], fill=col)
        _icon(d, icon, pad + 31, cy, 8.5, WHITE, col)
        d.text((pad + 56, cy - 10), name, font=f(14.5, True), fill=col)
        cnt = "%d건" % len(rows)
        d.text((w - pad - 16 - d.textlength(cnt, font=f(10.5, True)), cy - 7),
               cnt, font=f(10.5, True), fill=_tint(col, 0.55, MUTED))

        ry = y + hdr_h + 1
        for i, (hl, bl, tag, key, rh) in enumerate(rows):
            if key:
                d.rectangle([pad + 1, ry, w - pad - 1, ry + rh], fill=_tint(col, 0.06))
                d.rectangle([pad + 1, ry, pad + 5, ry + rh], fill=col)
            elif i:
                d.line([tx, ry, w - pad - 18, ry], fill=LINE)
            ty = ry + 11
            if tag:
                twd = d.textlength(tag, font=tf)
                pw = min(max(twd + 14, 30), 44)
                d.rounded_rectangle([pad + 16, ty + 1, pad + 16 + pw, ty + 18], 8,
                                    fill=_tint(col, 0.17))
                d.text((pad + 16 + (pw - twd) / 2.0, ty + 4), tag, font=tf, fill=col)
            else:
                d.ellipse([pad + 26, ty + 6, pad + 34, ty + 14], fill=col)
            for j, ln in enumerate(hl):
                d.text((tx, ty + j * 19), ln, font=hf, fill=INK)
            ty += len(hl) * 19
            if bl:
                ty += 3
                for ln in bl:
                    d.text((tx, ty), ln, font=bf, fill=SUB)
                    ty += 17
            ry += rh
        y += total_h + 14

    # ── 꼬리말 ───────────────────────────────────────────
    y += 2
    d.line([pad, y, w - pad, y], fill=LINE)
    y += 10
    foot = data.get("footer") or "공개된 언론 보도를 정리한 참고 자료입니다."
    for ln in _clip(_wrap(d, foot, f(10), inner_w), 3):
        d.text((pad, y), ln, font=f(10), fill=MUTED)
        y += 14
    y += 14

    img = img.crop((0, 0, w, min(int(y), 6000)))
    img = img.convert("P", palette=Image.ADAPTIVE, colors=96)
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
