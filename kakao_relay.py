#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
카카오톡 '나와의 채팅' 릴레이
==============================

Claude가 Gmail로 보낸 투자 브리핑을 읽어서 카카오톡 '나에게 보내기'로 전달합니다.

사용법:
    python kakao_relay.py setup    # 최초 1회 - 설정값 입력 + 카카오 로그인
    python kakao_relay.py config   # 설정값만 다시 입력 (키를 바꿀 때)
    python kakao_relay.py test     # 테스트 메시지 전송
    python kakao_relay.py run      # 메일함 확인 후 새 브리핑 전달 (스케줄러가 반복 호출)
    python kakao_relay.py resend   # 이미 보낸 것이라도 최근 브리핑 1통 다시 전송
    python kakao_relay.py watch    # 상주 모드 - 60초마다 자동 반복

필요: pip install requests pillow   (pillow 는 카드 이미지용)

필요한 것:
    1. 카카오 REST API 키 + Gmail 앱 비밀번호 (setup 이 물어봅니다)
    2. pip install requests

파이썬 3.8 이상이면 동작합니다. requests 외 외부 패키지는 쓰지 않습니다.
"""

import base64
import email
import email.header
import html
import http.server
import imaplib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime

try:
    import requests
except ImportError:
    print("[오류] requests 패키지가 없습니다. 명령 프롬프트에서 아래를 실행하세요:")
    print("       pip install requests")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────
VERSION = "3.5"          # 이 값이 3.5 미만이면 예전 파일입니다
BUILD_DATE = "2026-08-16"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOKEN_PATH = os.path.join(BASE_DIR, "tokens.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "relay.log")

KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_IMG_URL = "https://kapi.kakao.com/v2/api/talk/message/image/upload"

# 카카오 기본 텍스트 템플릿은 text 필드가 최대 200자입니다.
# 분할 표기 "(1/5)\n" 등을 붙일 여유를 두고 180자로 자릅니다.
KAKAO_TEXT_LIMIT = 180

# 릴레이가 처리할 메일 제목 접두어
SUBJECT_PREFIXES = [
    "[아침 브리핑]",
    "[장중 알림]",
    "[미국장 마감]",
    "[투자브리핑]",
    "[종합 브리핑]",
    "[전달경로 테스트]",
]

# Claude가 메일 본문에 넣는 카톡 전송용 구간 구분자
KAKAO_START = "===KAKAO_START==="
KAKAO_END = "===KAKAO_END==="
# 카톡 메시지의 '자세히 보기' 버튼이 열 리포트 주소
KAKAO_LINK = "===KAKAO_LINK==="
# 카드 이미지를 그릴 수치 (JSON)
KAKAO_DATA = "===KAKAO_DATA==="
KAKAO_DATA_END = "===KAKAO_DATA_END==="


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log("파일 읽기 실패 %s: %s" % (path, e))
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DEFAULTS = {
    # 카톡 메시지 하단 '자세히 보기' 버튼이 여는 주소.
    # ★ 이 도메인은 카카오 앱의 [제품 링크 관리] > 웹 도메인 에 등록돼 있어야
    #   버튼이 정상 동작합니다. 등록 안 된 도메인이면 '네트워크 상태 이상' 이 뜹니다.
    "kakao_link_url": "https://finance.naver.com",
    "kakao_link_mobile_url": "https://m.stock.naver.com",
    "redirect_uri": "http://localhost:5959/oauth",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "send_delay_sec": 1.2,
    "max_messages_per_brief": 8,
    "watch_interval_sec": 60,
    # 카드의 '자세히 보기' 가 여는 곳.
    # True 로 두면 카카오 CDN 의 이미지 원본을 열지만, 그 서버가 외부 직접 접근을
    # 막아 403 이 납니다. 그래서 False 로 두고 아래 kakao_link_url 을 씁니다.
    # 이미지를 크게 보시려면 카톡에서 카드 이미지를 길게 눌러 저장하세요.
    "card_link_to_image": False,
}

REQUIRED = ("kakao_rest_api_key", "gmail_address", "gmail_app_password")

FIELD_LABELS = {
    "kakao_rest_api_key": "카카오 REST API 키",
    "gmail_address": "Gmail 주소",
    "gmail_app_password": "Gmail 앱 비밀번호 (16자리)",
    "kakao_client_secret": "카카오 Client Secret (안 쓰면 그냥 엔터)",
}

# 선택 항목 — 카카오 앱에서 Client Secret 을 '사용함' 으로 켠 경우에만 필요합니다
OPTIONAL = ("kakao_client_secret",)


def _is_blank(value):
    """비어 있거나 아직 견본 문구인지."""
    if value is None:
        return True
    text = str(value).strip()
    return (not text) or text.startswith("여기에") or text.startswith("(")


def _check_kakao_key(key):
    key = key.strip()
    if len(key) != 32 or not re.fullmatch(r"[0-9a-fA-F]+", key):
        return ("REST API 키는 보통 영문소문자+숫자 32자리입니다. "
                "네이티브 앱 키/JavaScript 키를 넣지 않았는지 확인해주세요.")
    return None


def _check_app_password(pw):
    stripped = pw.replace(" ", "").replace("-", "")
    if len(stripped) != 16 or not stripped.isalpha():
        return ("앱 비밀번호는 공백을 뺀 영문 16자리입니다. "
                "구글 계정 로그인 비밀번호가 아니라 앱 비밀번호여야 합니다.")
    return None


CHECKS = {
    "kakao_rest_api_key": _check_kakao_key,
    "gmail_app_password": _check_app_password,
}


def prompt_for_config(cfg=None):
    """부족한 항목을 직접 물어보고 config.json 에 저장합니다."""
    cfg = dict(cfg or {})
    cfg.pop("_설명", None)
    cfg.pop("_아래는 건드리지 않아도 됩니다", None)

    print()
    print("=" * 62)
    print("  설정값 입력")
    print("=" * 62)
    print()
    print("  아래 항목을 붙여넣어 주세요. 명령 프롬프트에서는")
    print("  마우스 오른쪽 버튼을 누르면 붙여넣기가 됩니다.")
    print()
    print("  · 카카오 REST API 키")
    print("      developers.kakao.com > 내 애플리케이션 > 앱 선택 >")
    print("      [앱] > [플랫폼 키] > REST API 키")
    print("  · Gmail 앱 비밀번호")
    print("      myaccount.google.com/apppasswords 에서 발급한 16자리")
    print("      (구글 로그인 비밀번호가 아닙니다)")
    print()

    for key in REQUIRED:
        current = cfg.get(key)
        if not _is_blank(current):
            print("  [건너뜀] %s — 이미 설정됨" % FIELD_LABELS[key])
            continue

        while True:
            try:
                value = input("  %s: " % FIELD_LABELS[key]).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  입력이 취소되었습니다.")
                sys.exit(1)

            if not value:
                print("    → 값을 입력해주세요.")
                continue

            checker = CHECKS.get(key)
            warning = checker(value) if checker else None
            if warning:
                print("    [확인 필요] %s" % warning)
                try:
                    again = input("    그래도 이 값으로 진행할까요? (y/다시입력은 엔터): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n  입력이 취소되었습니다.")
                    sys.exit(1)
                if again != "y":
                    continue

            cfg[key] = value
            break

    # 선택 항목 — 비워두면 사용하지 않습니다
    for key in OPTIONAL:
        if not _is_blank(cfg.get(key)):
            continue
        print()
        print("  ※ 카카오 앱에서 Client Secret 을 '사용함' 으로 켜두셨다면 그 값을,")
        print("     끄셨거나 모르시겠으면 그냥 엔터를 누르세요.")
        try:
            value = input("  %s: " % FIELD_LABELS[key]).strip()
        except (EOFError, KeyboardInterrupt):
            value = ""
        if value:
            cfg[key] = value

    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)

    save_json(CONFIG_PATH, cfg)
    print()
    print("  설정을 저장했습니다 -> %s" % CONFIG_PATH)
    print()
    return cfg


ENV_KEYS = {
    "kakao_rest_api_key": "KAKAO_REST_API_KEY",
    "kakao_client_secret": "KAKAO_CLIENT_SECRET",
    "gmail_address": "GMAIL_ADDRESS",
    "gmail_app_password": "GMAIL_APP_PASSWORD",
}


def load_config(interactive=False):
    cfg = load_json(CONFIG_PATH) or {}

    # 환경변수가 있으면 우선 적용합니다 (GitHub Actions 등 무인 실행용)
    for key, env in ENV_KEYS.items():
        val = os.environ.get(env)
        if val and val.strip():
            cfg[key] = val.strip()

    missing = [k for k in REQUIRED if _is_blank(cfg.get(k))]
    if missing:
        if interactive:
            cfg = prompt_for_config(cfg)
        else:
            print("[오류] 아직 설정되지 않은 항목이 있습니다: %s"
                  % ", ".join(FIELD_LABELS[k] for k in missing))
            print("       '1_최초설정.bat' 을 실행하면 값을 물어봐서 자동으로 저장합니다.")
            sys.exit(1)

    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


# ─────────────────────────────────────────────────────────────
# 1) 최초 설정 - 카카오 OAuth 토큰 발급
# ─────────────────────────────────────────────────────────────
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """카카오가 인가 코드를 붙여 리디렉션하는 것을 한 번 받아냅니다."""
    code_holder = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        err = params.get("error", [None])[0]
        err_desc = params.get("error_description", [None])[0]

        # 브라우저가 자동으로 보내는 부수 요청(favicon 등)은 무시합니다.
        # 이걸 인증 실패로 처리하면 성공한 인증까지 취소돼 버립니다.
        if not code and not err:
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if code:
            _CallbackHandler.code_holder["code"] = code
            body = """
            <html><head><meta charset="utf-8"><title>인증 완료</title></head>
            <body style="font-family:sans-serif;text-align:center;padding-top:80px">
            <h2 style="color:#0F2B46">인증이 완료되었습니다</h2>
            <p style="color:#5B6B80">이 창을 닫고 명령 프롬프트로 돌아가세요.</p>
            </body></html>"""
        else:
            _CallbackHandler.code_holder["error"] = err
            _CallbackHandler.code_holder["error_description"] = err_desc or ""
            body = """
            <html><head><meta charset="utf-8"><title>인증 실패</title></head>
            <body style="font-family:sans-serif;text-align:center;padding-top:80px">
            <h2 style="color:#D63B3B">인증에 실패했습니다</h2>
            <p style="color:#5B6B80">%s</p>
            <p style="color:#8A97A8;font-size:13px">%s</p>
            </body></html>""" % (html.escape(str(err)), html.escape(str(err_desc or "")))

        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # 콘솔 소음 제거


def cmd_setup():
    cfg = load_config(interactive=True)
    redirect = cfg["redirect_uri"]
    parsed = urllib.parse.urlparse(redirect)
    port = parsed.port or 80

    # 포트가 이미 쓰이고 있는지 확인
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    if probe.connect_ex(("127.0.0.1", port)) == 0:
        probe.close()
        print("[오류] 포트 %d 가 이미 사용 중입니다. config.json 의 redirect_uri 포트를 바꾸고," % port)
        print("       카카오 디벨로퍼스에도 같은 값을 등록해주세요.")
        sys.exit(1)
    probe.close()

    auth_url = KAKAO_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg["kakao_rest_api_key"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "talk_message",
    })

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print()
    print("=" * 62)
    print(" 카카오 로그인 창을 엽니다. '카카오톡 메시지 전송'에 동의해주세요.")
    print("=" * 62)
    print()
    print(" 브라우저가 자동으로 열리지 않으면 아래 주소를 직접 붙여넣으세요:")
    print()
    print(" " + auth_url)
    print()

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print(" 브라우저에서 동의를 마칠 때까지 기다립니다 (최대 5분)...")
    deadline = time.time() + 300
    while time.time() < deadline:
        if "code" in _CallbackHandler.code_holder:
            time.sleep(0.6)  # 응답 페이지가 브라우저로 다 나갈 시간을 줍니다
            break
        if "error" in _CallbackHandler.code_holder:
            break
        time.sleep(0.4)
    server.shutdown()

    # 인가 코드를 먼저 확인합니다. 코드가 왔다면 성공이며,
    # 뒤늦게 들어온 부수 요청 때문에 실패로 처리해서는 안 됩니다.
    code = _CallbackHandler.code_holder.get("code")

    if not code and "error" in _CallbackHandler.code_holder:
        err = _CallbackHandler.code_holder.get("error")
        desc = _CallbackHandler.code_holder.get("error_description") or ""
        print("[오류] 카카오가 오류를 반환했습니다: %s" % err)
        if desc:
            print("       %s" % desc)
        print()
        if "consent" in str(err).lower() or "scope" in str(desc).lower():
            print("  → [카카오 로그인] > [동의항목] 에서 '카카오톡 메시지 전송' 을")
            print("     '이용 중 동의' 로 켠 뒤 다시 실행하세요.")
        elif "access_denied" in str(err).lower():
            print("  → 브라우저에서 '취소' 를 누르셨습니다. 다시 실행해 '동의하고 계속하기' 를 눌러주세요.")
        else:
            print("  → [앱] > [플랫폼 키] > REST API 키 화면의 리다이렉트 URI 와")
            print("     [카카오 로그인] > [동의항목] 설정을 확인해주세요.")
        print()
        sys.exit(1)

    if not code:
        print("[오류] 시간 안에 인가 코드를 받지 못했습니다.")
        print()
        print("  브라우저에서 '동의하고 계속하기' 를 눌러 '인증이 완료되었습니다'")
        print("  화면까지 확인하셨는지 점검한 뒤 다시 실행해주세요.")
        print()
        sys.exit(1)

    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["kakao_rest_api_key"],
        "redirect_uri": redirect,
        "code": code,
    }
    secret = cfg.get("kakao_client_secret")
    if secret and not _is_blank(secret):
        payload["client_secret"] = secret

    res = requests.post(KAKAO_TOKEN_URL, data=payload, timeout=15)

    if res.status_code != 200:
        print("[오류] 토큰 발급 실패 (%d): %s" % (res.status_code, res.text))
        body = res.text
        if "KOE010" in body or "invalid_client" in body:
            print()
            print("  → 이 앱은 Client Secret 이 '사용함' 으로 켜져 있습니다.")
            print("     아래 둘 중 하나를 하시면 됩니다.")
            print()
            print("     [방법 1 — 간단] Client Secret 끄기")
            print("       카카오 디벨로퍼스 > 내 애플리케이션 > 앱 선택 >")
            print("       제품 설정 > 카카오 로그인 > 보안 >")
            print("       Client Secret 상태를 '사용 안 함' 으로 변경")
            print()
            print("     [방법 2] Client Secret 값을 등록하기")
            print("       위와 같은 화면에서 Client Secret 코드를 복사한 뒤")
            print("       '5_설정변경.bat' 을 실행해 입력하세요")
            print()
            print("     둘 중 하나를 마친 뒤 '1_최초설정.bat' 을 다시 실행하세요.")
        elif "KOE006" in body or "redirect_uri" in body:
            print()
            print("  → Redirect URI 불일치입니다. 카카오 디벨로퍼스 >")
            print("     제품 설정 > 카카오 로그인 > Redirect URI 에")
            print("     아래를 정확히 그대로 등록했는지 확인하세요.")
            print("     %s" % redirect)
        elif "KOE003" in body or "client_id" in body:
            print()
            print("  → REST API 키가 잘못됐습니다. '5_설정변경.bat' 으로 다시 입력하세요.")
            print("     (네이티브 앱 키나 JavaScript 키가 아니라 REST API 키입니다)")
        sys.exit(1)

    tok = res.json()
    tok["obtained_at"] = int(time.time())
    save_json(TOKEN_PATH, tok)

    print()
    print(" 토큰을 저장했습니다 -> %s" % TOKEN_PATH)
    print(" 리프레시 토큰 유효기간: 약 %d일" % (tok.get("refresh_token_expires_in", 5184000) // 86400))
    print()

    # Gmail 접속도 함께 검증
    print(" Gmail 접속을 확인합니다...")
    try:
        conn = _imap_connect(cfg)
        conn.logout()
        print(" Gmail 접속 정상.")
    except Exception as e:
        print(" [경고] Gmail 접속 실패: %s" % e)
        print("        2단계 인증을 켠 뒤 '앱 비밀번호'를 발급받아 config.json 에 넣어주세요.")
        print("        (계정 비밀번호가 아니라 16자리 앱 비밀번호입니다)")

    print()
    print(" 설정 완료. 이제 'python kakao_relay.py test' 로 테스트해보세요.")
    print()


# ─────────────────────────────────────────────────────────────
# 2) 카카오 토큰 관리 + 전송
# ─────────────────────────────────────────────────────────────
def get_access_token(cfg):
    tok = load_json(TOKEN_PATH)

    # tokens.json 이 없으면 환경변수의 리프레시 토큰으로 발급받습니다
    env_refresh = os.environ.get("KAKAO_REFRESH_TOKEN", "").strip()
    if not tok and env_refresh:
        tok = {"refresh_token": env_refresh, "obtained_at": 0, "expires_in": 0}

    if not tok:
        raise RuntimeError("토큰이 없습니다. 먼저 'python kakao_relay.py setup' 을 실행하거나 "
                           "KAKAO_REFRESH_TOKEN 환경변수를 설정하세요.")

    age = int(time.time()) - tok.get("obtained_at", 0)
    # 액세스 토큰은 보통 6시간(21600초). 5분 여유를 두고 갱신합니다.
    if age < tok.get("expires_in", 21600) - 300:
        return tok["access_token"]

    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError("리프레시 토큰이 없습니다. setup 을 다시 실행하세요.")

    log("액세스 토큰 갱신 중...")
    payload = {
        "grant_type": "refresh_token",
        "client_id": cfg["kakao_rest_api_key"],
        "refresh_token": refresh,
    }
    secret = cfg.get("kakao_client_secret")
    if secret and not _is_blank(secret):
        payload["client_secret"] = secret

    res = requests.post(KAKAO_TOKEN_URL, data=payload, timeout=15)

    if res.status_code != 200:
        raise RuntimeError("토큰 갱신 실패 (%d): %s\n"
                           "리프레시 토큰이 만료됐을 수 있습니다. setup 을 다시 실행하세요."
                           % (res.status_code, res.text))

    new = res.json()
    tok["access_token"] = new["access_token"]
    tok["expires_in"] = new.get("expires_in", 21600)
    tok["obtained_at"] = int(time.time())
    if "refresh_token" in new:  # 카카오는 리프레시 토큰이 1개월 미만 남았을 때만 새로 줍니다
        tok["refresh_token"] = new["refresh_token"]
        log("★ 새 리프레시 토큰이 발급됐습니다. 무인 실행 중이라면 "
            "KAKAO_REFRESH_TOKEN 시크릿을 아래 값으로 갱신하세요:")
        log("  %s" % new["refresh_token"])
        tok["refresh_token_expires_in"] = new.get("refresh_token_expires_in",
                                                  tok.get("refresh_token_expires_in", 5184000))
    save_json(TOKEN_PATH, tok)
    log("토큰 갱신 완료.")
    return tok["access_token"]


def split_for_kakao(text, limit=KAKAO_TEXT_LIMIT, max_parts=8):
    """카카오 200자 제한에 맞춰 자릅니다. 가능하면 줄 단위로 끊습니다."""
    text = text.strip()
    if not text:
        return []

    chunks = []
    buf = ""
    for line in text.split("\n"):
        # 한 줄 자체가 limit 을 넘으면 강제로 쪼갭니다
        while len(line) > limit:
            if buf:
                chunks.append(buf.rstrip())
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) <= limit:
            buf = candidate
        else:
            chunks.append(buf.rstrip())
            buf = line

    if buf.strip():
        chunks.append(buf.rstrip())

    if len(chunks) > max_parts:
        kept = chunks[:max_parts]
        kept[-1] = kept[-1][:limit - 20].rstrip() + "\n…(이하 생략, 메일 확인)"
        chunks = kept

    if len(chunks) > 1:
        total = len(chunks)
        chunks = ["(%d/%d)\n%s" % (i + 1, total, c) for i, c in enumerate(chunks)]

    return chunks


def extract_kakao_link(body):
    """메일 본문에서 리포트 주소를 뽑아냅니다.

    '===KAKAO_LINK=== https://docs.google.com/...' 형태를 찾습니다.
    없으면 None 을 돌려주고, 그 경우 설정의 기본 링크를 씁니다.
    """
    if not body or KAKAO_LINK not in body:
        return None
    tail = body.split(KAKAO_LINK, 1)[1]
    m = re.search(r"https?://\S+", tail[:900])
    if not m:
        return None
    url = m.group(0).rstrip(").,>\"'")
    return _unwrap_google_redirect(url)


def _unwrap_google_redirect(url):
    """Gmail 이 감싼 리디렉션 주소를 원래 주소로 되돌립니다.

    Gmail 은 본문 링크를 https://www.google.com/url?q=<원본>&source=gmail...
    형태로 바꿔 저장합니다. 그대로 쓰면 도메인이 www.google.com 이 되어
    카카오가 링크를 막습니다.
    """
    for _ in range(3):                       # 이중으로 감싸인 경우까지
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return url
        host = (parsed.netloc or "").lower()
        if host.endswith("google.com") and parsed.path in ("/url", "/urlq"):
            qs = urllib.parse.parse_qs(parsed.query)
            inner = (qs.get("q") or qs.get("url") or [None])[0]
            if inner and inner.startswith("http"):
                url = urllib.parse.unquote(inner)
                continue
        break
    return url.rstrip("&")


def extract_kakao_data(body):
    """메일 본문의 ===KAKAO_DATA=== ~ ===KAKAO_DATA_END=== 사이 JSON 을 읽습니다."""
    if not body or KAKAO_DATA not in body:
        return None
    try:
        raw = body.split(KAKAO_DATA, 1)[1]
        raw = raw.split(KAKAO_DATA_END, 1)[0] if KAKAO_DATA_END in raw else raw
        raw = raw.strip()
        # 메일러가 넣은 인용부호(>)나 코드펜스 제거
        raw = re.sub(r"(?m)^\s*>\s?", "", raw)
        raw = re.sub(r"```[a-z]*", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return None
        return json.loads(raw[start:end + 1])
    except Exception as e:
        log("카드 데이터 해석 실패: %s" % e)
        return None


def upload_kakao_image(cfg, path):
    """이미지를 카카오 서버에 올리고 (url, w, h) 를 돌려줍니다."""
    token = get_access_token(cfg)
    with open(path, "rb") as fh:
        res = requests.post(KAKAO_IMG_URL,
                            headers={"Authorization": "Bearer " + token},
                            files={"file": (os.path.basename(path), fh, "image/png")},
                            timeout=60)
    if res.status_code != 200:
        raise RuntimeError("이미지 업로드 실패 [%d]: %s" % (res.status_code, res.text[:300]))
    info = (res.json().get("infos") or {}).get("original") or {}
    if not info.get("url"):
        raise RuntimeError("이미지 업로드 응답에 url 이 없습니다: %s" % res.text[:200])
    return info["url"], info.get("width", 640), info.get("height", 1200)


def send_kakao_image(cfg, image_url, size, title, desc, link=None):
    """feed 템플릿으로 채팅방에 사진을 띄웁니다."""
    token = get_access_token(cfg)
    web = link or cfg.get("kakao_link_url") or DEFAULTS["kakao_link_url"]
    template = {
        "object_type": "feed",
        "content": {
            "title": title[:180],
            "description": (desc or "")[:380],
            "image_url": image_url,
            "image_width": int(size[0]),
            "image_height": int(size[1]),
            "link": {"web_url": web, "mobile_web_url": web},
        },
    }
    res = requests.post(
        KAKAO_SEND_URL,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=20)
    if res.status_code != 200:
        raise RuntimeError("사진 전송 실패 [%d]: %s" % (res.status_code, res.text[:300]))
    return True


def send_kakao(cfg, text, link=None):
    """카카오톡 '나에게 보내기'. 성공 시 전송한 메시지 수를 반환.

    link 를 주면 '자세히 보기' 버튼이 그 주소를 엽니다.
    ※ 해당 도메인이 카카오 앱의 [제품 링크 관리] > 웹 도메인 에
      등록돼 있어야 버튼이 정상 동작합니다.
    """
    token = get_access_token(cfg)
    parts = split_for_kakao(text, max_parts=cfg.get("max_messages_per_brief", 8))
    if not parts:
        return 0

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }

    sent = 0
    for i, part in enumerate(parts):
        web = link or cfg.get("kakao_link_url") or DEFAULTS["kakao_link_url"]
        mobile = link or cfg.get("kakao_link_mobile_url") or web
        template = {
            "object_type": "text",
            "text": part,
            "link": {"web_url": web, "mobile_web_url": mobile},
        }
        res = requests.post(
            KAKAO_SEND_URL,
            headers=headers,
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=15,
        )
        if res.status_code != 200:
            raise RuntimeError("카카오 전송 실패 (%d/%d) [%d]: %s"
                               % (i + 1, len(parts), res.status_code, res.text))
        sent += 1
        if i < len(parts) - 1:
            time.sleep(cfg.get("send_delay_sec", 1.2))

    return sent


# ─────────────────────────────────────────────────────────────
# 3) Gmail 읽기
# ─────────────────────────────────────────────────────────────
def _imap_connect(cfg, timeout=25):
    """Gmail IMAP 접속. 응답이 없을 때 무한정 멈추지 않도록 타임아웃을 겁니다."""
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        try:
            conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=timeout)
        except TypeError:
            # 파이썬 3.8 이하는 timeout 인자를 받지 않습니다
            conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
        conn.login(cfg["gmail_address"], cfg["gmail_app_password"].replace(" ", ""))
        return conn
    finally:
        socket.setdefaulttimeout(prev)


# 메일러가 실제 인코딩 대신 붙이는 가짜 라벨들. 파이썬 코덱에 없습니다.
_BOGUS_CHARSETS = {"unknown-8bit", "unknown", "x-unknown", "8bit", "binary", "none", ""}


def _safe_decode(data, charset=None):
    """어떤 인코딩 라벨이 와도 절대 예외를 던지지 않고 문자열을 돌려줍니다."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data

    label = (charset or "").strip().lower()
    candidates = []
    if label and label not in _BOGUS_CHARSETS:
        candidates.append(label)
    # 한국어 메일에서 흔한 순서대로 시도합니다
    candidates += ["utf-8", "cp949", "euc-kr", "latin-1"]

    for enc in candidates:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError, AttributeError):
            continue
    # 마지막 방어선 — 깨진 바이트는 대체 문자로
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return str(data)


