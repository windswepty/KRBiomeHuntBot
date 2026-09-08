"""on_message · on_member_update · 퀘스트 진행 · 출석 · 포인트 원자성."""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakes as F
from fakes import GUILD, Member, Msg, note, drain


async def main():
    import utils.database as db
    import utils.verify_db as vdb

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    bot._connection.user = Member(2, "bot")
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")

    cr = bot.get_cog("ChatReward")
    lg = bot.get_cog("LinkGuard")
    vf = bot.get_cog("Verify") or bot.get_cog("VerifyRevoke")
    qs = bot.get_cog("Quest")

    ch = GUILD.channels[0]
    user = Member(50, "말하는사람", False, guild=GUILD)

    # ── 1. 퀘스트 꺼진 상태에서 채팅 ────────────────────────────────
    print("=== on_message (퀘스트 OFF) ===")
    for i in range(12):
        m = Msg(1000 + i, ch, user)
        m.content = f"안녕하세요 반갑습니다 {i}"
        m.guild = GUILD
        try:
            await cr.on_message(m)
        except Exception as e:
            note(False, "chat_reward on_message", f"{type(e).__name__}: {e}")
            import traceback; traceback.print_exc(limit=3); break
    else:
        p = await db.get_user_points(GUILD.id, user.id)
        note(True, "채팅 12회 → 포인트", f"{p}P")
    prog = await db.get_quest_progress(GUILD.id, user.id)
    note(not prog.get("p"), "퀘스트 OFF 시 진행도 미기록",
         f"기록 {len(prog.get('p') or {})}건")

    # ── 2. 퀘스트 켜고 다시 ─────────────────────────────────────────
    print("\n=== on_message (퀘스트 ON) ===")
    st = await db.get_quest_settings(GUILD.id)
    st["enabled"] = True
    st["goal_enabled"] = True
    st["channel_ids"] = ["111", "222"]
    st["goal_channel_id"] = 333
    await db.save_quest_settings(GUILD.id, st)

    for i in range(30):
        m = Msg(2000 + i, ch, user)
        m.content = f"퀘스트 진행 테스트 문장 {i}"
        m.guild = GUILD
        await cr.on_message(m)
    await drain()
    prog = await db.get_quest_progress(GUILD.id, user.id)
    goal = await db.get_server_goal(GUILD.id)
    note(prog.get("p", {}).get("chat", 0) > 0, "일일 채팅 진행도",
         str(prog.get("p", {}).get("chat")))
    note(prog.get("p", {}).get("ch_111", 0) > 0, "채널별 진행도",
         str(prog.get("p", {}).get("ch_111")))
    note(prog.get("p", {}).get("wchat", 0) > 0, "주간 채팅 진행도",
         str(prog.get("p", {}).get("wchat")))
    note(goal["progress"] > 0, "공동 목표 진행도", str(goal["progress"]))

    # ── 3. 출석 ─────────────────────────────────────────────────────
    print("\n=== 출석 ===")
    r = await db.checkin_user(GUILD.id, 60)
    note(r.get("ok"), "첫 출석", f"{r.get('points')}P / {r.get('streak')}일 / {r.get('gained')}등")
    r2 = await db.checkin_user(GUILD.id, 60)
    note(not r2.get("ok"), "같은 날 재출석 거부", str(r2.get("reason")))

    print("\n  동시 출석 30명 (같은 유저)")
    res = await asyncio.gather(*[db.checkin_user(GUILD.id, 61) for _ in range(30)])
    note(sum(1 for x in res if x.get("ok")) == 1, "1회만 지급",
         f"{sum(1 for x in res if x.get('ok'))}회")

    print("\n  동시 선착순 등수 50회")
    ranks = await asyncio.gather(*[db.take_checkin_rank(GUILD.id)
                                   for _ in range(50)])
    note(len(set(ranks)) == 50, "등수 전부 다름",
         f"{len(ranks)}회 / {len(set(ranks))}종 / 최대 {max(ranks)}")

    # ── 4. 포인트 원자성 ───────────────────────────────────────────
    print("\n=== 포인트 원자성 ===")
    await db.add_user_points(GUILD.id, 70, 1000)
    before = await db.get_user_points(GUILD.id, 70)
    res = await asyncio.gather(*[db.deduct_user_points(GUILD.id, 70, 100)
                                 for _ in range(20)])
    after = await db.get_user_points(GUILD.id, 70)
    ok_n = sum(1 for ok, _ in res if ok)
    note(before - after == ok_n * 100 and after >= 0 and ok_n == 10,
         "동시 차감 20회 (잔액 1000)", f"{before}P -> {after}P, 성공 {ok_n}회")

    print("\n동시 지급 100회")
    await asyncio.gather(*[db.add_user_points(GUILD.id, 71, 7) for _ in range(100)])
    got = await db.get_user_points(GUILD.id, 71)
    note(got == 700, "합계 정확", f"{got}P")

    # ── 5. 재고 ─────────────────────────────────────────────────────
    print("\n=== 재고 원자성 ===")
    items = await db.get_shop_items(GUILD.id)
    items.append({"id": 1, "name": "한정판", "price": 10, "stock": 3})
    await db.save_shop_items(GUILD.id, items)
    res = await asyncio.gather(*[db.consume_shop_stock(GUILD.id, 1)
                                 for _ in range(15)])
    left = next(i["stock"] for i in await db.get_shop_items(GUILD.id) if i["id"] == 1)
    note(sum(res) == 3 and left == 0, "상점 재고 3개 -> 3명만",
         f"성공 {sum(res)}회 / 남은 {left}")

    g = await db.get_gacha_settings(GUILD.id)
    g["items"] = [{"id": 1, "name": "레어", "stock": 2, "weight": 1}]
    await db.save_gacha_settings(GUILD.id, g)
    res = await asyncio.gather(*[db.consume_gacha_stock(GUILD.id, 1)
                                 for _ in range(9)])
    left = (await db.get_gacha_settings(GUILD.id))["items"][0]["stock"]
    note(sum(res) == 2 and left == 0, "뽑기 재고 2개 -> 2명만",
         f"성공 {sum(res)}회 / 남은 {left}")

    # ── 6. 퀘스트 수령 중복 ────────────────────────────────────────
    print("\n=== 퀘스트 수령 ===")
    await db.bump_quest_progress(GUILD.id, 80, "chat", 999)
    res = await asyncio.gather(*[db.claim_quest(GUILD.id, 80, "d:chat", 500)
                                 for _ in range(15)])
    note(sum(1 for x in res if x) == 1, "동시 수령 -> 1회만",
         f"{sum(1 for x in res if x)}회 / {await db.get_user_points(GUILD.id, 80)}P")

    print("\n동시 진행도 200회")
    await asyncio.gather(*[db.bump_quest_progress(GUILD.id, 81, "chat", 1)
                           for _ in range(200)])
    pr = await db.get_quest_progress(GUILD.id, 81)
    note(pr["p"].get("chat") == 200, "진행도 정확", str(pr["p"].get("chat")))

    print("\n동시 공동목표 300회")
    await asyncio.gather(*[db.bump_server_goal(GUILD.id, 1) for _ in range(300)])
    note(True, "공동 목표 누적", str((await db.get_server_goal(GUILD.id))["progress"]))

    # ── 7. on_member_update ────────────────────────────────────────
    print("\n=== on_member_update (인증 회수) ===")
    vs = await vdb.get_verify_settings(GUILD.id)
    vs.update({"enabled": True, "role_id": 901,
               "required_role_ids": [902], "auto_revoke": True})
    await vdb.save_verify_settings(GUILD.id, vs)
    from fakes import Role
    verified = Role(901, "인증"); req = Role(902, "필수역할")
    for cog in bot.cogs.values():
        if hasattr(cog, "on_member_update"):
            b = Member(90, "u", False, roles=[verified, req], guild=GUILD)
            a = Member(90, "u", False, roles=[verified], guild=GUILD)
            try:
                await cog.on_member_update(b, a)
                await drain()
                note("인증" in a.removed, f"{type(cog).__name__}: 필수역할 해제 시 회수",
                     str(a.removed))
            except Exception as e:
                note(False, f"{type(cog).__name__}.on_member_update",
                     f"{type(e).__name__}: {e}")
                import traceback; traceback.print_exc(limit=3)

            # 시스템 정지 상태에서는 회수하지 않아야 합니다
            vs["enabled"] = False
            await vdb.save_verify_settings(GUILD.id, vs)
            b = Member(91, "u", False, roles=[verified, req], guild=GUILD)
            a = Member(91, "u", False, roles=[verified], guild=GUILD)
            await cog.on_member_update(b, a)
            await drain()
            note(not a.removed, "정지 중에는 회수 안 함", str(a.removed))
            vs["enabled"] = True
            await vdb.save_verify_settings(GUILD.id, vs)

    # ── 8. link_guard on_message ───────────────────────────────────
    print("\n=== link_guard on_message ===")
    ls = await vdb.get_linkguard_settings(GUILD.id)
    ls.update({"enabled": True, "channel_ids": [111], "roblox_only": True})
    await vdb.save_linkguard_settings(GUILD.id, ls)
    wm = Msg(3000, ch, Member(3, "webhook"))
    wm.webhook_id = 12345
    wm.guild = GUILD
    e = discord.Embed(title="개인서버", description="https://www.roblox.com/games/123?privateServerLinkCode=abc")
    wm.embeds = [e]
    try:
        await lg.on_message(wm)
        await drain()
        note(True, "웹훅 임베드 처리", f"삭제됨={wm.deleted}")
    except Exception as ex:
        note(False, "웹훅 임베드 처리", f"{type(ex).__name__}: {ex}")
        import traceback; traceback.print_exc(limit=3)

    # 로블록스 링크가 없으면 건드리지 않아야 합니다
    wm2 = Msg(3001, ch, Member(3, "webhook"))
    wm2.webhook_id = 12345
    wm2.guild = GUILD
    wm2.embeds = [discord.Embed(title="공지", description="https://example.com/abc")]
    await lg.on_message(wm2)
    await drain()
    note(not wm2.deleted, "로블록스 아닌 링크는 원문 유지")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
