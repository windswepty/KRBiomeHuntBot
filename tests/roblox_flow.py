"""로블록스 연동 경로 — 실제로 가동 중인데 검증이 비어 있던 곳.

커버리지로 재 보니 save_link() 0%, _revoke_verify_if_required() 6% 였습니다.
둘 다 '조용히 잘못되면 아무도 모르는' 함수입니다.

  save_link                    저장이 실패해도 유저에게는 성공처럼 보일 수 있음
  _revoke_verify_if_required   연동만 지우고 인증 역할이 남으면
                               "링크 받아간 사람은 전원 연동됨" 전제가 깨짐
"""
import sys, os, tempfile, asyncio, time, json, urllib.error, http.client

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ["ROBLOX_CLIENT_ID"] = "1234567890"
os.environ["ROBLOX_CLIENT_SECRET"] = "s" * 40
os.environ["PUBLIC_BASE_URL"] = "https://example.test"

import discord
import fakes as F
from fakes import note, drain
from utils import roblox_oauth as ro
import cogs.roblox_link as RL
from utils.verify_db import save_verify_settings, save_roblox_link, get_roblox_link


# ── state 발급·소비 ────────────────────────────────────────────────
def test_pending():
    print("=== 로그인 왕복 state (1회용이어야 합니다) ===")
    ro.clear()
    url, sid = ro.start(1, 7)
    note("code_challenge=" in url and "S256" in url, "PKCE 포함")
    note("client_secret" not in url, "URL 에 secret 이 실리지 않음")

    info = ro.take_pending(sid)
    note(info is not None and info["user_id"] == 7, "정상 소비")

    note(ro.take_pending(sid) is None,
         "같은 state 두 번째는 거부 (재사용 차단)")
    note(ro.take_pending("없는state") is None, "모르는 state 는 거부")

    # 만료: 브라우저를 열어두고 한참 뒤 돌아오는 경우
    _, sid2 = ro.start(1, 7)
    ro._pending[sid2]["expires"] = time.monotonic() - 1
    note(ro.take_pending(sid2) is None, "만료된 state 는 거부")

    # 만료분이 메모리에 계속 쌓이면 안 됩니다
    ro.clear()
    for _ in range(5):
        _, s = ro.start(1, 7)
        ro._pending[s]["expires"] = time.monotonic() - 1
    ro.start(1, 8)          # 새 발급이 청소를 유발합니다
    note(len(ro._pending) == 1, f"만료분 자동 정리 (남은 {len(ro._pending)}개)")


# ── 토큰 교환 ──────────────────────────────────────────────────────
def test_exchange():
    print("")
    print("=== 토큰 교환 — 실패해도 예외가 새면 안 됩니다 ===")
    real = ro._request

    def fake(seq):
        it = iter(seq)

        def _f(url, data, headers, method):
            v = next(it)
            if isinstance(v, Exception):
                raise v
            return v
        return _f

    ro._request = fake([
        {"access_token": "AT"},
        {"sub": "820422116", "preferred_username": "ArcanicLuna",
         "nickname": "루나", "created_at": "2019-04-11T00:00:00Z"},
    ])
    got = ro.exchange("code", "verifier")
    note(got is not None and got["roblox_id"] == 820422116, "정상 교환", str(got))
    note(isinstance(got["roblox_id"], int), "roblox_id 는 int (문자열이면 대조가 깨집니다)")
    note(got["roblox_name"] == "ArcanicLuna", "사용자명")

    cases = [
        ("access_token 없음", [{"error": "invalid_grant"}]),
        ("토큰 응답이 비어 있음", [None]),
        ("userinfo 에 sub 없음", [{"access_token": "AT"}, {"name": "x"}]),
        ("userinfo 가 비어 있음", [{"access_token": "AT"}, None]),
        ("HTTP 401", [urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, None)]),
        ("응답 대기 중 타임아웃", [TimeoutError("timed out")]),
        ("연결 끊김", [http.client.RemoteDisconnected("closed")]),
        ("JSON 깨짐", [ValueError("Expecting value")]),
    ]
    for label, seq in cases:
        ro._request = fake(seq)
        try:
            r = ro.exchange("code", "verifier")
            note(r is None, f"{label} → None 반환 (예외 안 남)")
        except Exception as e:
            note(False, f"{label} → 예외가 샜습니다", f"{type(e).__name__}: {e}")

    ro._request = real