def _decode_header(raw):
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)

    out = []
    for chunk, enc in parts:
        out.append(_safe_decode(chunk, enc) if isinstance(chunk, bytes) else chunk)
    return "".join(out)


def _extract_plain_body(msg):
    """멀티파트 메일에서 text/plain 을 우선 추출. 없으면 html 을 텍스트로 변환."""
    plain, htmlbody = None, None

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = _safe_decode(payload, part.get_content_charset())
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and htmlbody is None:
                htmlbody = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = _safe_decode(payload, msg.get_content_charset()) if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            htmlbody = text
        else:
            plain = text

    if plain:
        return plain
    if htmlbody:
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", "", htmlbody)
        stripped = re.sub(r"(?i)<br\s*/?>", "\n", stripped)
        stripped = re.sub(r"(?i)</p>", "\n", stripped)
        stripped = re.sub(r"(?s)<[^>]+>", "", stripped)
        return html.unescape(stripped)
    return ""


def extract_kakao_section(body):
    """===KAKAO_START=== / ===KAKAO_END=== 사이를 뽑아냅니다. 없으면 본문 정리본."""
    if KAKAO_START in body and KAKAO_END in body:
        section = body.split(KAKAO_START, 1)[1].split(KAKAO_END, 1)[0]
        return section.strip()

    # 구분자가 없으면 본문 전체를 쓰되, 서명·인용부는 잘라냅니다
    cleaned = body
    for marker in ("\n-- \n", "\nOn ", "\n> "):
        idx = cleaned.find(marker)
        if idx > 100:
            cleaned = cleaned[:idx]
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(days_ago):
    """IMAP SEARCH 용 날짜 문자열 (예: 15-Aug-2026)."""
    t = time.localtime(time.time() - days_ago * 86400)
    return "%02d-%s-%d" % (t.tm_mday, _IMAP_MONTHS[t.tm_mon - 1], t.tm_year)


