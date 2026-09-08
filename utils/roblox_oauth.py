"""로블록스 계정 연동 — 공식 OAuth 2.0 (Sign in with Roblox).

개인서버 링크를 받아간 사람의 **로블록스 아이디**를 알아야, 게임 안
플레이어 목록과 대조해 유출로 들어온 사람을 찾아낼 수 있습니다.

닉네임을 그냥 입력받으면 남의 이름을 적어 사칭할 수 있으므로,
로블록스가 직접 신원을 보증하는 OAuth 로 소유권을 증명받습니다.

  /로블록스연동 → 로블록스 로그인·동의 → 콜백에서 sub(유저ID) 확보 → 저장

환경 변수가 없으면 이 경로는 **꺼진 채로** 동작하고, 명령어가 그 사실을
안내합니다. 봇의 다른 기능에는 영향이 없습니다.
"""

import os
import json
import time
import base64
import hashlib
import secrets
import threading
import urllib.parse
import urllib.request
import urllib.error
import http.client

AUTHORIZE = "https://apis.roblox.com/oauth/v1/authorize"
TOKEN = "https://apis.roblox.com/oauth/v1/token"
USERINFO = "https://apis.roblox.com/oauth/v1/userinfo"

# 신원 확인에 필요한 최소 권한만 요청합니다.
SCOPES = "openid profile"

# 디스코드에서 겪었듯(Cloudflare 403/1010), API 호출에는 User-Agent 를 반드시
# 지정합니다. 로블록스는 현재 기본 UA 도 통과시키지만 의존하지 않습니다.
USER_AGENT = "KRBiomeHuntBot (https://github.com/windswepty/KRBiomeHuntBot, 1.0)"

# 로그인 왕복 제한 시간
PENDING_TTL = 600          # 10분
MAX_PENDING = 2000

_lock = threading.Lock()
# state_id -> {"guild_id", "user_id", "verifier", "expires"}
_pending: dict[str, dict] = {}


