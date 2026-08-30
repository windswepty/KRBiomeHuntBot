"""개인서버 링크 중계용 1회성 토큰 저장소.

로블록스 링크를 디스코드에 직접 노출하지 않기 위한 장치입니다.
봇은 인증을 통과한 유저에게 **토큰 URL**만 주고, 실제 링크는 이 저장소를
거쳐 서버가 리다이렉트로 넘겨줍니다.

  버튼 클릭 → (봇) 역할 검사 → 토큰 발급 → /j/<token> 링크 전달
            → 클릭 → (Flask) 토큰 검증 → 로블록스로 302

토큰은 **1회용**입니다. 한 번 쓰면 즉시 잠기고, 안 써도 짧은 시간 뒤
만료되므로 외부로 퍼뜨려도 쓸 수 없습니다.
유저가 다시 들어가야 하면 디스코드 버튼을 눌러 새 주소를 받습니다.

⚠️ 이 모듈은 **asyncio 봇 스레드와 Flask 스레드가 함께** 접근합니다.
   (keep_alive.py 의 Flask 가 같은 프로세스의 데몬 스레드에서 돌기 때문)
   그래서 모든 접근을 threading.Lock 으로 감쌉니다.
"""

import os
import time
import secrets
import threading

# 토큰 수명 — 자리 경쟁 중에 눌러 들어가기엔 충분하고,
# 외부로 퍼뜨렸을 때는 이미 늦을 만큼 짧게 잡습니다.
TOKEN_TTL_SECONDS = 90

# 허용 사용 횟수. 1 = 한 번 쓰면 즉시 잠깁니다.
# 브라우저가 재시도하거나 실행이 한 번 실패하면 그 링크는 그대로 죽으므로,
# 유저는 디스코드 버튼을 다시 눌러 새 주소를 받아야 합니다.
# 안내 페이지가 그 방법을 알려줍니다.
MAX_USES = 1

# 메모리 상한 (몰림 대비). 넘으면 오래된 것부터 버립니다.
MAX_TOKENS = 5000

_lock = threading.Lock()
# token -> {"url": str, "user_id": int, "guild_id": int,
#           "expires": float(monotonic), "uses": int}
_tokens: dict[str, dict] = {}


def public_base_url() -> str | None:
    """중계 주소의 기준 URL. 설정돼 있지 않으면 None.

    Render 는 서비스에 RENDER_EXTERNAL_URL 을 자동으로 넣어줍니다.
    직접 지정하고 싶으면 PUBLIC_BASE_URL 로 덮어쓸 수 있습니다.
    """
    for key in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value.rstrip("/")
    return None


def _purge_locked(now: float) -> None:
    """만료 토큰 정리. 반드시 _lock 을 쥔 상태에서 호출하세요."""
    dead = [t for t, info in _tokens.items() if info["expires"] <= now]
    for t in dead:
        _tokens.pop(t, None)
    if len(_tokens) > MAX_TOKENS:
        # dict 는 삽입 순서를 유지하므로 앞쪽이 가장 오래된 항목입니다
        for t in list(_tokens)[:len(_tokens) - MAX_TOKENS // 2]:
            _tokens.pop(t, None)


def issue(url: str, user_id: int, guild_id: int) -> str:
    """링크 하나에 대한 토큰을 발급하고 토큰 문자열을 돌려줍니다."""
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _purge_locked(now)
        _tokens[token] = {
            "url": url,
            "user_id": int(user_id),
            "guild_id": int(guild_id),
            "expires": now + TOKEN_TTL_SECONDS,
            "uses": 0,
        }
    return token


def redeem(token: str) -> tuple[str | None, str]:
    """토큰을 실제 링크로 교환합니다.

    반환값은 (url, reason) 입니다. 실패 시 url 은 None 이며
    reason 은 "expired" / "exhausted" / "unknown" 중 하나입니다.
    """
    now = time.monotonic()
    with _lock:
        # 정리를 먼저 하면 방금 만료된 토큰이 사라져 "만료됨" 대신
        # "유효하지 않음"으로 안내됩니다. 조회를 먼저 해 사유를 정확히 알려줍니다.
        info = _tokens.get(token)
        if info is None:
            _purge_locked(now)
            return None, "unknown"
        if info["expires"] <= now:
            _tokens.pop(token, None)
            _purge_locked(now)
            return None, "expired"
        if info["uses"] >= MAX_USES:
            return None, "exhausted"
        info["uses"] += 1
        return info["url"], "ok"


def peek_owner(token: str) -> tuple[int, int] | None:
    """토큰을 **소비하지 않고** 발급 대상만 확인합니다.

    신원 게이트가 "이 링크가 누구 것인지"를 먼저 알아야 하는데,
    redeem() 을 쓰면 확인만 하고도 링크가 소모돼 버립니다.
    반환값은 (guild_id, user_id) 이며, 없거나 만료면 None 입니다.

    **이미 다 쓴 토큰도 없는 것으로 봅니다.** 여기서 소유자를 돌려주면
    호출부가 신원 게이트로 보내는데, 유저는 디스코드 로그인을 전부 마치고
    돌아온 뒤에야 "이미 사용된 링크"를 보게 됩니다. 게다가 그 과정에서
    죽은 토큰의 수명을 연장해 메모리에 더 오래 남깁니다.
    None 을 주면 호출부가 곧바로 소비 경로로 넘겨 정확한 사유를 띄웁니다.
    """
    now = time.monotonic()
    with _lock:
        info = _tokens.get(token)
        if info is None or info["expires"] <= now:
            return None
        if info["uses"] >= MAX_USES:
            return None
        return info["guild_id"], info["user_id"]


def extend(token: str, seconds: int) -> bool:
    """살아있는 토큰의 수명을 연장합니다.

    OAuth 신원 확인으로 보내는 동안 토큰이 죽어버리면, 확인에 성공하고도
    "링크가 만료되었습니다"를 보게 됩니다. 디스코드 로그인·동의를 거치는
    첫 이용자에게 특히 자주 일어납니다. 그래서 게이트로 내보내기 직전에
    왕복 예산만큼 늘려 줍니다.

    1회용(MAX_USES)과 소유자 검증은 그대로라 유출 내성은 유지됩니다.
    """
    now = time.monotonic()
    with _lock:
        info = _tokens.get(token)
        if info is None or info["expires"] <= now:
            return False
        info["expires"] = now + seconds
        return True


def stats() -> dict:
    """현재 보관 중인 토큰 수 (진단용)."""
    now = time.monotonic()
    with _lock:
        alive = sum(1 for i in _tokens.values() if i["expires"] > now)
        return {"total": len(_tokens), "alive": alive}


def clear() -> None:
    """전체 비우기 (테스트용)."""
    with _lock:
        _tokens.clear()