def _search_candidates(conn, cfg, max_scan, days=3):
    """확인할 메일 번호 목록을 최대한 좁혀서 돌려줍니다.

    '안 읽음' 조건은 쓰지 않습니다. Gmail 은 본인이 본인에게 보낸 메일을
    자동으로 읽음 처리하기 때문에, 그 조건으로는 브리핑을 영영 못 찾습니다.
    대신 최근 며칠치를 훑고, 이미 보낸 것은 Message-ID 로 걸러냅니다.
    """
    me = cfg.get("gmail_address", "")
    since = _imap_date(days)
    uids = []

    # 1차: 최근 며칠 + 본인이 보낸 메일
    if me:
        try:
            typ, data = conn.search(None, "SINCE", since, "FROM", '"%s"' % me)
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
        except Exception:
            uids = []

    # 2차: 못 찾으면 최근 며칠치 전체
    if not uids:
        try:
            typ, data = conn.search(None, "SINCE", since)
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
        except Exception:
            uids = []

    total = len(uids)
    if total > max_scan:
        uids = uids[-max_scan:]      # 최신 것이 중요하므로 뒤에서 자릅니다
        log("최근 %d일 메일 %d통 중 최신 %d통만 확인합니다." % (days, total, max_scan))
    elif total:
        log("최근 %d일 메일 %d통 확인 중..." % (days, total))
    else:
        log("최근 %d일 안에 메일이 없습니다." % days)

    return uids


