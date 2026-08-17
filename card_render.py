#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""리포트 카드 PNG 렌더러 (모바일 가독성 판)

Claude 가 메일에 담아 보낸 수치(JSON)를 받아 릴레이 호스트에서 카드 이미지를
직접 그립니다. 이미지 바이트가 모델 컨텍스트를 거치지 않으므로 용량 문제가 없습니다.

모바일 최적화 두 가지
  1) 논리 폭을 560px 로 좁히고 글자를 키워, 화면 폭 대비 글자 비율을 높였습니다.
     (본문 기준 폭의 1.6% → 2.5%. 텔레그램은 이미지를 말풍선 폭에 맞춰 축소하므로
      절대 픽셀 수가 아니라 이 비율이 체감 크기를 결정합니다.)
  2) SCALE 배로 실제 렌더해 고해상도 화면에서 흐려지지 않게 했습니다.

필요: pip install pillow
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ── 색 (검증 통과 팔레트) ──────────────────────────────────
NAVY = (18, 48, 78)
NAVY_2 = (31, 68, 103)
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

# ── 치수 ──────────────────────────────────────────────────
W = 560          # 논리 폭. 좁을수록 글자가 상대적으로 커집니다
PAD = 16
SCALE = 2        # 실제 출력 배율 (560 → 1120px)

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
    raise RuntimeError("한글 폰트를 찾지 못했습니다. 나눔고딕 또는 맑은 고딕이 필요합니다.")


class F:
    """논리 크기를 주면 SCALE 배로 확대한 폰트를 돌려줍니다."""

    def __init__(self, scale=SCALE):
        reg, bold = _font_paths()
        self._reg, self._bold = reg, bold
        self._scale = scale
        self._cache = {}

    def __call__(self, size, bold=False):
        key = (size, bold)
        if key not in self._cache:
            px = max(1, int(round(size * self._scale)))
            self._cache[key] = ImageFont.truetype(self._bold if bold else self._reg, px)
        return self._cache[key]


class S:
    """논리 좌표로 그리면 실제로는 SCALE 배로 그려주는 얇은 래퍼.

    덕분에 배치 계산은 전부 560px 기준 한 벌만 유지하면 됩니다.
    """

    def __init__(self, draw, scale=SCALE):
        self.d = draw
        self.s = scale

    def _p(self, xy):
        s = self.s
        if isinstance(xy, (list, tuple)):
            return [tuple(v * s for v in p) if isinstance(p, (list, tuple)) else p * s
                    for p in xy]
        return xy

    def _k(self, k):
        if k.get("width"):
            k = dict(k)
            k["width"] = max(1, int(round(k["width"] * self.s)))
        return k

    def text(self, xy, *a, **k):
        self.d.text(self._p(xy), *a, **k)

    def rectangle(self, xy, **k):
        self.d.rectangle(self._p(xy), **self._k(k))

    def rounded_rectangle(self, xy, radius=0, **k):
        self.d.rounded_rectangle(self._p(xy), radius * self.s, **self._k(k))

    def ellipse(self, xy, **k):
        self.d.ellipse(self._p(xy), **self._k(k))

    def line(self, xy, **k):
        self.d.line(self._p(xy), **self._k(k))

    def polygon(self, xy, **k):
        self.d.polygon(self._p(xy), **k)

    def pieslice(self, xy, start, end, **k):
        self.d.pieslice(self._p(xy), start, end, **k)

    def textlength(self, text, font=None):
        return self.d.textlength(text, font=font) / float(self.s)


# ── 작은 도구들 ───────────────────────────────────────────
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


def _tint(rgb, ratio, base=WHITE):
    """base 위에 얹은 옅은 색. ratio 0=base, 1=원색."""
    return tuple(int(round(b + (c - b) * ratio)) for c, b in zip(rgb, base))


def _won(n):
    try:
        return "{:,}".format(int(round(float(n))))
    except (TypeError, ValueError):
        return str(n)


def _wrap(d, text, font, max_w):
    """글자 단위 줄바꿈 (한국어는 공백이 드물어 단어 단위로는 부족)."""
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        trial = cur + ch
        if d.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines


