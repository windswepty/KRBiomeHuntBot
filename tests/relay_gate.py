"""중계 게이트가 **켜져 있을 때** 실제로 막는가 (`/j/<token>`).

`tests/linkflow.py` 는 게이트가 **꺼진** 경로만 봅니다(HEAD·소비·410).
정작 게이트의 존재 이유인 **"발급받은 본인인가"** 는 검증이 없었습니다.

막아야 하는 것은 이겁니다.

    A 가 버튼을 눌러 받은 /j/<토큰> 주소를 B 에게 넘긴다
      → B 가 열면 들어가진다?  ← 이러면 게이트가 없는 것과 같습니다

그리고 **막는 것만큼 중요한 게 헛돌지 않는 것**입니다. 쿠키를 저장 못 하는
브라우저가 디스코드와 이 페이지 사이를 무한히 오가면, 그 사람은 영영
못 들어갑니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

# 게이트를 **켠 상태**로 띄웁니다. 이게 이 스위트의 전부입니다.
os.environ["DISCORD_CLIENT_ID"] = "test-client-id"
os.environ["DISCORD_CLIENT_SECRET"] = "test-client-secret"
os.environ["SECRET_KEY"] = "test-signing-key"
os.environ["PUBLIC_BASE_URL"] = "https://example.org"

import fakes as F
from fakes import note

import utils.link_tokens as LT
import utils.oauth_gate as G
import keep_alive

ROBLOX = "https://www.roblox.com/games/1/T?privateServerLinkCode=abc123"
OWNER, STRANGER = 6001, 6002
GUILD_ID = 1


def client():
    return keep_alive.app.test_client()


def cookie_for(uid):
    return G.sign_user(uid)


async def main():
    note(G.enabled(), "게이트가 켜진 상태로 시작", str(G.enabled()))
    G.set_member_gate(None)          # 멤버 판정은 따로 검증합니다

    print("=== 1. ⚠️ 남의 링크는 열 수 없어야 합니다 ===")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    c = client()
    c.set_cookie(G.COOKIE_NAME, cookie_for(STRANGER), domain="localhost")
    r = c.get(f"/j/{t}", follow_redirects=False)
    note(r.status_code == 403, "남이 열면 403", str(r.status_code))
    body = r.get_data(as_text=True)
    note("로블록스" not in body and "privateServerLinkCode" not in body,
         "**주소가 화면에 새지 않음**")
    note(LT.peek_owner(t) is not None,
         "막혔을 뿐 토큰은 살아 있음 (주인은 아직 쓸 수 있어야 합니다)")

    print("")
    print("  주인이 열면 통과해야 합니다")
    c2 = client()
    c2.set_cookie(G.COOKIE_NAME, cookie_for(OWNER), domain="localhost")
    r = c2.get(f"/j/{t}", follow_redirects=False)
    note(r.status_code in (200, 302), "주인은 통과", str(r.status_code))
    if r.status_code == 302:
        note("roblox.com" in (r.headers.get("Location") or ""),
             "로블록스로 보냄", (r.headers.get("Location") or "")[:50])
    note(LT.peek_owner(t) is None, "쓰고 나면 토큰이 소모됨")

    print("")
    print("  한 번 쓴 토큰은 주인이라도 다시 못 씁니다")
    r = c2.get(f"/j/{t}", follow_redirects=False)
    note(r.status_code == 410, "두 번째는 410", str(r.status_code))

    print("")
    print("=== 2. 쿠키가 없으면 디스코드로 한 번 보냅니다 ===")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    c3 = client()
    r = c3.get(f"/j/{t}", follow_redirects=False)
    loc = r.headers.get("Location") or ""
    note(r.status_code == 302, "302 로 보냄", str(r.status_code))
    note("discord.com" in loc, "디스코드 동의 화면으로", loc[:56])
    note("scope=identify" in loc, "권한은 identify 만")
    note(LT.peek_owner(t) is not None,
         "**보내는 동안 토큰을 소비하지 않음** (돌아와서 써야 합니다)")

    print("")
    print("  왕복 시간만큼 수명을 늘려 둬야 합니다")
    # 안 늘리면 신원 확인에 성공하고도 돌아온 순간 '만료' 를 봅니다.
    info = LT._tokens[t]
    import time
    left = info["expires"] - time.monotonic()
    note(left > LT.TOKEN_TTL_SECONDS,
         f"수명이 기본({LT.TOKEN_TTL_SECONDS}초)보다 길어짐", f"{left:.0f}초")

    print("")
    print("=== 3. ⚠️ 쿠키를 못 쓰는 브라우저가 무한히 돌면 안 됩니다 ===")
    # 확인을 마치고 돌아왔는데(?v=1) 또 쿠키가 없으면 거기서 멈춰야 합니다.
    r = c3.get(f"/j/{t}?v=1", follow_redirects=False)
    note(r.status_code == 403, "?v=1 + 쿠키 없음 → 403 으로 멈춤", str(r.status_code))
    note("discord.com" not in (r.headers.get("Location") or ""),
         "**디스코드로 다시 보내지 않음** (무한 왕복 차단)")

    print("")
    print("=== 4. 위조 쿠키로는 통과할 수 없어야 ===")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    for label, value in (("아무 문자열", "not-a-cookie"),
                         ("주인 ID 를 평문으로", str(OWNER)),
                         ("남의 키로 서명", __import__("itsdangerous")
                          .URLSafeTimedSerializer("가짜키", salt="hbh-gate")
                          .dumps(str(OWNER)))):
        cx = client()
        cx.set_cookie(G.COOKIE_NAME, value, domain="localhost")
        r = cx.get(f"/j/{t}", follow_redirects=False)
        # 위조 쿠키는 '쿠키 없음' 과 같게 취급되어 디스코드로 갑니다
        loc = r.headers.get("Location") or ""
        ok = r.status_code == 302 and "discord.com" in loc
        note(ok, f"{label} → 통과 못 하고 신원 확인으로", f"{r.status_code} {loc[:36]}")
    note(LT.peek_owner(t) is not None, "위조 시도로 토큰이 타지 않음")

    print("")
    print("=== 5. HEAD 는 토큰을 건드리지 않아야 (미리보기·백신) ===")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    ch = client()
    ch.set_cookie(G.COOKIE_NAME, cookie_for(OWNER), domain="localhost")
    r = ch.head(f"/j/{t}")
    note(r.status_code == 204, "HEAD 는 204", str(r.status_code))
    note(LT.peek_owner(t) is not None, "토큰이 살아 있음")

    print("")
    print("=== 6. 멤버·역할 판정이 붙으면 그것도 막아야 ===")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    G.set_member_gate(lambda g, u: (False, "인증 역할 없음"))
    try:
        cm = client()
        cm.set_cookie(G.COOKIE_NAME, cookie_for(OWNER), domain="localhost")
        r = cm.get(f"/j/{t}", follow_redirects=False)
        note(r.status_code == 403, "역할이 없으면 주인이라도 403", str(r.status_code))
        note(LT.peek_owner(t) is not None, "이때도 토큰은 살아 있음")
    finally:
        G.set_member_gate(None)

    print("")
    print("=== 7. /auth/callback ===")
    c4 = client()
    bad = [
        ("파라미터 없음", "/auth/callback"),
        ("state 위조", "/auth/callback?code=x&state=forged"),
        ("디스코드가 거부", "/auth/callback?error=access_denied"),
        ("code 없음", f"/auth/callback?state={G.sign_state('tok')}"),
    ]
    for label, path in bad:
        r = c4.get(path, follow_redirects=False)
        note(r.status_code < 500, f"{label} → 500 이 아님", str(r.status_code))
        note("Set-Cookie" not in r.headers,
             f"{label} → **쿠키를 심지 않음**")

    print("")
    print("  정상 콜백은 쿠키를 심고 원래 링크로 돌려보내야 합니다")
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    real = G.exchange_code
    try:
        G.exchange_code = lambda code, uri: OWNER
        r = c4.get(f"/auth/callback?code=good&state={G.sign_state(t)}",
                   follow_redirects=False)
        note(r.status_code == 302, "302 로 되돌림", str(r.status_code))
        loc = r.headers.get("Location") or ""
        note(f"/j/{t}" in loc, "원래 가려던 링크로", loc[:48])
        note("v=1" in loc, "왕복 표식(?v=1)을 붙임 (무한 루프 차단용)")
        setc = r.headers.get("Set-Cookie") or ""
        note(G.COOKIE_NAME in setc, "쿠키를 심음", setc[:40])
        note("HttpOnly" in setc, "**HttpOnly** (스크립트가 못 읽음)", setc[:80])
        # ⚠️ `or` 로 무르게 두면 **헛도는 검증**이 됩니다. 실제 값을 봅니다.
        note("Secure" in setc, "**Secure** (http 로는 안 보냄)", setc[:90])
        note("SameSite=Lax" in setc or "SameSite=Strict" in setc,
             "**SameSite** (남의 사이트에서 끌려가지 않음)", setc[:90])
        note("Max-Age" in setc or "Expires" in setc, "수명이 박혀 있음")
    finally:
        G.exchange_code = real

    print("")
    print("=== 8. 게이트를 끄면 예전처럼 토큰만으로 통과 ===")
    # 환경 변수를 빼도 입장이 막히면 안 됩니다 (설정 전 배포 대비).
    LT.clear()
    t = LT.issue(ROBLOX, OWNER, GUILD_ID)
    saved = {k: os.environ.pop(k) for k in
             ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SECRET_KEY")
             if k in os.environ}
    try:
        note(not G.enabled(), "게이트가 꺼짐")
        r = client().get(f"/j/{t}", follow_redirects=False)
        note(r.status_code in (200, 302), "쿠키 없이도 통과", str(r.status_code))
        note(LT.peek_owner(t) is None, "토큰이 정상 소비됨")
    finally:
        os.environ.update(saved)

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