def _headers_of(conn, uid):
    """제목과 Message-ID 만 가볍게 읽습니다. 본문은 받지 않습니다."""
    typ, data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID)])")
    if typ != "OK" or not data or not data[0]:
        return "", ""
    raw = data[0][1] if isinstance(data[0], tuple) else b""
    text = _safe_decode(raw, "utf-8")
    text = re.sub(r"\r?\n[ \t]+", " ", text)   # 여러 줄로 접힌 헤더 펴기

    m = re.search(r"(?im)^subject:\s*(.*)$", text)
    subject = _decode_header(m.group(1).strip()) if m else ""

    m = re.search(r"(?im)^message-id:\s*(.*)$", text)
    msgid = m.group(1).strip() if m else ""

    return subject, msgid


def fetch_new_briefs(cfg, already_sent=(), max_scan=100, force=False):
    """아직 전달하지 않은 브리핑 메일을 (uid, 제목, 카톡본문, msgid) 로 반환.

    제목 헤더로 먼저 걸러낸 뒤, 해당하는 메일의 본문만 받아옵니다.
    이미 보낸 메일은 Message-ID 로 걸러내므로 읽음/안읽음과 무관합니다.
    """
    already_sent = set(already_sent or ())
    conn = _imap_connect(cfg)
    try:
        conn.select("INBOX")
        uids = _search_candidates(conn, cfg, max_scan)
        if not uids:
            return []

        # 1단계 — 제목과 Message-ID 만 확인 (가벼움)
        matched, skipped = [], 0
        for i, uid in enumerate(uids, 1):
            try:
                subject, msgid = _headers_of(conn, uid)
            except Exception as e:
                log("제목 확인 실패 (uid=%s): %s" % (uid, e))
                continue
            if subject and any(p in subject for p in SUBJECT_PREFIXES):
                if (not force) and msgid and msgid in already_sent:
                    skipped += 1
                    log("  건너뜀(이미 전달): %s" % subject)
                else:
                    matched.append((uid, subject, msgid))
            if i % 25 == 0:
                log("  ... %d/%d 확인" % (i, len(uids)))

        if not matched:
            return []
        log("새 브리핑 %d통 발견. 본문을 가져옵니다." % len(matched))

        # 2단계 — 해당 메일의 본문만 받아옵니다
        results = []
        for uid, subject, msgid in matched:
            try:
                typ, msgdata = conn.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                body = _extract_plain_body(msg)
                kakao_text = extract_kakao_section(body)
                if not kakao_text:
                    log("카톡 구간을 찾지 못했습니다: %s" % subject)
                    continue
                results.append((uid, subject, kakao_text, msgid,
                                extract_kakao_link(body),
                                extract_kakao_data(body)))
            except Exception as e:
                log("메일 한 통을 건너뜁니다 (uid=%s): %s" % (uid, e))
                continue

        return results
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


