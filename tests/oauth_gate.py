"""중계 링크 신원 게이트 (`utils/oauth_gate.py`).

중계 토큰만으로는 **누가 눌렀는지** 알 수 없습니다. 토큰 주소를 받은
사람이 쓰기 전에 남에게 넘기면 그대로 통합니다. 이 모듈이 그 구멍을 막는
유일한 장치인데, 검증이 28% 뿐이었습니다.

여기서 지키는 것은 세 가지입니다.

  1. **위조한 쿠키로는 남이 될 수 없다.** 여기가 뚫리면 게이트가 없는
     것보다 나쁩니다 — 있다고 믿고 안심하게 되니까요.
  2. **권한은 최소로만 요청한다** (`identify`). 서버 목록(`guilds`)까지
     받으면 유저가 동의 화면에서 겁먹고 이탈합니다.
  3. **환경 변수가 없으면 조용히 꺼진다.** 설정 전에 배포해도 입장이
     막히면 안 됩니다.
"""
import sys, os, tempfile, asyncio, time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import fakes as F
from fakes import note

import utils.oauth_gate as G


def setenv(**kw):
    """환경 변수를 통째로 갈아끼웁니다 (None 이면 지웁니다)."""
    for k in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SECRET_KEY"):
        os.environ.pop(k, None)
    for k, v in kw.items():
        if v is not None:
            os.environ[k] = v


