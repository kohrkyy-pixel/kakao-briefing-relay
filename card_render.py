#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""리포트 카드 PNG 렌더러 (모바일 전체화면 판)

Claude 가 메일에 담아 보낸 수치(JSON)를 받아 릴레이 호스트에서 카드를 그립니다.
이미지 바이트가 모델 컨텍스트를 거치지 않으므로 용량 제약이 없습니다.

핵심 설계 — 왜 여러 장으로 나누는가
  긴 카드 한 장은 폰에서 전체화면으로 열면 세로에 맞춰 축소되어 좌우에 검은
  여백이 생기고 글씨가 작아집니다. 그래서 화면 비율(9:19.5 = 2.17)보다 조금
  납작한 560 x 1080 논리 페이지(1.93)로 잘라 여러 장을 만듭니다. 각 장은
  가로를 꽉 채우므로 손으로 확대할 필요가 없습니다.

  출력 해상도는 1400 x 2700 (SCALE 2.5). S25+ 를 QHD+ 로 쓰셔도 선명합니다.

필요: pip install pillow
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ── 색 ────────────────────────────────────────────────────
NAVY = (18, 48, 78)
NAVY_2 = (31, 68, 103)
GOLD = (185, 130, 42)
INK = (22, 32, 46)
SUB = (74, 90, 110)          # 흰 배경 대비 7.05:1
MUTED = (100, 114, 130)      # 흰 배경 대비 4.92:1 (WCAG AA)
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
W = 560              # 논리 폭
PAGE_H = 1080        # 논리 높이. 1.93 : 1 → 전체화면에서 좌우 여백 없음
PAD = 16
SCALE = 2.5          # 출력 1400 x 2700
HERO_H = 196         # 1쪽 상단 헤더
STRIP_H = 68         # 2쪽부터의 얇은 머리띠
FOOT_H = 34          # 쪽 번호 자리

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

    _shared = {}

    def __init__(self, scale=SCALE):
        reg, bold = _font_paths()
        self._reg, self._bold = reg, bold
        self._scale = scale

    def __call__(self, size, bold=False):
        key = (self._reg, self._scale, size, bold)
        if key not in F._shared:
            px = max(1, int(round(size * self._scale)))
            F._shared[key] = ImageFont.truetype(self._bold if bold else self._reg, px)
        return F._shared[key]


class S:
    """논리 좌표로 그리면 실제로는 SCALE 배로 그려주는 래퍼."""

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


def _probe():
    """길이 재기 전용 그리기 객체."""
    return S(ImageDraw.Draw(Image.new("RGB", (8, 8))))


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
    return tuple(int(round(b + (c - b) * ratio)) for c, b in zip(rgb, base))


def _won(n):
    try:
        return "{:,}".format(int(round(float(n))))
    except (TypeError, ValueError):
        return str(n)


def _wrap(d, text, font, max_w):
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
    o = str(op or "")
    if "매수" in o:
        return UP
    if "매도" in o or "축소" in o or "익절" in o:
        return DOWN
    if "관망" in o:
        return MUTED
    return SUB


def _delta_mark(d, x, y, s, direction, col):
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
    lw = max(2, r / 5.0)
    if kind == "globe":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=lw)
        d.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r], outline=col, width=lw * 0.7)
        d.line([cx - r, cy, cx + r, cy], fill=col, width=lw * 0.7)
    elif kind == "gov":
        d.rectangle([cx - r * 0.62, cy - r * 0.95, cx - r * 0.62 + lw, cy + r * 0.95], fill=col)
        d.polygon([(cx - r * 0.34, cy - r * 0.88), (cx + r * 0.85, cy - r * 0.45),
                   (cx - r * 0.34, cy - r * 0.02)], fill=col)
    elif kind == "chart":
        bw = r * 0.46
        for i, hgt in enumerate((0.5, 0.78, 1.0)):
            x0 = cx - r * 0.92 + i * (bw + r * 0.24)
            d.rectangle([x0, cy + r * 0.8 - r * 1.7 * hgt, x0 + bw, cy + r * 0.8], fill=col)
    elif kind == "ball":
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