def client_id() -> str:
    return (os.getenv("ROBLOX_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.getenv("ROBLOX_CLIENT_SECRET") or "").strip()


def enabled() -> bool:
    return bool(client_id() and client_secret())


def redirect_uri() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    return f"{base.rstrip('/')}/roblox/callback"


def _purge_locked(now: float) -> None:
    for k in [k for k, v in _pending.items() if v["expires"] <= now]:
        _pending.pop(k, None)
    if len(_pending) > MAX_PENDING:
        for k in list(_pending)[:len(_pending) - MAX_PENDING // 2]:
            _pending.pop(k, None)


def start(guild_id: int, user_id: int) -> tuple[str, str]:
    """로그인 URL과 state_id 를 만듭니다.

    PKCE 를 씁니다. code_verifier 는 브라우저에 노출되지 않도록 서버 메모리에
    두고, 브라우저에는 그것을 가리키는 임의의 state_id 만 보냅니다.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state_id = secrets.token_urlsafe(24)

    now = time.monotonic()
    with _lock:
        _purge_locked(now)
        _pending[state_id] = {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "verifier": verifier,
            "expires": now + PENDING_TTL,
        }

    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE}?{urllib.parse.urlencode(params)}", state_id


def take_pending(state_id: str) -> dict | None:
    """state 를 소비합니다(1회용). 없거나 만료면 None."""
    now = time.monotonic()
    with _lock:
        info = _pending.pop(state_id, None)
        _purge_locked(now)
        if info is None or info["expires"] <= now:
            return None
        return info


def _request(url: str, data: bytes | None, headers: dict, method: str) -> dict | None:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    return body if isinstance(body, dict) else None


def exchange(code: str, verifier: str) -> dict | None:
    """인가 코드를 교환해 로블록스 사용자 정보를 돌려줍니다.

    반환 예: {"roblox_id": 123, "roblox_name": "xXmonoXx",
              "display_name": "모노", "created_at": "2019-04-11T..."}
    """
    try:
        body = urllib.parse.urlencode({
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri(),
        }).encode()
        token = _request(TOKEN, body, {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }, "POST")
        if not token:
            return None
        access = token.get("access_token")
        if not access:
            print("[RobloxOAuth] 응답에 access_token 없음", flush=True)
            return None

        me = _request(USERINFO, None, {
            "Authorization": f"Bearer {access}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }, "GET")
        if not me:
            return None

        sub = me.get("sub")
        if not sub:
            print("[RobloxOAuth] userinfo 에 sub 없음", flush=True)
            return None
        return {
            "roblox_id": int(sub),
            "roblox_name": me.get("preferred_username") or me.get("nickname") or str(sub),
            "display_name": me.get("nickname") or me.get("name") or "",
            "created_at": me.get("created_at") or "",
        }
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        print(f"[RobloxOAuth] 토큰 교환 실패 HTTP {e.code}: {detail}", flush=True)
        return None
    except (OSError, http.client.HTTPException,
            ValueError, KeyError, TypeError, AttributeError) as e:
        # 응답 대기 중 나는 TimeoutError 등은 URLError 로 감싸이지 않으므로
        # OSError 로 넓게 잡습니다 (디스코드 게이트에서 겪은 것과 같은 함정).
        print(f"[RobloxOAuth] 토큰 교환 실패: {type(e).__name__}: {e}", flush=True)
        return None



# 사용자 이름 조회 (공개 API — 인증이 필요 없습니다)
USERS_BATCH = "https://users.roblox.com/v1/users"

# 한 번에 물어볼 수 있는 최대 인원
NAMES_BATCH_SIZE = 100


def fetch_names(roblox_ids: list) -> dict:
    """로블록스 아이디 → `{"name": 사용자명, "display": 표시이름}`.

    **저장해 둔 이름만 믿으면 대조가 어긋납니다.**
      · 로블록스는 개명이 가능해서 연동 당시 이름이 낡습니다.
      · **게임 안 플레이어 목록은 표시이름(displayName)을 보여줍니다.**
        사용자명만 가지고 맞춰 보면 대부분 안 맞습니다.

    OAuth 설정과 무관하게 동작합니다(로그인이 필요 없는 공개 API).
    실패하면 **빈 dict** 를 돌려줍니다 — 호출부는 저장된 이름으로 대조를
    계속하고 그 사실을 화면에 알립니다. 조용히 틀린 결과를 주는 것보다
    낫습니다.
    """
    out: dict[int, dict] = {}
    ids = []
    for value in roblox_ids or []:
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if num > 0 and num not in ids:
            ids.append(num)

    for start_at in range(0, len(ids), NAMES_BATCH_SIZE):
        chunk = ids[start_at:start_at + NAMES_BATCH_SIZE]
        try:
            body = json.dumps({"userIds": chunk,
                               "excludeBannedUsers": False}).encode()
            res = _request(USERS_BATCH, body, {
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }, "POST")
            for row in (res or {}).get("data") or []:
                rid = row.get("id")
                if rid is None:
                    continue
                out[int(rid)] = {"name": row.get("name") or "",
                                 "display": row.get("displayName") or ""}
        except urllib.error.HTTPError as e:
            print(f"[RobloxOAuth] 이름 조회 실패 HTTP {e.code}", flush=True)
        except (OSError, http.client.HTTPException,
                ValueError, KeyError, TypeError, AttributeError) as e:
            # 응답을 읽는 중에 나는 예외는 URLError 로 감싸이지 않습니다.
            print(f"[RobloxOAuth] 이름 조회 실패: {type(e).__name__}: {e}", flush=True)
    return out


def clear() -> None:
    """테스트용."""
    with _lock:
        _pending.clear()


# ── 저장 다리 (Flask 스레드 → asyncio 봇) ─────────────────────────────
#
# 콜백은 Flask 스레드에서 처리되는데 저장 함수는 async 입니다.
# 봇이 자기 이벤트 루프로 실행해 주는 함수를 여기에 꽂아둡니다.

_saver = None


def set_saver(fn) -> None:
    """fn(guild_id, user_id, info) -> tuple[bool, str] 형태.

    반환값은 (성공여부, 사유) 이며 사유는 화면 안내에 씁니다.
    """
    global _saver
    _saver = fn


def save_link(guild_id: int, user_id: int, info: dict) -> tuple[bool, str]:
    if _saver is None:
        return False, "not_ready"
    try:
        return _saver(guild_id, user_id, info)
    except Exception as e:
        print(f"[RobloxOAuth] 저장 실패: {type(e).__name__}: {e}", flush=True)
        return False, "error"