def _clip(lines, limit):
    if len(lines) <= limit:
        return lines
    out = list(lines[:limit])
    if out and len(out[-1]) > 1:
        out[-1] = out[-1][:-1] + "…"
    return out


def _opinion_color(op):
    """투자의견 → 색. 한국식(매수=빨강, 매도=파랑)."""
    o = str(op or "")
    if "매수" in o:
        return UP
    if "매도" in o or "축소" in o or "익절" in o:
        return DOWN
    if "관망" in o:
        return MUTED
    return SUB


def _delta_mark(d, x, y, s, direction, col):
    """▲ ▼ ─ 를 폰트에 의존하지 않고 직접 그립니다."""
    if direction == "up":
        d.polygon([(x + s / 2.0, y), (x, y + s), (x + s, y + s)], fill=col)
    elif direction == "down":
        d.polygon([(x, y), (x + s, y), (x + s / 2.0, y + s)], fill=col)
    else:
        d.rectangle([x, y + s * 0.38, x + s, y + s * 0.62], fill=col)


# ── 섹션 색·아이콘 ────────────────────────────────────────
SECTION_COLORS = ["#2E6FD9", "#C07A1E", "#158A6E", "#7A4FBF", "#B4531A", "#2B6CB0"]

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


def _icon(d, kind, cx, cy, r, col=WHITE, bg=NAVY):
    """배지 안 픽토그램."""
    lw = max(2, r / 5.0)
    if kind == "globe":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=lw)
        d.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r], outline=col, width=lw * 0.7)
        d.line([cx - r, cy, cx + r, cy], fill=col, width=lw * 0.7)
    elif kind == "gov":                # 깃발
        d.rectangle([cx - r * 0.62, cy - r * 0.95, cx - r * 0.62 + lw, cy + r * 0.95], fill=col)
        d.polygon([(cx - r * 0.34, cy - r * 0.88), (cx + r * 0.85, cy - r * 0.45),
                   (cx - r * 0.34, cy - r * 0.02)], fill=col)
    elif kind == "chart":
        bw = r * 0.46
        for i, hgt in enumerate((0.5, 0.78, 1.0)):
            x0 = cx - r * 0.92 + i * (bw + r * 0.24)
            d.rectangle([x0, cy + r * 0.8 - r * 1.7 * hgt, x0 + bw, cy + r * 0.8], fill=col)
    elif kind == "ball":               # 트로피
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


def _canvas(height):
    img = Image.new("RGB", (W * SCALE, height * SCALE), PAPER)
    return img, S(ImageDraw.Draw(img))


def _finish(img, y, out_path, colors=110):
    y = int(min(y, img.height / SCALE))
    img = img.crop((0, 0, W * SCALE, y * SCALE))
    img = img.convert("P", palette=Image.ADAPTIVE, colors=colors)
    img.save(out_path, optimize=True)
    return out_path