# ── 저장 다리 ──────────────────────────────────────────────────────
def test_save_link():
    print("")
    print("=== 저장 다리 (Flask 스레드 → 봇 루프) ===")
    ro.set_saver(None)
    ro._saver = None
    ok, why = ro.save_link(1, 7, {"roblox_id": 1})
    note((ok, why) == (False, "not_ready"),
         "저장 함수가 아직 안 꽂혔으면 not_ready", str((ok, why)))

    def boom(*a):
        raise RuntimeError("DB 폭발")
    ro.set_saver(boom)
    ok, why = ro.save_link(1, 7, {"roblox_id": 1})
    note((ok, why) == (False, "error"),
         "저장 중 예외가 나도 Flask 500 이 아니라 (False, error)", str((ok, why)))

    ro.set_saver(lambda g, u, i: (True, "ok"))
    note(ro.save_link(1, 7, {"roblox_id": 1}) == (True, "ok"), "정상 저장 통과")


# ── 같은 로블록스 계정 중복 등록 ───────────────────────────────────
async def test_duplicate_account():
    print("")
    print("=== 로블록스 계정 하나는 디스코드 하나에만 ===")
    # 겹치면 게임 내 명단 대조가 통째로 무의미해집니다.
    saver = RL._make_saver(None, asyncio.get_running_loop())

    async def call(uid, rid, name):
        return await saver.__wrapped__(1, uid, {"roblox_id": rid, "roblox_name": name}) \
            if hasattr(saver, "__wrapped__") else None

    # bridge 는 스레드용이라 내부 _save 를 직접 부릅니다
    await save_roblox_link(1, 100, 555, "첫사람", method="oauth", created_at="")
    from utils.verify_db import find_roblox_owner
    owner = await find_roblox_owner(1, 555)
    note(owner is not None and str(owner.get("user_id")) == "100",
         "소유자 조회", str(owner and owner.get("user_id")))

    owner2 = await find_roblox_owner(1, 999)
    note(owner2 is None, "등록 안 된 계정은 소유자 없음")


# ── 연동 해제 시 인증 역할 회수 ────────────────────────────────────
async def test_revoke():
    print("")
    print("=== 연동을 지우면 인증 역할도 회수돼야 합니다 ===")
    # 안 그러면 '링크를 받아간 사람은 전원 연동돼 있다'는 전제가 깨집니다.
    cog = RL.RobloxLink(None)
    guild = F.GUILD
    role = guild.get_role(902)          # 필수역할
    member = F.Member(50, "대상", False, roles=[role], guild=guild)

    await save_verify_settings(guild.id, {"require_roblox": False, "role_id": role.id})
    msg = await cog._revoke_verify_if_required(guild, member)
    note(msg == "" and role in member.roles,
         "연동 필수가 꺼져 있으면 역할을 건드리지 않음")

    await save_verify_settings(guild.id, {"require_roblox": True, "role_id": role.id})
    msg = await cog._revoke_verify_if_required(guild, member)
    note(role not in member.roles, "연동 필수면 역할 회수")
    note("회수" in msg, "무엇을 했는지 안내 문구를 돌려줌", msg[:40])

    msg = await cog._revoke_verify_if_required(guild, member)
    note(msg == "", "이미 없는 역할은 조용히 넘어감 (불필요한 API 호출 없음)")

    await save_verify_settings(guild.id, {"require_roblox": True, "role_id": None})
    m2 = F.Member(51, "역할설정없음", False, roles=[role], guild=guild)
    note(await cog._revoke_verify_if_required(guild, m2) == "",
         "인증 역할이 미설정이면 아무것도 안 함")

    # 봇 권한이 부족해도 명령 전체가 죽으면 안 됩니다
    await save_verify_settings(guild.id, {"require_roblox": True, "role_id": role.id})
    m3 = F.Member(52, "권한부족", False, roles=[role], guild=guild)

    async def deny(*a, **k):
        raise discord.Forbidden(
            type("R", (), {"status": 403, "reason": "Forbidden"})(), "no perm")
    m3.remove_roles = deny
    msg = await cog._revoke_verify_if_required(guild, m3)
    note("권한" in msg, "권한 부족은 안내로 알림 (예외로 죽지 않음)", msg[:40])

    async def blow(*a, **k):
        raise RuntimeError("알 수 없는 오류")
    m4 = F.Member(53, "예외", False, roles=[role], guild=guild)
    m4.remove_roles = blow
    msg = await cog._revoke_verify_if_required(guild, m4)
    note("오류" in msg, "예상 못한 예외도 삼키고 안내", msg[:40])