async def main():
    print("=== 1. 환경 변수가 없으면 게이트는 꺼져 있어야 ===")
    # 설정 전에 배포해도 입장이 막히면 안 됩니다.
    setenv()
    note(G.enabled() is False, "아무것도 없으면 꺼짐", str(G.enabled()))
    note(G._signing_key() is None, "서명 키도 없음")
    note(G.sign_user(123) is None, "쿠키를 만들지 않음")
    note(G.read_user("아무거나") is None, "쿠키를 읽지도 않음")

    setenv(DISCORD_CLIENT_ID="cid")
    note(G.enabled() is False, "client_id 만으로는 안 켜짐")
    setenv(DISCORD_CLIENT_SECRET="sec")
    note(G.enabled() is False, "client_secret 만으로도 안 켜짐")

    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="sec")
    note(G.enabled() is True, "둘 다 있으면 켜짐")

    print("")
    print("=== 2. 서명 키 ===")
    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="sec")
    derived = G._signing_key()
    note(derived is not None, "client_secret 에서 파생됨")
    note(derived != "sec", "**시크릿을 그대로 쓰지 않음** (용도 분리)", str(derived)[:16])

    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="sec",
           SECRET_KEY="explicit-key")
    note(G._signing_key() == "explicit-key", "SECRET_KEY 가 있으면 그걸 씀")

    print("")
    print("=== 3. 쿠키 왕복 ===")
    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="sec")
    cookie = G.sign_user(4242)
    note(isinstance(cookie, str) and cookie, "쿠키가 만들어짐")
    note("4242" not in cookie, "**유저 ID 가 그대로 보이지 않음**", cookie[:24])
    note(G.read_user(cookie) == 4242, "다시 읽으면 같은 ID", str(G.read_user(cookie)))

    print("")
    print("  ⚠️ 위조·변조한 쿠키는 통하면 안 됩니다")
    bad = [
        ("빈 값", ""), ("None", None), ("아무 문자열", "not-a-cookie"),
        # 끝이 아니라 **가운데**를 바꿉니다 (위 state 와 같은 이유 —
        # base64 끝 글자는 여러 값이 같은 것으로 디코딩됩니다).
        ("한 글자 바꾼 것",
         cookie[:len(cookie) // 2]
         + ("A" if cookie[len(cookie) // 2] != "A" else "B")
         + cookie[len(cookie) // 2 + 1:]),
        ("앞을 자른 것", cookie[3:]), ("뒤를 자른 것", cookie[:-4]),
        ("서명만 뗀 것", cookie.split(".")[0]),
    ]
    for label, v in bad:
        note(G.read_user(v) is None, f"{label} → 거부", repr(G.read_user(v)))

    print("")
    print("  ⚠️ 다른 사람 ID 로 직접 만든 쿠키도 통하면 안 됩니다")
    # 공격자가 자기 서버에서 임의 키로 쿠키를 구워 와도 서명이 다릅니다.
    from itsdangerous import URLSafeTimedSerializer
    forged = URLSafeTimedSerializer("공격자가-고른-키", salt="hbh-gate").dumps("9999")
    note(G.read_user(forged) is None, "남의 키로 서명한 쿠키 거부",
         repr(G.read_user(forged)))

    print("")
    print("  ⚠️ salt 가 다르면 통하면 안 됩니다 (같은 키라도)")
    same_key = URLSafeTimedSerializer(G._signing_key(), salt="다른-용도").dumps("9999")
    note(G.read_user(same_key) is None, "용도(salt)가 다른 토큰 거부")

    print("")
    print("  시크릿을 바꾸면 기존 쿠키는 무효가 되어야 합니다")
    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="새-시크릿")
    note(G.read_user(cookie) is None, "시크릿 교체 후 옛 쿠키 무효",
         repr(G.read_user(cookie)))

    print("")
    print("  만료된 쿠키는 거부해야 합니다")
    setenv(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="sec")
    old = G._serializer().dumps("777")
    real_max = G.COOKIE_MAX_AGE
    try:
        G.COOKIE_MAX_AGE = -1          # 이미 지난 것으로 취급
        note(G.read_user(old) is None, "수명이 지난 쿠키 거부")
    finally:
        G.COOKIE_MAX_AGE = real_max
    note(G.read_user(old) == 777, "수명 안이면 다시 통과", str(G.read_user(old)))

    print("")
    print("=== 4. state (OAuth 왕복 표식) ===")
    st = G.sign_state("토큰abc")
    note(G.read_state(st) == "토큰abc", "왕복이 됨", repr(G.read_state(st)))
    note(G.read_state(None) is None, "빈 값 거부")
    note(G.read_state("위조") is None, "위조 거부")
    # ⚠️ **마지막 글자를 바꾸는 방식은 쓰지 마세요.** base64 의 끝 글자에는
    #    쓰이지 않는 비트가 남아 있어서, 서로 다른 글자 여러 개가 **같은
    #    값으로 디코딩**됩니다. 실측으로 500회 중 29회가 "변조했는데도
    #    통과" 했습니다 — 검증이 간헐적으로 실패하는 원인이었습니다.
    #    가운데를 바꾸면 그런 애매함이 없습니다 (500회 중 0회).
    i = len(st) // 2
    flipped = st[:i] + ("Z" if st[i] != "Z" else "Y") + st[i + 1:]
    note(flipped != st, "정말로 바꿨는지 먼저 확인")
    note(G.read_state(flipped) is None, "변조 거부", flipped[:24])
    note(G.STATE_MAX_AGE <= 900,
         f"state 수명이 짧음 ({G.STATE_MAX_AGE}초)", str(G.STATE_MAX_AGE))
    note(G.COOKIE_MAX_AGE > G.STATE_MAX_AGE, "쿠키가 state 보다 오래 삶")

    print("")
    print("=== 5. ⚠️ 동의 화면에 최소 권한만 요청해야 ===")
    url = G.authorize_url("https://example.org/auth/callback", "STATE1")
    note("scope=identify" in url, "identify 만 요청", url[:90])
    note("guilds" not in url, "**서버 목록(guilds)은 요청하지 않음**")
    note("email" not in url, "이메일도 요청하지 않음")
    note("state=STATE1" in url, "state 가 실려 있음")
    note("response_type=code" in url, "authorization code 방식")
    note("example.org" in url, "redirect_uri 가 들어감")
    note("prompt=none" not in url,
         "prompt=none 을 쓰지 않음 (첫 유저가 오류를 보게 됩니다)")
    note(url.startswith("https://discord.com/"), "디스코드로 보냄", url[:32])

    print("")
    print("=== 6. 멤버 판정기 ===")
    G.set_member_gate(None)
    ok, why = G.check_member(1, 2)
    note(ok is True, "판정기가 없으면 통과시킴 (기능 도입 전 차단 금지)")

    G.set_member_gate(lambda g, u: (u == 42, "역할 없음"))
    note(G.check_member(1, 42)[0] is True, "자격 있는 유저는 통과")
    note(G.check_member(1, 43)[0] is False, "자격 없는 유저는 차단")
    note(G.check_member(1, 43)[1] == "역할 없음", "사유를 돌려줌")

    print("")
    print("  ⚠️ 판정기가 터져도 정당한 유저를 막으면 안 됩니다")
    # 무인 운영에서 일시적 오류로 전원이 막히면 아무도 풀어줄 수 없습니다.
    def boom(g, u):
        raise RuntimeError("일부러 낸 오류")

    G.set_member_gate(boom)
    ok, _ = G.check_member(1, 2)
    note(ok is True, "오류 시 통과 처리 (fail-open)", str(ok))
    G.set_member_gate(None)

    print("")
    print("=== 7. 코드 교환 실패를 조용히 삼키지 않아야 ===")
    import urllib.request
    import urllib.error
    real_open = urllib.request.urlopen

    class FakeResp:
        def __init__(self, body):
            self._b = body.encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cases = [
        ("HTTP 401", urllib.error.HTTPError("u", 401, "no", {}, None)),
        ("네트워크 끊김", OSError("connection reset")),
        ("타임아웃", TimeoutError("timed out")),
        ("JSON 아님", ValueError("bad json")),
    ]
    for label, exc in cases:
        def raiser(*a, **k):
            raise exc
        urllib.request.urlopen = raiser
        try:
            got = G.exchange_code("code", "https://example.org/cb")
            note(got is None, f"{label} → None (500 으로 새지 않음)", repr(got))
        except Exception as e:
            note(False, f"{label} 에서 예외가 새어나옴", f"{type(e).__name__}: {e}")
        finally:
            urllib.request.urlopen = real_open

    print("")
    print("  access_token 이 없으면 None 이어야 합니다")
    urllib.request.urlopen = lambda *a, **k: FakeResp('{"error":"invalid_grant"}')
    try:
        note(G.exchange_code("c", "u") is None, "토큰 없는 응답 → None")
    finally:
        urllib.request.urlopen = real_open

    print("")
    print("  정상 응답이면 유저 ID 를 돌려줘야 합니다")
    seq = [FakeResp('{"access_token":"AT"}'), FakeResp('{"id":"5150"}')]
    urllib.request.urlopen = lambda *a, **k: seq.pop(0)
    try:
        note(G.exchange_code("c", "u") == 5150, "정상 경로에서 ID 반환")
    finally:
        urllib.request.urlopen = real_open

    print("")
    print("=== 8. 디스코드 API 요구사항 ===")
    note("KRBiomeHuntBot" in G.USER_AGENT,
         "User-Agent 를 보냄 (없으면 Cloudflare 가 403)", G.USER_AGENT[:40])
    note(G.DISCORD_AUTHORIZE.startswith("https://"), "authorize 가 https")
    note(G.DISCORD_TOKEN.startswith("https://"), "token 이 https")

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