# ═════════════════════════════════════════════════════════
# 종합 뉴스 카드
# ═════════════════════════════════════════════════════════
def render_news_card(data, out_path="card.png"):
    f = F()
    img, d = _canvas(4000)

    sections = data.get("sections") or []
    metrics = data.get("metrics") or []
    total = sum(len(s.get("items") or []) for s in sections)
    iw = W - 2 * PAD

    # ── 헤더 ──
    head = str(data.get("headline") or "").strip()
    hl = _clip(_wrap(d, head, f(15.5, True), iw - 26), 2) if head else []
    hh = 118 + (len(hl) * 24 + 16 if hl else 0)
    d.rectangle([0, 0, W, hh], fill=NAVY)
    d.rectangle([0, 0, W, 5], fill=GOLD)
    d.text((PAD, 24), "DAILY NEWS BRIEFING", font=f(11, True), fill=GOLD)
    d.text((PAD, 44), data.get("title", "종합 브리핑"), font=f(27, True), fill=WHITE)
    sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
    if sub:
        d.text((PAD, 84), sub, font=f(12.5), fill=HEAD_SUB)
    if sections:
        badge = "%d개 분야 · %d건" % (len(sections), total)
        bw = d.textlength(badge, font=f(12, True)) + 26
        d.rounded_rectangle([W - PAD - bw, 40, W - PAD, 68], 14, fill=NAVY_2)
        d.text((W - PAD - bw + 13, 46), badge, font=f(12, True), fill=WHITE)
    if hl:
        hy = 112
        d.rectangle([PAD, hy + 4, PAD + 5, hy + len(hl) * 24 - 4], fill=HERO)
        for i, ln in enumerate(hl):
            d.text((PAD + 15, hy + i * 24), ln, font=f(15.5, True), fill=HERO)
    y = hh + 16

    # ── 지표 스트립 (2열) ──
    if metrics:
        m = list(metrics)[:4]
        gap, th = 10, 74
        cols = 2 if len(m) > 2 else len(m)
        tw = (iw - gap * (cols - 1)) / float(cols)
        for i, mt in enumerate(m):
            if not isinstance(mt, dict):
                mt = {"label": str(mt)}
            x0 = PAD + (i % cols) * (tw + gap)
            ty = y + (i // cols) * (th + gap)
            d.rounded_rectangle([x0, ty, x0 + tw, ty + th], 12, fill=WHITE, outline=LINE)
            delta = str(mt.get("delta") or "")
            dirn = str(mt.get("dir") or "").lower()
            if not dirn:
                dirn = "up" if delta.startswith("+") else ("down" if delta.startswith("-") else "flat")
            col = UP if dirn == "up" else DOWN if dirn == "down" else MUTED
            d.rounded_rectangle([x0, ty + 14, x0 + 4, ty + th - 14], 2, fill=col)
            d.text((x0 + 16, ty + 12), str(mt.get("label") or ""), font=f(12), fill=MUTED)
            d.text((x0 + 16, ty + 30), str(mt.get("value") or ""), font=f(21, True), fill=INK)
            if delta:
                _delta_mark(d, x0 + 16, ty + 58, 9, dirn, col)
                d.text((x0 + 30, ty + 54), delta, font=f(12.5, True), fill=col)
        rows = (len(m) + cols - 1) // cols
        y += rows * (th + gap) + 8

    # ── 섹션 ──
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

        hf, bf, tf = f(15, True), f(13, False), f(11, True)
        gx, gw = PAD + 12, 44          # 태그 배지 자리
        tx = gx + gw + 12
        tw = W - PAD - 12 - tx

        rows = []
        for h_t, b_t, tag, key in norm:
            hls = _clip(_wrap(d, h_t, hf, tw), 2)
            bls = _clip(_wrap(d, b_t, bf, tw), 3) if b_t else []
            rh = 13 + len(hls) * 21 + (len(bls) * 19 + 4 if bls else 0) + 13
            rows.append((hls, bls, tag, key, rh))

        hdr_h = 48
        total_h = hdr_h + sum(r[4] for r in rows) + 2

        d.rounded_rectangle([PAD, y, W - PAD, y + total_h], 14, fill=WHITE, outline=LINE)
        band = _tint(col, 0.10)
        d.rounded_rectangle([PAD + 1, y + 1, W - PAD - 1, y + hdr_h], 14, fill=band)
        d.rectangle([PAD + 1, y + hdr_h - 14, W - PAD - 1, y + hdr_h], fill=band)
        d.line([PAD + 1, y + hdr_h, W - PAD - 1, y + hdr_h], fill=_tint(col, 0.30))

        cy = y + hdr_h / 2.0
        d.ellipse([PAD + 14, cy - 16, PAD + 46, cy + 16], fill=col)
        _icon(d, icon, PAD + 30, cy, 9, WHITE, col)
        d.text((PAD + 58, cy - 11), name, font=f(16.5, True), fill=col)
        cnt = "%d건" % len(rows)
        d.text((W - PAD - 14 - d.textlength(cnt, font=f(11.5, True)), cy - 8),
               cnt, font=f(11.5, True), fill=_tint(col, 0.6, MUTED))

        ry = y + hdr_h + 1
        for i, (hls, bls, tag, key, rh) in enumerate(rows):
            if key:
                d.rectangle([PAD + 1, ry, W - PAD - 1, ry + rh], fill=_tint(col, 0.07))
                d.rectangle([PAD + 1, ry, PAD + 6, ry + rh], fill=col)
            elif i:
                d.line([tx, ry, W - PAD - 12, ry], fill=LINE)
            ty = ry + 13
            if tag:
                twd = d.textlength(tag, font=tf)
                d.rounded_rectangle([gx, ty + 1, gx + gw, ty + 21], 10, fill=_tint(col, 0.17))
                d.text((gx + (gw - twd) / 2.0, ty + 4), tag, font=tf, fill=col)
            else:
                d.ellipse([gx + gw / 2 - 5, ty + 6, gx + gw / 2 + 5, ty + 16], fill=col)
            for j, ln in enumerate(hls):
                d.text((tx, ty + j * 21), ln, font=hf, fill=INK)
            ty += len(hls) * 21
            if bls:
                ty += 4
                for ln in bls:
                    d.text((tx, ty), ln, font=bf, fill=SUB)
                    ty += 19
            ry += rh
        y += total_h + 14

    # ── 꼬리말 ──
    y += 4
    d.line([PAD, y, W - PAD, y], fill=LINE)
    y += 12
    foot = data.get("footer") or "공개된 언론 보도를 정리한 참고 자료입니다."
    for ln in _clip(_wrap(d, foot, f(11), iw), 3):
        d.text((PAD, y), ln, font=f(11), fill=MUTED)
        y += 16
    y += 16
    return _finish(img, y, out_path)


# ═════════════════════════════════════════════════════════
# 투자 브리핑 카드
# ═════════════════════════════════════════════════════════
def render_card(data, out_path="card.png"):
    """수치 dict → 카드 PNG. data["type"]=="news" 면 뉴스 카드로 넘깁니다."""
    if str(data.get("type", "")).lower() == "news" or data.get("sections"):
        return render_news_card(data, out_path)

    f = F()
    img, d = _canvas(4000)
    iw = W - 2 * PAD

    holdings = data.get("holdings") or []
    market = data.get("market") or []
    watch = data.get("watchlist") or []
    checks = data.get("checkpoints") or []

    # ── 헤더 ──
    pnl = data.get("pnl")
    hh = 190 if pnl is not None else 120
    d.rectangle([0, 0, W, hh], fill=NAVY)
    d.rectangle([0, 0, W, 5], fill=GOLD)
    d.text((PAD, 24), "PORTFOLIO INTELLIGENCE REPORT", font=f(11, True), fill=GOLD)
    d.text((PAD, 44), data.get("title", "투자 브리핑"), font=f(27, True), fill=WHITE)
    sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
    if sub:
        for i, ln in enumerate(_clip(_wrap(d, sub, f(12.5), iw), 2)):
            d.text((PAD, 84 + i * 18), ln, font=f(12.5), fill=HEAD_SUB)

    if pnl is not None:
        d.text((PAD, 122), "총 평가손익", font=f(12), fill=HEAD_SUB)
        sign = "+" if float(pnl) >= 0 else ""
        big = "%s%s" % (sign, _won(pnl))
        d.text((PAD, 140), big, font=f(33, True), fill=HERO)
        bx = PAD + d.textlength(big, font=f(33, True)) + 7
        d.text((bx, 158), "원", font=f(15, True), fill=HERO)
        bx += d.textlength("원", font=f(15, True)) + 14
        pct = "%s%.2f%%" % (sign, float(data.get("pnl_pct", 0)))
        d.text((bx, 156), pct, font=f(17, True), fill=HERO)
        if data.get("value") is not None:
            vt = _won(data["value"]) + "원"
            d.text((W - PAD - d.textlength("평가금액", font=f(12)), 122),
                   "평가금액", font=f(12), fill=HEAD_SUB)
            d.text((W - PAD - d.textlength(vt, font=f(18, True)), 141),
                   vt, font=f(18, True), fill=WHITE)
    y = hh + 14

    def section(title, yy):
        d.rounded_rectangle([PAD, yy + 2, PAD + 5, yy + 20], 2, fill=GOLD)
        d.text((PAD + 14, yy), title, font=f(16, True), fill=INK)
        return yy + 32

    def box(yy, height):
        d.rounded_rectangle([PAD, yy, W - PAD, yy + height], 14, fill=WHITE, outline=LINE)

    # ── 시장 지표 (2열) ──
    if market:
        gap, th = 10, 70
        tw = (iw - gap) / 2.0
        for i, row in enumerate(market[:6]):
            k, v, chg, dirn = (list(row) + ["", "", ""])[:4]
            x0 = PAD + (i % 2) * (tw + gap)
            ty = y + (i // 2) * (th + gap)
            d.rounded_rectangle([x0, ty, x0 + tw, ty + th], 12, fill=WHITE, outline=LINE)
            col = UP if str(dirn).lower() == "up" else DOWN
            d.rounded_rectangle([x0, ty + 13, x0 + 4, ty + th - 13], 2, fill=col)
            d.text((x0 + 16, ty + 11), str(k), font=f(12), fill=MUTED)
            d.text((x0 + 16, ty + 29), str(v), font=f(20, True), fill=INK)
            if str(chg):
                d.text((x0 + 16, ty + 53), str(chg), font=f(12.5, True), fill=col)
        y += ((len(market[:6]) + 1) // 2) * (th + gap) + 10

    # ── 포트폴리오 도넛 ──
    if holdings and any(h.get("weight") for h in holdings):
        y += 8
        y = section("포트폴리오", y)
        bh = 44 + 34 * len(holdings) + (36 if data.get("portfolio_note") else 0)
        bh = max(bh, 168)
        box(y, bh)
        cx, cy, r, thick = PAD + 78, y + 80, 54, 21
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
            d.text((cx - d.textlength(t1, font=f(11, True)) / 2, cy - 17), t1,
                   font=f(11, True), fill=SUB)
            t2 = _won(float(data["value"]) / 10000) + "만"
            d.text((cx - d.textlength(t2, font=f(16, True)) / 2, cy - 2), t2,
                   font=f(16, True), fill=INK)
        lx, ly = PAD + 152, y + 26
        for h in holdings:
            d.rounded_rectangle([lx, ly + 4, lx + 12, ly + 16], 3, fill=_hex(h.get("color")))
            d.text((lx + 20, ly), str(h.get("name", "")), font=f(14.5, True), fill=INK)
            pct = "%.1f%%" % float(h.get("weight") or 0)
            d.text((W - PAD - 16 - d.textlength(pct, font=f(14, True)), ly), pct,
                   font=f(14, True), fill=SUB)
            ly += 34
        note = data.get("portfolio_note")
        if note:
            ly += 2
            d.line([lx, ly, W - PAD - 16, ly], fill=LINE)
            ly += 8
            for ln in _clip(_wrap(d, note, f(12), W - PAD - 20 - lx), 2):
                d.text((lx, ly), ln, font=f(12), fill=SUB); ly += 17
        y += bh + 8

    # ── 종목별 수익률 ──
    if holdings and any(h.get("pct") is not None for h in holdings):
        y += 8
        y = section("종목별 수익률", y)
        items = sorted([h for h in holdings if h.get("pct") is not None],
                       key=lambda x: -float(x["pct"]))
        rows = [(h.get("name", ""), float(h["pct"]), _hex(h.get("color"))) for h in items]
        if data.get("pnl_pct") is not None:
            rows.append(("전체", float(data["pnl_pct"]), NAVY))
        bh = 22 + 38 * len(rows)
        box(y, bh)
        x0, x1 = PAD + 92, W - PAD - 86
        vmax = max([abs(v) for _, v, _ in rows] + [0.01]) * 1.12
        by = y + 16
        for nm, v, col in rows:
            d.text((x0 - 12 - d.textlength(nm, font=f(13, True)), by + 1), nm,
                   font=f(13, True), fill=INK)
            bw = max((x1 - x0) * abs(v) / vmax, 5)
            d.rounded_rectangle([x0, by, x0 + bw, by + 18], 5, fill=col)
            lab = "%s%.2f%%" % ("+" if v >= 0 else "", v)
            d.text((x0 + bw + 9, by + 2), lab, font=f(13, True), fill=col)
            by += 38
        y += bh + 8

    # ── 보유 종목 · 의견 ──
    if holdings:
        y += 8
        y = section("보유 종목 · 의견", y)
        tx = PAD + 16
        tw = W - PAD - 16 - tx
        rows = []
        for h in holdings:
            parts = []
            cl = float(h["close"]) if h.get("close") else None
            for key, lb in (("target_s", "단기"), ("target_m", "중기")):
                if h.get(key) is not None:
                    t = float(h[key])
                    up = " (%+.0f%%)" % ((t / cl - 1) * 100) if cl else ""
                    parts.append("%s %s%s" % (lb, _won(t), up))
            rl = _clip(_wrap(d, str(h.get("reason") or ""), f(12), tw), 3) if h.get("reason") else []
            op = h.get("opinion")
            tline = 26 if (op or parts) else 0
            rh = 52 + tline + (len(rl) * 17 + 4 if rl else 0) + 14
            rows.append((h, parts, rl, op, rh))
        bh = 12 + sum(r[4] for r in rows)
        box(y, bh)
        ry = y + 12
        for i, (h, parts, rl, op, rh) in enumerate(rows):
            col = _hex(h.get("color"))
            d.rounded_rectangle([tx, ry + 4, tx + 6, ry + 18], 2, fill=col)
            nx = tx + 15
            nm = str(h.get("name", ""))
            d.text((nx, ry), nm, font=f(15.5, True), fill=INK)
            nx += d.textlength(nm, font=f(15.5, True)) + 7
            if h.get("qty"):
                d.text((nx, ry + 4), "%s주" % h["qty"], font=f(11.5), fill=MUTED)
            hp = h.get("pnl")
            if hp is not None:
                pcol = UP if float(hp) >= 0 else DOWN
                sign = "+" if float(hp) >= 0 else ""
                pl = "%s%.2f%%" % (sign, float(h.get("pct") or 0))
                d.text((W - PAD - 16 - d.textlength(pl, font=f(15, True)), ry),
                       pl, font=f(15, True), fill=pcol)
                am = "%s%s원" % (sign, _won(hp))
                d.text((W - PAD - 16 - d.textlength(am, font=f(11.5)), ry + 21),
                       am, font=f(11.5), fill=pcol)
            sub2 = " · ".join(str(x) for x in (
                (_won(h["close"]) + "원") if h.get("close") else None, h.get("note")) if x)
            if sub2:
                for ln in _clip(_wrap(d, sub2, f(12), tw - 96), 1):
                    d.text((tx + 15, ry + 22), ln, font=f(12), fill=SUB)
            ly = ry + 48
            if op or parts:
                bx = tx + 15
                if op:
                    ocol = _opinion_color(op)
                    ow = d.textlength(str(op), font=f(12.5, True)) + 22
                    d.rounded_rectangle([bx, ly, bx + ow, ly + 24], 11, fill=_tint(ocol, 0.15))
                    d.text((bx + 11, ly + 4), str(op), font=f(12.5, True), fill=ocol)
                    bx += ow + 10
                if parts:
                    txt = "  ·  ".join(parts)
                    if d.textlength(txt, font=f(12.5, True)) > (W - PAD - 16 - bx):
                        txt = "  ·  ".join(p.replace("단기 ", "").replace("중기 ", "")
                                          for p in parts)
                    d.text((bx, ly + 6), txt, font=f(12.5, True), fill=INK)
                ly += 26
            if rl:
                ly += 4
                for ln in rl:
                    d.text((tx + 15, ly), ln, font=f(12), fill=MUTED)
                    ly += 17
            if i < len(rows) - 1:
                d.line([tx, ry + rh - 8, W - PAD - 16, ry + rh - 8], fill=LINE)
            ry += rh
        y += bh + 8

    # ── 주목할 저평가 종목 ──
    if watch:
        y += 8
        y = section("주목할 저평가 종목", y)
        px, pw = PAD + 16, 46
        tx = px + pw + 12
        tw = W - PAD - 16 - tx
        rows = []
        for wt in watch:
            tg = [str(x) for x in (wt.get("target_s"), wt.get("target_m")) if x]
            nl = _clip(_wrap(d, str(wt.get("note") or ""), f(12), W - PAD - 16 - px), 3) \
                if wt.get("note") else []
            rows.append((wt, tg, nl, 26 + len(tg) * 19 + (len(nl) * 17 + 5 if nl else 0) + 16))
        bh = 12 + sum(r[3] for r in rows)
        box(y, bh)
        ry = y + 12
        for i, (wt, tg, nl, rh) in enumerate(rows):
            mk = str(wt.get("market") or "")
            dom = mk.upper().startswith("KR") or "국내" in mk
            mcol = _hex("#2E6FD9") if dom else _hex("#158A6E")
            lbl = "국내" if dom else "미국"
            d.rounded_rectangle([px, ry + 1, px + pw, ry + 23], 10, fill=_tint(mcol, 0.16))
            d.text((px + (pw - d.textlength(lbl, font=f(11.5, True))) / 2.0, ry + 5), lbl,
                   font=f(11.5, True), fill=mcol)
            nx = tx
            nm = str(wt.get("name", ""))
            d.text((nx, ry), nm, font=f(15.5, True), fill=INK)
            nx += d.textlength(nm, font=f(15.5, True)) + 8
            if wt.get("ticker"):
                d.text((nx, ry + 5), str(wt["ticker"]), font=f(11.5), fill=MUTED)
            if wt.get("price"):
                pt = str(wt["price"])
                d.text((W - PAD - 16 - d.textlength(pt, font=f(14, True)), ry + 1), pt,
                       font=f(14, True), fill=INK)
            ty = ry + 26
            for t in tg:
                d.text((tx, ty), t, font=f(12.5, True), fill=_hex("#158A6E"))
                ty += 19
            if nl:
                ty += 5
                for ln in nl:
                    d.text((px, ty), ln, font=f(12), fill=MUTED)
                    ty += 17
            if i < len(rows) - 1:
                d.line([px, ry + rh - 8, W - PAD - 16, ry + rh - 8], fill=LINE)
            ry += rh
        y += bh + 8

    # ── 핵심 체크포인트 ──
    if checks or data.get("risk"):
        y += 8
        y = section("핵심 체크포인트", y)
        inner = W - 2 * PAD - 32
        blocks = []
        for c in checks:
            head, body = (list(c) + [""])[:2] if isinstance(c, (list, tuple)) else (str(c), "")
            blocks.append((str(head), _wrap(d, body, f(13), inner) if body else []))
        risk_lines = _wrap(d, data["risk"], f(12.5), inner - 20) if data.get("risk") else []
        bh = 16
        for head, lines in blocks:
            bh += 23 + 19 * len(lines) + 12
        if risk_lines:
            bh += 16 + 19 * len(risk_lines) + 16
        box(y, bh)
        ty = y + 14
        for head, lines in blocks:
            d.text((PAD + 16, ty), head, font=f(14.5, True), fill=INK)
            ty += 23
            for ln in lines:
                d.text((PAD + 16, ty), ln, font=f(13), fill=SUB); ty += 19
            ty += 12
        if risk_lines:
            rh = 16 + 19 * len(risk_lines)
            d.rounded_rectangle([PAD + 16, ty, W - PAD - 16, ty + rh], 9, fill=WARN_BG)
            d.rectangle([PAD + 16, ty + 5, PAD + 20, ty + rh - 5], fill=WARN)
            ly = ty + 9
            for ln in risk_lines:
                d.text((PAD + 30, ly), ln, font=f(12.5), fill=WARN); ly += 19
            ty += rh + 16
        y += bh + 8

    # ── 면책 ──
    y += 12
    d.line([PAD, y, W - PAD, y], fill=LINE)
    y += 12
    foot = data.get("footer") or ("정보 제공 목적이며 투자 권유가 아닙니다. "
                                  "투자 판단과 결과의 책임은 투자자 본인에게 있습니다.")
    for ln in _clip(_wrap(d, foot, f(11), iw), 4):
        d.text((PAD, y), ln, font=f(11), fill=MUTED); y += 16
    y += 16
    return _finish(img, y, out_path)


if __name__ == "__main__":
    import json
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        print("사용법: python card_render.py <데이터.json>")
        sys.exit(1)
    data = json.load(open(src, encoding="utf-8"))
    p = render_card(data, "card_preview.png")
    print("생성:", p, os.path.getsize(p) // 1024, "KB")