# ── 연동 필수인데 OAuth 가 꺼져 있으면 ─────────────────────────────
def test_disabled():
    print("")
    print("=== 환경 변수가 없으면 꺼진 채로 동작해야 합니다 ===")
    cid = os.environ.pop("ROBLOX_CLIENT_ID")
    note(ro.enabled() is False, "CLIENT_ID 없으면 비활성")
    os.environ["ROBLOX_CLIENT_ID"] = cid
    sec = os.environ.pop("ROBLOX_CLIENT_SECRET")
    note(ro.enabled() is False, "SECRET 없으면 비활성")
    os.environ["ROBLOX_CLIENT_SECRET"] = sec
    note(ro.enabled() is True, "둘 다 있으면 활성")
    note(ro.redirect_uri() == "https://example.test/roblox/callback",
         "redirect_uri 조립", ro.redirect_uri())


async def test_link_check():
    """`/연동점검` — "Redirect URI is invalid" 제보를 받았을 때 첫 화면.

    **전체 장애인지 그 사람만인지**를 가려 주지 못하면, 운영진이 멀쩡한
    앱 설정을 건드리다 진짜로 망가뜨립니다. 그래서 판정 문구를 검사합니다.
    """
    print("")
    print("=== 연동 점검 (제보를 받았을 때 첫 화면) ===")
    from datetime import datetime, timedelta
    from utils.verify_db import KST

    cog = RL.RobloxLink(type("B", (), {"user": None})())
    original = RL.list_roblox_links

    def at(**kw):
        return (datetime.now(KST) - timedelta(**kw)).strftime(
            "%Y-%m-%d %H:%M:%S")

    async def run(stamps):
        async def _stub(gid, limit=1000):
            return [{"user_id": str(i), "roblox_id": i, "roblox_name": "n",
                     "linked_at": s} for i, s in enumerate(stamps, start=1)]
        RL.list_roblox_links = _stub
        it = F.Inter()
        it.guild = F.GUILD
        await cog.link_check.callback(cog, it)
        await drain()
        sent = it.followup.sent[-1] if it.followup.sent else {}
        return sent, sent.get("embed")

    sent, e = await run([at(days=3), at(minutes=10)])
    body = str(e.to_dict())
    note(e.color == discord.Color.green(), "최근 성공이 있으면 초록",
         str(e.color))
    note("그 사람 쪽 문제" in (e.description or ""),
         "전체 장애가 아님을 분명히 말함", (e.description or "")[:40])
    note(sent.get("ephemeral") is True, "ephemeral=True")
    note("https://example.test/roblox/callback" in body,
         "Creator Hub 와 대조할 주소를 그대로 보여줌")
    note("Redirect URLs" in body, "어디에 등록해야 하는지도 알려줌")
    note("`1234567890`" in body, "Client ID 를 보여줌 (앱 구분용)")
    note("새로 실행" in body, "안 되는 사람에게 줄 안내가 들어 있음")

    sent, e = await run([at(hours=5)])
    note(e.color == discord.Color.green(), "24시간 안이면 초록")
    note("24시간" in (e.description or ""), "언제까지 됐는지 말해 줌")

    sent, e = await run([at(days=3)])
    note(e.color == discord.Color.orange(), "하루 넘게 없으면 주황",
         str(e.color))
    note("Creator Hub" in (e.description or ""),
         "그때는 앱 설정을 보라고 안내")

    sent, e = await run([])
    note(e.color == discord.Color.orange(), "기록이 없으면 주황")
    note("아직 없습니다" in (e.description or ""), "기록 없음을 구분해 말함")

    print("")
    print("--- 설정 자체가 깨진 경우는 빨간불이어야 ---")
    cid = os.environ.pop("ROBLOX_CLIENT_ID")
    sent, e = await run([at(minutes=1)])
    note(e.color == discord.Color.red(), "OAuth 꺼짐 → 빨강")
    note(".env" in (e.description or ""), "어디를 봐야 하는지 말해 줌")
    os.environ["ROBLOX_CLIENT_ID"] = cid

    base = os.environ.pop("PUBLIC_BASE_URL")
    sent, e = await run([at(minutes=1)])
    note(e.color == discord.Color.red(), "공개 주소 없음 → 빨강")
    note("PUBLIC_BASE_URL" in (e.description or ""), "빠진 값을 이름으로 지목")
    os.environ["PUBLIC_BASE_URL"] = base

    RL.list_roblox_links = original


async def main():
    test_pending()
    test_exchange()
    test_save_link()
    await test_duplicate_account()
    await test_revoke()
    await test_link_check()
    test_disabled()

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