def mark_seen(cfg, uid):
    conn = _imap_connect(cfg)
    try:
        conn.select("INBOX")
        conn.store(uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 4) 명령
# ─────────────────────────────────────────────────────────────
def cmd_status():
    """지금 무엇이 준비됐고 무엇이 빠졌는지 한눈에 보여줍니다."""
    print()
    print("=" * 62)
    print("  릴레이 상태 확인   (버전 %s / %s)" % (VERSION, BUILD_DATE))
    print("=" * 62)
    print()

    ok = True

    # 1) 설정 파일
    cfg = load_json(CONFIG_PATH) or {}
    missing = [k for k in REQUIRED if _is_blank(cfg.get(k))]
    if not cfg:
        print("  [ ] 설정 파일       config.json 이 아직 없습니다")
        ok = False
    elif missing:
        print("  [ ] 설정 파일       미입력: %s"
              % ", ".join(FIELD_LABELS[k] for k in missing))
        ok = False
    else:
        key = str(cfg.get("kakao_rest_api_key", ""))
        secret = cfg.get("kakao_client_secret")
        secret_note = "Client Secret 사용" if (secret and not _is_blank(secret)) else "Client Secret 미사용"
        print("  [O] 설정 파일       카카오 키 %s… / %s"
              % (key[:6], cfg.get("gmail_address")))
        print("                      %s" % secret_note)

    # 2) 카카오 토큰
    tok = load_json(TOKEN_PATH)
    if not tok or not tok.get("access_token"):
        print("  [ ] 카카오 인증     아직 인증하지 않았습니다")
        ok = False
    else:
        age_days = (int(time.time()) - tok.get("obtained_at", 0)) // 86400
        left = tok.get("refresh_token_expires_in", 5184000) // 86400 - age_days
        print("  [O] 카카오 인증     완료 (재인증까지 약 %d일 남음)" % max(left, 0))

    # 3) Gmail 접속
    if not missing and cfg:
        try:
            conn = _imap_connect(cfg)
            conn.logout()
            print("  [O] Gmail 접속      정상")
        except Exception as e:
            print("  [ ] Gmail 접속      실패: %s" % str(e)[:80])
            print("      → 앱 비밀번호 16자리가 맞는지 확인하세요 (계정 비밀번호 아님)")
            ok = False
    else:
        print("  [ ] Gmail 접속      설정이 끝나야 확인할 수 있습니다")

    print()
    if ok:
        print("  모두 준비됐습니다. '2_테스트.bat' 으로 카톡 전송을 확인해보세요.")
    else:
        print("  위에서 [ ] 표시된 항목을 먼저 해결해야 합니다.")
        print("  대부분 '1_최초설정.bat' 을 다시 실행하면 됩니다.")
    print()
    return 0 if ok else 1


