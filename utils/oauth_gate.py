"""중계 링크에 디스코드 신원 확인을 붙이는 게이트.

중계 토큰만으로는 **누가 눌렀는지** 알 수 없습니다. 그래서 토큰 주소를
받은 사람이 쓰기 전에 다른 사람에게 넘기면 그대로 통합니다.

이 모듈은 그 구멍을 막습니다.

  첫 클릭  : /j/<token> → 쿠키 없음 → 디스코드 OAuth(identify) 동의
                        → 콜백에서 서명 쿠키에 디스코드 ID 저장
  이후     : /j/<token> → 쿠키의 ID == 토큰 발급 대상? → 즉시 통과

토큰은 발급 대상 유저 ID를 이미 들고 있으므로, "이 서버 멤버인가"보다
강한 **"발급받은 본인인가"** 를 확인합니다.

환경 변수가 갖춰지지 않으면 게이트는 **꺼진 채로** 동작하고, 기존처럼
토큰만으로 통과시킵니다. 설정 전에 배포해도 입장이 막히지 않습니다.
"""

import os
import time
import hashlib
import http.client
import urllib.parse
import urllib.request
import urllib.error
import json

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# 쿠키 수명 — 이 기간 동안은 동의 화면 없이 즉시 통과합니다.
COOKIE_NAME = "hbh_uid"
COOKIE_MAX_AGE = 30 * 24 * 3600      # 30일

# OAuth 왕복 중 상태(state)를 담는 쿠키의 수명
STATE_MAX_AGE = 600                   # 10분

DISCORD_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_ME = "https://discord.com/api/users/@me"

# 디스코드 API 는 Cloudflare 뒤에 있고, 파이썬 기본 User-Agent
# (Python-urllib/3.x)는 시그니처 차단(HTTP 403, error code 1010)에 걸립니다.
# 디스코드 문서도 모든 API 요청에 User-Agent 를 요구합니다.
USER_AGENT = "KRBiomeHuntBot (https://github.com/windswepty/KRBiomeHuntBot, 1.0)"


def client_id() -> str:
    return (os.getenv("DISCORD_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()


def _signing_key() -> str | None:
    """쿠키 서명 키.

    SECRET_KEY 가 있으면 그걸 쓰고, 없으면 Client Secret 에서 파생합니다.
    (그냥 재사용하지 않고 용도 문자열을 섞어 분리합니다)
    Client Secret 을 재발급하면 기존 쿠키는 무효가 되어 한 번 더 동의하면 됩니다.
    """
    explicit = (os.getenv("SECRET_KEY") or "").strip()
    if explicit:
        return explicit
    secret = client_secret()
    if secret:
        return hashlib.sha256(b"hbh-oauth-cookie-v1|" + secret.encode()).hexdigest()
    return None


def enabled() -> bool:
    """게이트를 켤 수 있는 상태인지."""
    return bool(client_id() and client_secret() and _signing_key())


def _serializer() -> URLSafeTimedSerializer | None:
    key = _signing_key()
    return URLSafeTimedSerializer(key, salt="hbh-gate") if key else None


# ── 쿠키 ──────────────────────────────────────────────────────────────

def sign_user(user_id: int) -> str | None:
    s = _serializer()
    return s.dumps(str(user_id)) if s else None


def read_user(cookie_value: str | None) -> int | None:
    """쿠키에서 디스코드 유저 ID를 꺼냅니다. 위조·만료면 None."""
    if not cookie_value:
        return None
    s = _serializer()
    if s is None:
        return None
    try:
        raw = s.loads(cookie_value, max_age=COOKIE_MAX_AGE)
        return int(raw)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def sign_state(payload: str) -> str | None:
    s = _serializer()
    return s.dumps(payload) if s else None


def read_state(value: str | None) -> str | None:
    if not value:
        return None
    s = _serializer()
    if s is None:
        return None
    try:
        return s.loads(value, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


# ── OAuth 흐름 ────────────────────────────────────────────────────────

def authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # 신원 확인에 필요한 최소 권한만 요청합니다.
        # 가입한 서버 목록(guilds)은 요청하지 않습니다.
        "scope": "identify",
        "state": state,
        # prompt 는 지정하지 않습니다. 디스코드는 이미 승인한 앱·같은 권한이면
        # 알아서 동의 화면을 건너뜁니다. prompt=none 을 주면 아직 승인하지
        # 않은 유저에게 동의 화면 대신 오류가 돌아올 수 있습니다.
    }
    return f"{DISCORD_AUTHORIZE}?{urllib.parse.urlencode(params)}"


def _post(url: str, data: dict, headers: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def exchange_code(code: str, redirect_uri: str) -> int | None:
    """인가 코드를 교환해 디스코드 유저 ID를 얻습니다. 실패하면 None."""
    try:
        token = _post(
            DISCORD_TOKEN,
            {
                "client_id": client_id(),
                "client_secret": client_secret(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        if not isinstance(token, dict):
            print(f"[OAuthGate] 예상치 못한 응답 형태: {type(token).__name__}", flush=True)
            return None
        access = token.get("access_token")
        if not access:
            return None
        req = urllib.request.Request(
            DISCORD_ME,
            headers={
                "Authorization": f"Bearer {access}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
        with urllib.request.urlopen(req, timeout=10) as resp:
            me = json.loads(resp.read().decode())
        uid = me.get("id")
        return int(uid) if uid else None
    except urllib.error.HTTPError as e:
        # 디스코드가 알려주는 사유를 그대로 남깁니다.
        # (redirect_uri 불일치, client_secret 오류 등이 여기서 드러납니다)
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        print(f"[OAuthGate] 코드 교환 실패 HTTP {e.code}: {detail}", flush=True)
        return None
    except (OSError, http.client.HTTPException,
            ValueError, KeyError, TypeError, AttributeError) as e:
        # URLError 는 OSError 의 하위라 여기에 함께 잡힙니다. 응답을 기다리다
        # 나는 TimeoutError / RemoteDisconnected / SSLError 는 URLError 로
        # 감싸이지 않아, 예전 목록으로는 새어나가 Flask 500 이 됐습니다.
        # (그러면 준비해 둔 안내 페이지도, 진단 로그도 나오지 않습니다)
        print(f"[OAuthGate] 코드 교환 실패: {type(e).__name__}: {e}", flush=True)
        return None


# ── 서버 멤버·역할 확인 ───────────────────────────────────────────────
#
# 봇이 자기 캐시로 판정하는 함수를 여기에 꽂아둡니다.
# Flask 스레드에서 호출되므로, 꽂는 쪽에서 **동기이고 가벼운** 함수를 주세요.

_member_gate = None


def set_member_gate(fn) -> None:
    """fn(guild_id: int, user_id: int) -> tuple[bool, str] 형태."""
    global _member_gate
    _member_gate = fn


def check_member(guild_id: int, user_id: int) -> tuple[bool, str]:
    """서버 멤버이고 필요한 역할을 갖고 있는지. 판정기가 없으면 통과시킵니다."""
    if _member_gate is None:
        return True, "ok"
    try:
        return _member_gate(guild_id, user_id)
    except Exception as e:
        # 판정기 오류로 정당한 유저를 막지 않습니다
        print(f"[OAuthGate] 멤버 확인 오류(통과 처리): {e}", flush=True)
        return True, "ok"