# ── 페이지 조립 ───────────────────────────────────────────
def _sec_title(d, f, title, y):
    d.rounded_rectangle([PAD, y + 2, PAD + 6, y + 23], 3, fill=GOLD)
    d.text((PAD + 16, y), title, font=f(18, True), fill=INK)
    return y + 36


def _box(d, y, h):
    d.rounded_rectangle([PAD, y, W - PAD, y + h], 16, fill=WHITE, outline=LINE)


class Blk:
    """높이를 미리 아는 그리기 조각."""
    __slots__ = ("h", "fn")

    def __init__(self, h, fn):
        self.h, self.fn = h, fn


def _paginate(blocks, first_avail, rest_avail):
    pages, cur, used, avail = [], [], 0, first_avail
    for b in blocks:
        if cur and used + b.h > avail:
            pages.append(cur)
            cur, used, avail = [], 0, rest_avail
        cur.append(b)
        used += b.h
    if cur:
        pages.append(cur)
    return pages or [[]]


def _page_paths(out_path, n):
    if n <= 1:
        return [out_path]
    root, ext = os.path.splitext(out_path)
    return [out_path] + ["%s_%d%s" % (root, i + 1, ext) for i in range(1, n)]


def _emit(pages, out_path, hero, strip, colors=110):
    """페이지 목록을 PNG 로 씁니다. 경로 목록을 돌려줍니다."""
    f = F()
    paths = _page_paths(out_path, len(pages))
    total = len(pages)
    for i, page in enumerate(pages):
        img = Image.new("RGB", (int(W * SCALE), int(PAGE_H * SCALE)), PAPER)
        d = S(ImageDraw.Draw(img))
        y = hero(d, f) if i == 0 else strip(d, f, i + 1, total)
        for b in page:
            y = b.fn(d, y)
        if total > 1:
            lbl = "%d / %d" % (i + 1, total)
            d.text((W - PAD - d.textlength(lbl, font=f(12.5, True)), PAGE_H - 27),
                   lbl, font=f(12.5, True), fill=MUTED)
        img = img.convert("P", palette=Image.ADAPTIVE, colors=colors)
        img.save(paths[i], optimize=True)
    return paths


def _foot_block(d0, f, text):
    lines = _clip(_wrap(d0, text, f(12), W - 2 * PAD), 4)

    def draw(d, y):
        d.line([PAD, y + 6, W - PAD, y + 6], fill=LINE)
        yy = y + 18
        for ln in lines:
            d.text((PAD, yy), ln, font=f(12), fill=MUTED)
            yy += 18
        return yy + 8
    return Blk(24 + 18 * len(lines) + 8, draw)