def _explain_no_token():
    print()
    print("-" * 62)
    print("  카카오 인증이 아직 끝나지 않았습니다.")
    print("-" * 62)
    print()
    print("  '1_최초설정.bat' 을 실행해 브라우저에서 카카오 로그인과")
    print("  '동의하고 계속하기' 까지 마쳐야 합니다.")
    print()
    print("  이미 실행하셨는데도 이 메시지가 나온다면, 인증 도중")
    print("  아래 중 하나에서 막혔을 가능성이 높습니다.")
    print()
    print("   1. 카카오 디벨로퍼스 > 제품 설정 > 카카오 로그인 > 동의항목")
    print("      '카카오톡 메시지 전송' 이 '이용 중 동의' 로 켜져 있는지")
    print("   2. 같은 화면의 Redirect URI 에 아래 주소가 정확히 등록됐는지")
    print("      http://localhost:5959/oauth")
    print("   3. 카카오 로그인 자체가 '활성화 ON' 인지")
    print()
    print("  세 가지를 확인한 뒤 '1_최초설정.bat' 을 다시 실행하세요.")
    print("  현재 상태는 '6_상태확인.bat' 으로 볼 수 있습니다.")
    print()


def cmd_test():
    cfg = load_config()
    now = datetime.now().strftime("%m/%d %H:%M")
    text = (
        "카카오톡 릴레이 테스트\n"
        "━━━━━━━━━━━\n"
        "이 메시지가 보이면 연결이 정상입니다.\n"
        "앞으로 아침 브리핑(07:00), 장중 알림(09~15시),\n"
        "미국장 마감 결산(06:00)이 이 채팅방으로 옵니다.\n"
        "━━━━━━━━━━━\n"
        "%s 발송\n"
        "※ 정보 제공 목적, 투자 권유 아님" % now
    )
    try:
        n = send_kakao(cfg, text)
    except RuntimeError as e:
        if "토큰이 없습니다" in str(e) or "리프레시 토큰" in str(e):
            _explain_no_token()
            return 1
        log("전송 실패: %s" % e)
        if "insufficient scope" in str(e) or "scope" in str(e).lower():
            print()
            print("  → 카카오 디벨로퍼스의 동의항목에서 '카카오톡 메시지 전송' 을")
            print("     '이용 중 동의' 로 켠 뒤 '1_최초설정.bat' 을 다시 실행하세요.")
            print()
        return 1
    log("테스트 메시지 %d건 전송 완료. 카카오톡 '나와의 채팅'을 확인하세요." % n)
    return 0


