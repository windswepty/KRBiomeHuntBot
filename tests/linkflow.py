"""링크 보호 전 흐름 · 1회용 토큰 · OAuth 라우트 · 워터마크 · 유출 감지."""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://example.invalid"

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Msg, Role, Inter, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]

ROBLOX = ("https://www.roblox.com/games/1234567/Test"
          "?privateServerLinkCode=abcdef123456")


async def main():
    import utils.verify_db as vdb
    import utils.link_tokens as LT

    vcols = FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    lg = bot.get_cog("LinkGuard")

    ch = GUILD.channels[0]
    verified = Role(901, "인증")

    print("=== 1. 웹훅 임베드 보호 ===")
    ls = await vdb.get_linkguard_settings(GUILD.id)
    ls.update({"enabled": True, "channel_ids": [111], "roblox_only": True,
               "relay_links": True, "log_channel_id": 333})
    await vdb.save_linkguard_settings(GUILD.id, ls)
    vs = await vdb.get_verify_settings(GUILD.id)
    vs.update({"enabled": True, "role_id": 901})
    await vdb.save_verify_settings(GUILD.id, vs)

    m = Msg(4000, ch, Member(3, "웹훅"))
    m.webhook_id, m.guild = 12345, GUILD
    m.embeds = [discord.Embed(title="개인서버 매크로", description=ROBLOX)]
    await lg.on_message(m)
    await drain()
    note(m.deleted, "원본 삭제됨")
    note(len(ch.sent) > 0, "링크 제거한 임베드 재게시", f"{len(ch.sent)}건")
    posted = ch.sent[-1] if ch.sent else {}
    body = ""
    if posted.get("embed") is not None:
        e = posted["embed"]
        body = (e.title or "") + (e.description or "")
    note("roblox.com" not in body, "재게시본에 원본 링크가 없음")
    note(posted.get("view") is not None, "입장 버튼이 붙음")

    plinks = vcols["plink_col"].docs
    note(len(plinks) == 1, "protected_links 저장", f"{len(plinks)}건")
    saved = list(plinks.values())[0]
    note(any("roblox.com" in u for u in (saved.get("links") or [])),
         "원본 링크는 DB 에만 보관", str(len(saved.get("links") or [])))
    note(len(vcols["guardkey_col"].docs) >= 1, "유출 감지용 색인 등록",
         f"{len(vcols['guardkey_col'].docs)}건")

    print("\n=== 2. 로블록스가 아닌 링크는 건드리지 않음 ===")
    before = len(vcols["plink_col"].docs)
    m2 = Msg(4001, ch, Member(3, "웹훅"))
    m2.webhook_id, m2.guild = 12345, GUILD
    m2.embeds = [discord.Embed(title="제작자", description="https://maker.example.com")]
    await lg.on_message(m2)
    await drain()
    note(not m2.deleted and len(vcols["plink_col"].docs) == before,
         "원문 그대로 (매크로 홍보 링크 오인 방지)")

    print("\n=== 3. 감시 대상이 아닌 채널 ===")
    other = GUILD.channels[1]
    m3 = Msg(4002, other, Member(3, "웹훅"))
    m3.webhook_id, m3.guild = 12345, GUILD
    m3.embeds = [discord.Embed(title="x", description=ROBLOX)]
    await lg.on_message(m3)
    await drain()
    note(not m3.deleted, "다른 채널은 보호하지 않음")

    print("\n=== 4. 사람이 쓴 메시지 ===")
    m4 = Msg(4003, ch, Member(50, "사람", False, guild=GUILD))
    m4.webhook_id, m4.guild = None, GUILD
    m4.embeds = [discord.Embed(title="x", description=ROBLOX)]
    await lg.on_message(m4)
    await drain()
    note(not m4.deleted, "웹훅이 아니면 대상이 아님")

    print("\n=== 5. 버튼 클릭 (권한 검사 순서) ===")
    key = list(plinks)[0]
    import cogs.link_guard as LG

    async def click(user):
        it = Inter(uid=user.id, admin=False, roles=list(user.roles))
        it.message = Msg(int(key) if str(key).isdigit() else 4000, ch, user)
        v = LG.ProtectedLinkView()
        btn = next(c for c in v.children if isinstance(c, discord.ui.Button))
        await btn.callback(it)
        await drain()
        return it

    it = await click(Member(60, "미인증", False, guild=GUILD))
    txt = str(it.response.sent) + str(it.followup.sent)
    note("roblox.com" not in txt, "미인증자에게 링크가 나가지 않음")

    it = await click(Member(61, "인증됨", False, roles=[verified], guild=GUILD))
    txt = str(it.response.sent) + str(it.followup.sent)
    note(it.response.done, "인증자는 응답을 받음", F.outcome(it))

    print("\n  전원 스위치를 끄면 역할과 무관하게 막혀야 합니다")
    ls["enabled"] = False
    await vdb.save_linkguard_settings(GUILD.id, ls)
    it = await click(Member(62, "인증됨", False, roles=[verified], guild=GUILD))
    txt = str(it.response.sent) + str(it.followup.sent)
    note("roblox.com" not in txt, "정지 중에는 링크가 안 나감")
    ls["enabled"] = True
    await vdb.save_linkguard_settings(GUILD.id, ls)

    print("\n=== 6. 1회용 토큰 ===")
    LT.clear()
    t = LT.issue(ROBLOX, 61, GUILD.id)
    note(bool(t), "발급", str(t)[:12] + "...")
    note(LT.peek_owner(t) == (GUILD.id, 61), "소유자 확인 (guild, user)",
         str(LT.peek_owner(t)))
    url, why = LT.redeem(t)
    note(url == ROBLOX, "한 번은 사용됨", why)
    url2, why2 = LT.redeem(t)
    note(url2 is None, "두 번째는 거부", why2)

    t2 = LT.issue(ROBLOX, 61, GUILD.id)
    note(LT.extend(t2, 600), "수명 연장 (OAuth 왕복용)")
    note(LT.redeem(t2)[0] == ROBLOX, "연장 후에도 사용 가능")

    print("\n=== 7. /j/<token> — HEAD 가 토큰을 먼저 소비하면 안 됨 ===")
    import keep_alive
    c = keep_alive.app.test_client()
    t3 = LT.issue(ROBLOX, 61, GUILD.id)
    r = c.head(f"/j/{t3}")
    note(r.status_code == 204, "HEAD 는 204", str(r.status_code))
    note(LT.peek_owner(t3) is not None, "HEAD 뒤에도 토큰이 살아 있음")
    r = c.get(f"/j/{t3}", follow_redirects=False)
    note(r.status_code in (302, 303, 307, 200), "GET 은 동작",
         str(r.status_code))
    note(LT.peek_owner(t3) is None,
         "GET 이 토큰을 소비함 (소모된 토큰은 peek 도 None)")
    note(LT.redeem(t3)[1] == "exhausted", "재사용은 exhausted 로 거부",
         LT.redeem(t3)[1])

    r = c.get("/j/존재하지않는토큰", follow_redirects=False)
    note(r.status_code == 410, "없는 토큰은 410 Gone (500 아님)", str(r.status_code))

    print("\n=== 8. 만료 판정이 정리보다 먼저 ===")
    LT.clear()
    t4 = LT.issue(ROBLOX, 61, GUILD.id)
    with LT._lock if hasattr(LT, "_lock") else _null():
        pass
    for store in ("_tokens", "_store", "_links"):
        d = getattr(LT, store, None)
        if isinstance(d, dict) and t4 in d:
            rec = d[t4]
            if isinstance(rec, dict):
                for k in ("exp", "expires", "expires_at", "until"):
                    if k in rec:
                        rec[k] = 0
            break
    url, why = LT.redeem(t4)
    note(url is None and why == "expired",
         "만료로 안내 (unknown 이 아님)", why)

    print("\n=== 9. OAuth 라우트가 500 을 내지 않음 ===")
    for path in ("/auth/callback", "/auth/callback?code=bad&state=bad",
                 "/roblox/callback", "/roblox/callback?code=bad&state=bad"):
        r = c.get(path)
        note(r.status_code < 500, f"GET {path[:38]}", str(r.status_code))
        r = c.head(path)
        note(r.status_code < 500, f"HEAD {path[:38]}", str(r.status_code))

    print("\n=== 10. 워터마크 · 유출 추적 ===")
    await vdb.save_watermark(GUILD.id, "WM1234", 61, "인증됨")
    found = await vdb.get_watermark(GUILD.id, "WM1234")
    note(found is not None and str(found.get("user_id")) == "61",
         "워터마크로 유출자 추적", str(found.get("user_name") if found else None))
    note(await vdb.get_watermark(GUILD.id, "없는코드") is None,
         "없는 코드는 None")

    await vdb.register_guarded_keys(GUILD.id, ["abcdef123456", "zzz999"])
    keys = await vdb.get_guarded_keys(GUILD.id)
    note("abcdef123456" in keys, "유출 감지 색인 조회", f"{len(keys)}개")

    print("\n  링크 사용 기록 (80명 동시)")
    res = await asyncio.gather(*[
        vdb.record_link_usage(GUILD.id, 4000, 1000 + i, f"유저{i}")
        for i in range(80)])
    rows = await vdb.get_link_usage(GUILD.id, 4000)
    note(len(rows) == 80, "80명이 각각 기록됨 (뭉개지지 않음)", f"{len(rows)}명")

    print("\n  같은 사람이 여러 번 눌러도 1명")
    await asyncio.gather(*[
        vdb.record_link_usage(GUILD.id, 4001, 555, "연타맨") for _ in range(30)])
    rows = await vdb.get_link_usage(GUILD.id, 4001)
    note(len(rows) == 1, "중복 제거", f"{len(rows)}명")

    print("\n  클릭 로그 상한")
    for i in range(5):
        await vdb.add_click_log(GUILD.id, 1000 + i, f"유저{i}", ROBLOX, "view")
    logs = await vdb.get_click_logs(GUILD.id, limit=50)
    note(len(logs) == 5, "클릭 로그 기록", f"{len(logs)}건")

    print("\n=== 11. 정적 페이지 ===")
    for path in ("/privacy", "/terms"):
        r = c.get(path)
        note(r.status_code == 200, f"GET {path}", str(r.status_code))

    WATERMARKED = ROBLOX + "#hbh-c26ae66d"

    print("\n=== 12. 모바일 브릿지 페이지 · 워터마크 프래그먼트 제거 ===")
    t_m1 = LT.issue(WATERMARKED, 61, GUILD.id)
    r = c.get(f"/j/{t_m1}", follow_redirects=False,
              headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
    note(r.status_code == 200, "모바일 UA → 200 브릿지 페이지", str(r.status_code))
    body = r.data.decode()
    note("#hbh-" not in body, "브릿지 페이지에서 워터마크 프래그먼트 제거됨")
    note("privateServerLinkCode=abcdef123456" in body,
         "로블록스 링크 코드는 유지됨")
    note("로블록스에서 열기" in body, "터치 버튼 존재")
    note('meta http-equiv="refresh"' in body, "meta refresh 자동 이동 존재")

    t_m2 = LT.issue(WATERMARKED, 61, GUILD.id)
    r = c.get(f"/j/{t_m2}", follow_redirects=False,
              headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8)"})
    note(r.status_code == 200, "Android UA → 200 브릿지 페이지", str(r.status_code))
    note("#hbh-" not in r.data.decode(), "Android 에서도 프래그먼트 제거됨")

    print("\n  PC 에서는 302 리다이렉트, 프래그먼트 제거")
    t_p = LT.issue(WATERMARKED, 61, GUILD.id)
    r = c.get(f"/j/{t_p}", follow_redirects=False,
              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    note(r.status_code == 302, "PC UA → 302 리다이렉트", str(r.status_code))
    loc = r.headers.get("Location", "")
    note("#hbh-" not in loc, "리다이렉트 Location 에서 프래그먼트 제거됨")
    note("privateServerLinkCode=abcdef123456" in loc,
         "리다이렉트 URL 에 링크 코드 유지됨")

    print("\n  프래그먼트 없는 URL 도 정상 동작")
    t_clean = LT.issue(ROBLOX, 61, GUILD.id)
    r = c.get(f"/j/{t_clean}", follow_redirects=False,
              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    note(r.status_code == 302, "프래그먼트 없는 URL → 302", str(r.status_code))
    note(ROBLOX in r.headers.get("Location", ""),
         "원본 URL 그대로 리다이렉트")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


asyncio.run(main())