# ═════════════════════════════════════════════════════════
# 종합 뉴스 카드
# ═════════════════════════════════════════════════════════
def render_news_card(data, out_path="card.png"):
    f = F()
    d0 = _probe()
    iw = W - 2 * PAD

    sections = data.get("sections") or []
    metrics = data.get("metrics") or []
    total_items = sum(len(s.get("items") or []) for s in sections)

    head = str(data.get("headline") or "").strip()
    hlines = _clip(_wrap(d0, head, f(16.5, True), iw - 26), 2) if head else []
    hero_h = HERO_H if hlines else HERO_H - 46

    def hero(d, f):
        d.rectangle([0, 0, W, hero_h], fill=NAVY)
        d.rectangle([0, 0, W, 6], fill=GOLD)
        d.text((PAD, 28), "DAILY NEWS BRIEFING", font=f(12, True), fill=GOLD)
        d.text((PAD, 50), data.get("title", "종합 브리핑"), font=f(30, True), fill=WHITE)
        sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
        if sub:
            d.text((PAD, 94), sub, font=f(13.5), fill=HEAD_SUB)
        if sections:
            badge = "%d개 분야 · %d건" % (len(sections), total_items)
            bw = d.textlength(badge, font=f(13, True)) + 28
            d.rounded_rectangle([W - PAD - bw, 48, W - PAD, 79], 15, fill=NAVY_2)
            d.text((W - PAD - bw + 14, 55), badge, font=f(13, True), fill=WHITE)
        if hlines:
            hy = 124
            d.rectangle([PAD, hy + 4, PAD + 6, hy + len(hlines) * 26 - 4], fill=HERO)
            for i, ln in enumerate(hlines):
                d.text((PAD + 18, hy + i * 26), ln, font=f(16.5, True), fill=HERO)
        return hero_h + 18

    def strip(d, f, page, total):
        d.rectangle([0, 0, W, STRIP_H], fill=NAVY)
        d.rectangle([0, 0, W, 5], fill=GOLD)
        d.text((PAD, 22), data.get("title", "종합 브리핑"), font=f(18, True), fill=WHITE)
        sub = str(data.get("date") or "")
        if sub:
            d.text((W - PAD - d.textlength(sub, font=f(13)), 27), sub,
                   font=f(13), fill=HEAD_SUB)
        return STRIP_H + 18

    blocks = []

    # 지표 스트립 (2열)
    if metrics:
        m = list(metrics)[:4]
        cols = 2 if len(m) > 2 else len(m)
        gap, th = 10, 84
        tw = (iw - gap * (cols - 1)) / float(cols)
        rows_n = (len(m) + cols - 1) // cols

        def draw_metrics(d, y, m=m, cols=cols, tw=tw, th=th, gap=gap, rows_n=rows_n):
            for i, mt in enumerate(m):
                if not isinstance(mt, dict):
                    mt = {"label": str(mt)}
                x0 = PAD + (i % cols) * (tw + gap)
                ty = y + (i // cols) * (th + gap)
                d.rounded_rectangle([x0, ty, x0 + tw, ty + th], 14, fill=WHITE, outline=LINE)
                delta = str(mt.get("delta") or "")
                dirn = str(mt.get("dir") or "").lower()
                if not dirn:
                    dirn = "up" if delta.startswith("+") else (
                        "down" if delta.startswith("-") else "flat")
                col = UP if dirn == "up" else DOWN if dirn == "down" else MUTED
                d.rounded_rectangle([x0, ty + 16, x0 + 5, ty + th - 16], 3, fill=col)
                d.text((x0 + 18, ty + 13), str(mt.get("label") or ""), font=f(13), fill=MUTED)
                d.text((x0 + 18, ty + 33), str(mt.get("value") or ""), font=f(24, True), fill=INK)
                if delta:
                    _delta_mark(d, x0 + 18, ty + 66, 10, dirn, col)
                    d.text((x0 + 34, ty + 61), delta, font=f(14, True), fill=col)
            return y + rows_n * (th + gap) + 8
        blocks.append(Blk(rows_n * (th + gap) + 8, draw_metrics))

    # 섹션들
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

        hf, bf, tf = f(16.5, True), f(14.5), f(12, True)
        gx, gw = PAD + 14, 48
        tx = gx + gw + 13
        tw = W - PAD - 13 - tx

        rows = []
        for h_t, b_t, tag, key in norm:
            hls = _clip(_wrap(d0, h_t, hf, tw), 2)
            bls = _clip(_wrap(d0, b_t, bf, tw), 3) if b_t else []
            rh = 14 + len(hls) * 23 + (len(bls) * 21 + 5 if bls else 0) + 14
            rows.append((hls, bls, tag, key, rh))

        hdr_h = 54
        box_h = hdr_h + sum(r[4] for r in rows) + 2

        def draw_sec(d, y, name=name, col=col, icon=icon, rows=rows,
                     hdr_h=hdr_h, box_h=box_h, hf=hf, bf=bf, tf=tf,
                     gx=gx, gw=gw, tx=tx):
            d.rounded_rectangle([PAD, y, W - PAD, y + box_h], 16, fill=WHITE, outline=LINE)
            band = _tint(col, 0.10)
            d.rounded_rectangle([PAD + 1, y + 1, W - PAD - 1, y + hdr_h], 16, fill=band)
            d.rectangle([PAD + 1, y + hdr_h - 16, W - PAD - 1, y + hdr_h], fill=band)
            d.line([PAD + 1, y + hdr_h, W - PAD - 1, y + hdr_h], fill=_tint(col, 0.30))
            cy = y + hdr_h / 2.0
            d.ellipse([PAD + 14, cy - 18, PAD + 50, cy + 18], fill=col)
            _icon(d, icon, PAD + 32, cy, 10, WHITE, col)
            d.text((PAD + 62, cy - 12), name, font=f(18, True), fill=col)
            cnt = "%d건" % len(rows)
            d.text((W - PAD - 16 - d.textlength(cnt, font=f(12.5, True)), cy - 9),
                   cnt, font=f(12.5, True), fill=col)

            ry = y + hdr_h + 1
            for i, (hls, bls, tag, key, rh) in enumerate(rows):
                if key:
                    d.rectangle([PAD + 1, ry, W - PAD - 1, ry + rh], fill=_tint(col, 0.07))
                    d.rectangle([PAD + 1, ry, PAD + 7, ry + rh], fill=col)
                elif i:
                    d.line([tx, ry, W - PAD - 13, ry], fill=LINE)
                ty = ry + 14
                if tag:
                    twd = d.textlength(tag, font=tf)
                    d.rounded_rectangle([gx, ty + 1, gx + gw, ty + 24], 11,
                                        fill=_tint(col, 0.17))
                    d.text((gx + (gw - twd) / 2.0, ty + 5), tag, font=tf, fill=col)
                else:
                    d.ellipse([gx + gw / 2 - 6, ty + 7, gx + gw / 2 + 6, ty + 19], fill=col)
                for j, ln in enumerate(hls):
                    d.text((tx, ty + j * 23), ln, font=hf, fill=INK)
                ty += len(hls) * 23
                if bls:
                    ty += 5
                    for ln in bls:
                        d.text((tx, ty), ln, font=bf, fill=SUB)
                        ty += 21
                ry += rh
            return y + box_h + 16
        blocks.append(Blk(box_h + 16, draw_sec))

    blocks.append(_foot_block(d0, f, data.get("footer")
                              or "공개된 언론 보도를 정리한 참고 자료입니다."))

    pages = _paginate(blocks, PAGE_H - (hero_h + 18) - FOOT_H,
                      PAGE_H - (STRIP_H + 18) - FOOT_H)
    return _emit(pages, out_path, hero, strip)


# ═════════════════════════════════════════════════════════
# 투자 브리핑 카드
# ═════════════════════════════════════════════════════════
def render_card(data, out_path="card.png"):
    """수치 dict → 카드 PNG 여러 장. 경로 목록을 돌려줍니다."""
    if str(data.get("type", "")).lower() == "news" or data.get("sections"):
        return render_news_card(data, out_path)

    f = F()
    d0 = _probe()
    iw = W - 2 * PAD

    holdings = data.get("holdings") or []
    market = data.get("market") or []
    watch = data.get("watchlist") or []
    checks = data.get("checkpoints") or []
    pnl = data.get("pnl")
    hero_h = HERO_H if pnl is not None else 128

    def hero(d, f):
        d.rectangle([0, 0, W, hero_h], fill=NAVY)
        d.rectangle([0, 0, W, 6], fill=GOLD)
        d.text((PAD, 28), "PORTFOLIO INTELLIGENCE REPORT", font=f(12, True), fill=GOLD)
        d.text((PAD, 50), data.get("title", "투자 브리핑"), font=f(30, True), fill=WHITE)
        sub = " · ".join(x for x in (data.get("date"), data.get("subtitle")) if x)
        if sub:
            for i, ln in enumerate(_clip(_wrap(d, sub, f(13.5), iw), 2)):
                d.text((PAD, 92 + i * 19), ln, font=f(13.5), fill=HEAD_SUB)
        if pnl is not None:
            d.text((PAD, 130), "총 평가손익", font=f(13), fill=HEAD_SUB)
            sign = "+" if float(pnl) >= 0 else ""
            big = "%s%s" % (sign, _won(pnl))
            d.text((PAD, 149), big, font=f(35, True), fill=HERO)
            bx = PAD + d.textlength(big, font=f(35, True)) + 8
            d.text((bx, 169), "원", font=f(16, True), fill=HERO)
            bx += d.textlength("원", font=f(16, True)) + 16
            d.text((bx, 166), "%s%.2f%%" % (sign, float(data.get("pnl_pct", 0))),
                   font=f(19, True), fill=HERO)
            if data.get("value") is not None:
                vt = _won(data["value"]) + "원"
                d.text((W - PAD - d.textlength("평가금액", font=f(13)), 130),
                       "평가금액", font=f(13), fill=HEAD_SUB)
                d.text((W - PAD - d.textlength(vt, font=f(19, True)), 150),
                       vt, font=f(19, True), fill=WHITE)
        return hero_h + 18

    def strip(d, f, page, total):
        d.rectangle([0, 0, W, STRIP_H], fill=NAVY)
        d.rectangle([0, 0, W, 5], fill=GOLD)
        d.text((PAD, 22), data.get("title", "투자 브리핑"), font=f(18, True), fill=WHITE)
        if pnl is not None:
            sign = "+" if float(pnl) >= 0 else ""
            t = "%s%s원 (%s%.2f%%)" % (sign, _won(pnl), sign, float(data.get("pnl_pct", 0)))
            d.text((W - PAD - d.textlength(t, font=f(13, True)), 26), t,
                   font=f(13, True), fill=HERO)
        return STRIP_H + 18

    blocks = []

    # ── 시장 지표 (2열) ──
    if market:
        m = list(market)[:6]
        gap, th = 10, 80
        tw = (iw - gap) / 2.0
        rows_n = (len(m) + 1) // 2

        def draw_mkt(d, y, m=m, tw=tw, th=th, gap=gap, rows_n=rows_n):
            for i, row in enumerate(m):
                k, v, chg, dirn = (list(row) + ["", "", ""])[:4]
                x0 = PAD + (i % 2) * (tw + gap)
                ty = y + (i // 2) * (th + gap)
                d.rounded_rectangle([x0, ty, x0 + tw, ty + th], 14, fill=WHITE, outline=LINE)
                col = UP if str(dirn).lower() == "up" else DOWN
                d.rounded_rectangle([x0, ty + 15, x0 + 5, ty + th - 15], 3, fill=col)
                d.text((x0 + 18, ty + 12), str(k), font=f(13), fill=MUTED)
                d.text((x0 + 18, ty + 32), str(v), font=f(23, True), fill=INK)
                if str(chg):
                    d.text((x0 + 18, ty + 60), str(chg), font=f(14, True), fill=col)
            return y + rows_n * (th + gap) + 8
        blocks.append(Blk(rows_n * (th + gap) + 8, draw_mkt))

    # ── 포트폴리오 ──
    if holdings and any(h.get("weight") for h in holdings):
        note = data.get("portfolio_note")
        nlines = _clip(_wrap(d0, note, f(13), W - PAD - 20 - (PAD + 168)), 3) if note else []
        pf_h = max(56 + 38 * len(holdings) + (17 + 19 * len(nlines) if nlines else 0), 190)

        def draw_pf(d, y, bh=pf_h, nlines=nlines):
            yy = _sec_title(d, f, "포트폴리오", y)
            _box(d, yy, bh)
            cx, cy, r, thick = PAD + 88, yy + bh / 2, 62, 24
            start = -90.0
            for h in holdings:
                wgt = float(h.get("weight") or 0)
                if wgt <= 0:
                    continue
                ext = 360.0 * wgt / 100.0
                d.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + ext - 1.2,
                           fill=_hex(h.get("color")))
                start += ext
            d.ellipse([cx - r + thick, cy - r + thick, cx + r - thick, cy + r - thick],
                      fill=WHITE)
            if data.get("value") is not None:
                t1 = "총 평가"
                d.text((cx - d.textlength(t1, font=f(12, True)) / 2, cy - 19), t1,
                       font=f(12, True), fill=SUB)
                t2 = _won(float(data["value"]) / 10000) + "만"
                d.text((cx - d.textlength(t2, font=f(18, True)) / 2, cy - 3), t2,
                       font=f(18, True), fill=INK)
            lx, ly = PAD + 168, yy + 28
            for h in holdings:
                d.rounded_rectangle([lx, ly + 5, lx + 13, ly + 18], 3,
                                    fill=_hex(h.get("color")))
                d.text((lx + 22, ly), str(h.get("name", "")), font=f(16, True), fill=INK)
                pct = "%.1f%%" % float(h.get("weight") or 0)
                d.text((W - PAD - 18 - d.textlength(pct, font=f(15.5, True)), ly), pct,
                       font=f(15.5, True), fill=SUB)
                ly += 38
            if nlines:
                ly += 3
                d.line([lx, ly, W - PAD - 18, ly], fill=LINE)
                ly += 10
                for ln in nlines:
                    d.text((lx, ly), ln, font=f(13), fill=SUB); ly += 19
            return yy + bh + 14
        blocks.append(Blk(36 + pf_h + 14, draw_pf))

    # ── 종목별 수익률 ──
    if holdings and any(h.get("pct") is not None for h in holdings):
        items = sorted([h for h in holdings if h.get("pct") is not None],
                       key=lambda x: -float(x["pct"]))
        brows = [(h.get("name", ""), float(h["pct"]), _hex(h.get("color"))) for h in items]
        if data.get("pnl_pct") is not None:
            brows.append(("전체", float(data["pnl_pct"]), NAVY))
        bar_h = 24 + 42 * len(brows)

        def draw_bar(d, y, rows=brows, bh=bar_h):
            yy = _sec_title(d, f, "종목별 수익률", y)
            _box(d, yy, bh)
            x0, x1 = PAD + 104, W - PAD - 96
            vmax = max([abs(v) for _, v, _ in rows] + [0.01]) * 1.12
            by = yy + 17
            for nm, v, col in rows:
                d.text((x0 - 14 - d.textlength(nm, font=f(14.5, True)), by + 2), nm,
                       font=f(14.5, True), fill=INK)
                bw = max((x1 - x0) * abs(v) / vmax, 6)
                d.rounded_rectangle([x0, by, x0 + bw, by + 20], 6, fill=col)
                lab = "%s%.2f%%" % ("+" if v >= 0 else "", v)
                d.text((x0 + bw + 10, by + 2), lab, font=f(14.5, True), fill=col)
                by += 42
            return yy + bh + 14
        blocks.append(Blk(36 + bar_h + 14, draw_bar))

    # ── 보유 종목 · 의견 (종목 하나 = 조각 하나) ──
    if holdings:
        tx0 = PAD + 18
        tw0 = W - PAD - 18 - tx0
        for i, h in enumerate(holdings):
            parts = []
            cl = float(h["close"]) if h.get("close") else None
            for key, lb in (("target_s", "단기"), ("target_m", "중기")):
                if h.get(key) is not None:
                    t = float(h[key])
                    up = " (%+.0f%%)" % ((t / cl - 1) * 100) if cl else ""
                    parts.append("%s %s%s" % (lb, _won(t), up))
            rl = _clip(_wrap(d0, str(h.get("reason") or ""), f(14), tw0), 3) \
                if h.get("reason") else []
            op = h.get("opinion")
            rh = 58 + (30 if (op or parts) else 0) + (len(rl) * 20 + 6 if rl else 0) + 16
            first = (i == 0)

            def draw_h(d, y, h=h, parts=parts, rl=rl, op=op, rh=rh, first=first,
                       tx=tx0, tw=tw0):
                yy = _sec_title(d, f, "보유 종목 · 의견", y) if first else y
                _box(d, yy, rh)
                ry = yy + 15
                col = _hex(h.get("color"))
                d.rounded_rectangle([tx, ry + 4, tx + 7, ry + 21], 3, fill=col)
                nx = tx + 17
                nm = str(h.get("name", ""))
                d.text((nx, ry), nm, font=f(17.5, True), fill=INK)
                nx += d.textlength(nm, font=f(17.5, True)) + 8
                if h.get("qty"):
                    d.text((nx, ry + 7), "%s주" % h["qty"], font=f(13), fill=MUTED)
                hp = h.get("pnl")
                if hp is not None:
                    pcol = UP if float(hp) >= 0 else DOWN
                    sign = "+" if float(hp) >= 0 else ""
                    pl = "%s%.2f%%" % (sign, float(h.get("pct") or 0))
                    d.text((W - PAD - 18 - d.textlength(pl, font=f(17, True)), ry),
                           pl, font=f(17, True), fill=pcol)
                    am = "%s%s원" % (sign, _won(hp))
                    d.text((W - PAD - 18 - d.textlength(am, font=f(13)), ry + 25),
                           am, font=f(13), fill=pcol)
                sub2 = " · ".join(str(x) for x in (
                    (_won(h["close"]) + "원") if h.get("close") else None, h.get("note")) if x)
                if sub2:
                    for ln in _clip(_wrap(d, sub2, f(13), tw - 110), 1):
                        d.text((tx + 17, ry + 26), ln, font=f(13), fill=SUB)
                ly = ry + 52
                if op or parts:
                    bx = tx + 17
                    if op:
                        ocol = _opinion_color(op)
                        ow = d.textlength(str(op), font=f(14, True)) + 24
                        d.rounded_rectangle([bx, ly, bx + ow, ly + 27], 12,
                                            fill=_tint(ocol, 0.15))
                        d.text((bx + 12, ly + 5), str(op), font=f(14, True), fill=ocol)
                        bx += ow + 11
                    if parts:
                        txt = "  ·  ".join(parts)
                        if d.textlength(txt, font=f(14, True)) > (W - PAD - 18 - bx):
                            txt = "  ·  ".join(p.replace("단기 ", "").replace("중기 ", "")
                                               for p in parts)
                        d.text((bx, ly + 7), txt, font=f(14, True), fill=INK)
                    ly += 30
                if rl:
                    ly += 6
                    for ln in rl:
                        d.text((tx + 17, ly), ln, font=f(14), fill=SUB)
                        ly += 20
                return yy + rh + 10
            blocks.append(Blk((36 if first else 0) + rh + 10, draw_h))

    # ── 저평가 종목 (종목 하나 = 조각 하나) ──
    if watch:
        px0, pw0 = PAD + 18, 52
        tx1 = px0 + pw0 + 13
        for i, wt in enumerate(watch):
            tg = [str(x) for x in (wt.get("target_s"), wt.get("target_m")) if x]
            nl = _clip(_wrap(d0, str(wt.get("note") or ""), f(13.5), W - PAD - 18 - px0), 3) \
                if wt.get("note") else []
            rh = 32 + len(tg) * 22 + (len(nl) * 19 + 6 if nl else 0) + 16
            first = (i == 0)

            def draw_w(d, y, wt=wt, tg=tg, nl=nl, rh=rh, first=first,
                       px=px0, pw=pw0, tx=tx1):
                yy = _sec_title(d, f, "주목할 저평가 종목", y) if first else y
                _box(d, yy, rh)
                ry = yy + 14
                mk = str(wt.get("market") or "")
                dom = mk.upper().startswith("KR") or "국내" in mk
                mcol = _hex("#2E6FD9") if dom else _hex("#158A6E")
                lbl = "국내" if dom else "미국"
                d.rounded_rectangle([px, ry + 1, px + pw, ry + 26], 12, fill=_tint(mcol, 0.16))
                d.text((px + (pw - d.textlength(lbl, font=f(13, True))) / 2.0, ry + 5), lbl,
                       font=f(13, True), fill=mcol)
                nx = tx
                nm = str(wt.get("name", ""))
                d.text((nx, ry), nm, font=f(17, True), fill=INK)
                nx += d.textlength(nm, font=f(17, True)) + 9
                if wt.get("ticker"):
                    d.text((nx, ry + 7), str(wt["ticker"]), font=f(13), fill=MUTED)
                if wt.get("price"):
                    pt = str(wt["price"])
                    d.text((W - PAD - 18 - d.textlength(pt, font=f(15.5, True)), ry + 2), pt,
                           font=f(15.5, True), fill=INK)
                ty = ry + 32
                for t in tg:
                    d.text((tx, ty), t, font=f(14, True), fill=_hex("#158A6E"))
                    ty += 22
                if nl:
                    ty += 6
                    for ln in nl:
                        d.text((px, ty), ln, font=f(13.5), fill=SUB)
                        ty += 19
                return yy + rh + 10
            blocks.append(Blk((36 if first else 0) + rh + 10, draw_w))

    # ── 체크포인트 ──
    if checks or data.get("risk"):
        inner = W - 2 * PAD - 36
        cps = []
        for c in checks:
            head, body = (list(c) + [""])[:2] if isinstance(c, (list, tuple)) else (str(c), "")
            cps.append((str(head), _wrap(d0, body, f(14.5), inner)))
        risk_lines = _wrap(d0, data["risk"], f(14), inner - 22) if data.get("risk") else []
        cp_h = 18
        for head, lines in cps:
            cp_h += 26 + 21 * len(lines) + 14
        if risk_lines:
            cp_h += 18 + 21 * len(risk_lines) + 18

        def draw_cp(d, y, cps=cps, risk_lines=risk_lines, bh=cp_h):
            yy = _sec_title(d, f, "핵심 체크포인트", y)
            _box(d, yy, bh)
            ty = yy + 16
            for head, lines in cps:
                d.text((PAD + 18, ty), head, font=f(16, True), fill=INK)
                ty += 26
                for ln in lines:
                    d.text((PAD + 18, ty), ln, font=f(14.5), fill=SUB); ty += 21
                ty += 14
            if risk_lines:
                rh = 18 + 21 * len(risk_lines)
                d.rounded_rectangle([PAD + 18, ty, W - PAD - 18, ty + rh], 10, fill=WARN_BG)
                d.rectangle([PAD + 18, ty + 6, PAD + 23, ty + rh - 6], fill=WARN)
                ly = ty + 10
                for ln in risk_lines:
                    d.text((PAD + 34, ly), ln, font=f(14), fill=WARN); ly += 21
            return yy + bh + 14
        blocks.append(Blk(36 + cp_h + 14, draw_cp))

    blocks.append(_foot_block(d0, f, data.get("footer") or
                              "정보 제공 목적이며 투자 권유가 아닙니다. "
                              "투자 판단과 결과의 책임은 투자자 본인에게 있습니다."))

    pages = _paginate(blocks, PAGE_H - (hero_h + 18) - FOOT_H,
                      PAGE_H - (STRIP_H + 18) - FOOT_H)
    return _emit(pages, out_path, hero, strip)


if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        print("사용법: python card_render.py <데이터.json>")
        sys.exit(1)
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    paths = render_card(data, "card_preview.png")
    if isinstance(paths, str):
        paths = [paths]
    for p in paths:
        print("생성:", p, os.path.getsize(p) // 1024, "KB")