def _send_card(cfg, card, subject, link):
    """카드 데이터를 이미지로 그려 카톡 채팅방에 사진으로 보냅니다."""
    try:
        from card_render import render_card
    except ImportError as e:
        log("card_render.py 를 불러오지 못했습니다 (%s). 'pip install pillow' 후 "
            "card_render.py 를 같은 폴더에 두세요." % e)
        return False

    path = os.path.join(BASE_DIR, "card.png")
    log("카드 이미지를 그리는 중...")
    render_card(card, path)
    log("  %d KB 생성" % (os.path.getsize(path) // 1024))

    # PC 에도 날짜별 사본을 남깁니다 (cards 폴더에서 언제든 크게 보기/공유 가능)
    try:
        cards_dir = os.path.join(BASE_DIR, "cards")
        os.makedirs(cards_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        name = re.sub(r"[^0-9A-Za-z가-힣_]+", "_",
                      (card.get("title") or "카드")).strip("_") or "카드"
        saved = os.path.join(cards_dir, "%s_%s.png" % (name, stamp))
        with open(path, "rb") as a, open(saved, "wb") as b:
            b.write(a.read())
        log("  사본 저장: cards\\%s" % os.path.basename(saved))
    except Exception as e:
        log("  사본 저장 실패(무시): %s" % e)

    url, w, h = upload_kakao_image(cfg, path)
    log("  이미지 주소: %s" % url)
    title = card.get("title") or subject
    if card.get("date"):
        title = "%s %s" % (title, card["date"])
    desc = card.get("summary") or ""
    if not desc and card.get("pnl") is not None:
        sign = "+" if float(card["pnl"]) >= 0 else ""
        desc = "총 평가손익 %s%s원 (%s%.2f%%)" % (
            sign, "{:,}".format(int(card["pnl"])), sign, float(card.get("pnl_pct", 0)))
    # '자세히 보기' 를 누르면 이미지가 원본 크기로 열립니다.
    # (브라우저에서 확대하거나 길게 눌러 저장할 수 있습니다)
    if cfg.get("card_link_to_image", True):
        link = url
    send_kakao_image(cfg, url, (w, h), title, desc, link=link)
    return True


def cmd_run(force=False, latest_only=False):
    cfg = load_config()
    state = load_json(STATE_PATH, {"sent": []}) or {"sent": []}

    tok = load_json(TOKEN_PATH)
    has_env_refresh = bool(os.environ.get("KAKAO_REFRESH_TOKEN", "").strip())
    if not has_env_refresh and (not tok or not tok.get("access_token")):
        _explain_no_token()
        return 1

    log("Gmail 접속 중...")
    try:
        briefs = fetch_new_briefs(cfg, already_sent=state.get("sent", []),
                                  force=force)
    except imaplib.IMAP4.error as e:
        log("Gmail 로그인 실패: %s" % e)
        log("  → 2단계 인증을 켜고 '앱 비밀번호' 16자리를 넣었는지 확인하세요.")
        return 1
    except Exception as e:
        log("Gmail 접속 오류: %s" % e)
        return 1

    if not briefs:
        log("새 브리핑 없음.")
        return 0

    if latest_only:
        briefs = briefs[-1:]        # 가장 최근 것 한 통만
        log("가장 최근 브리핑 1통만 전송합니다: %s" % briefs[0][1])

    ok = 0
    for uid, subject, text, msgid, link, card in briefs:
        # Message-ID 가 있으면 그것으로, 없으면 제목으로 중복을 막습니다
        key = msgid or ("subject|%s" % subject)
        if key in state["sent"] and not force:
            continue
        try:
            sent_img = False
            if card:
                try:
                    sent_img = _send_card(cfg, card, subject, link)
                except Exception as ce:
                    log("사진 전송 실패(요약 텍스트로 대체): %s" % ce)
            n = 0
            if (not sent_img) or cfg.get("send_text_with_image", True):
                n = send_kakao(cfg, text, link=link)
            log("전송 완료 [%s] → %s%s"
                % (subject,
                   ("사진 1건" if sent_img else "") +
                   (" + " if sent_img and n else "") +
                   ("텍스트 %d건" % n if n else ""),
                   " (링크 포함)" if link else ""))
            state["sent"].append(key)
            state["sent"] = state["sent"][-300:]  # 최근 300건만 보관
            save_json(STATE_PATH, state)
            ok += 1
            try:
                mark_seen(cfg, uid)
            except Exception:
                pass  # 읽음 표시는 실패해도 무방합니다
        except Exception as e:
            log("전송 실패 [%s]: %s" % (subject, e))

    return 0 if ok else 1


def cmd_watch():
    cfg = load_config()
    interval = cfg.get("watch_interval_sec", 60)
    log("상주 모드 시작 (%d초 간격). 종료하려면 Ctrl+C." % interval)
    while True:
        try:
            cmd_run()
        except KeyboardInterrupt:
            log("종료합니다.")
            return 0
        except Exception as e:
            log("예기치 못한 오류: %s" % e)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("종료합니다.")
            return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "setup":
        cmd_setup()
    elif cmd == "status":
        sys.exit(cmd_status())
    elif cmd == "config":
        # 기존 값을 지우고 처음부터 다시 입력받습니다
        existing = load_json(CONFIG_PATH) or {}
        for k in REQUIRED + OPTIONAL:
            if k != "gmail_address":
                existing.pop(k, None)
        prompt_for_config(existing)
        print("  이어서 'python kakao_relay.py setup' 으로 카카오 인증을 진행하세요.")
    elif cmd == "test":
        sys.exit(cmd_test())
    elif cmd == "run":
        sys.exit(cmd_run())
    elif cmd == "resend":
        # 이미 보냈더라도 가장 최근 브리핑 한 통을 다시 보냅니다
        sys.exit(cmd_run(force=True, latest_only=True))
    elif cmd == "watch":
        sys.exit(cmd_watch())
    else:
        print()
        print("=" * 62)
        print("  알 수 없는 명령: %r" % cmd)
        print("=" * 62)
        print()
        print("  이 파일의 버전: %s (%s)" % (VERSION, BUILD_DATE))
        print()
        print("  사용할 수 있는 명령")
        print("    setup    설정값 입력 + 카카오 인증   (1_최초설정.bat)")
        print("    status   현재 상태 점검             (6_상태확인.bat)")
        print("    test     테스트 메시지 전송          (2_테스트.bat)")
        print("    run      메일 확인 후 카톡 전달      (3_한번실행.bat)")
        print("    watch    상주 모드                  (4_상주모드.bat)")
        print("    config   설정값 다시 입력            (5_설정변경.bat)")
        print("    resend   최근 브리핑 강제 재전송      (7_다시보내기.bat)")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
