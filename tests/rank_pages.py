"""포인트 랭킹 30등 · 10명씩 3쪽.

여기서 지키는 것:

  1. **등수가 쪽을 넘어가도 이어져야 합니다.** 2쪽 첫 줄이 다시 1위로
     보이면 랭킹으로서 의미가 없습니다.
  2. **30명을 한 번에 읽고 잘라 씁니다.** 쪽마다 따로 읽으면 그 사이
     순위가 바뀌어 같은 사람이 두 쪽에 나오거나 아무 쪽에도 없게 됩니다.
  3. **사람이 없는 쪽 버튼은 잠깁니다.** 눌러도 빈 화면이면 고장으로 보입니다.
  4. **캐시에 없는 멤버는 한꺼번에** 받아옵니다. 순서대로 부르면 30명일 때
     REST 왕복 30번이라 3초 제한을 넘깁니다(실제로 그렇게 죽었습니다).
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Inter, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


async def main():
    import utils.database as db

    cols = FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"
    print(f"가짜 컬렉션 장착: {len(cols)}개")

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")

    # ⚠️ load_extension 은 sys.modules 를 새 인스턴스로 덮어씁니다.
    CK = sys.modules["cogs.checkin"]
    cog = bot.get_cog("CheckIn")
    note(CK.__dict__ is cog.point_ranking.callback.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    base_members = list(GUILD.members)

    async def seed(count, *, in_guild=True):
        """포인트가 다른 유저 `count` 명을 만듭니다 (1위가 가장 많음)."""
        db.users_col.docs.clear()
        GUILD.members[:] = list(base_members)
        for i in range(count):
            uid = 9000 + i
            await db.add_user_points(GUILD.id, uid, (count - i) * 100)
            if in_guild:
                GUILD.members.append(Member(uid, f"유저{i + 1:02}", guild=GUILD))

    def lines_of(embed):
        f = next((x for x in embed.fields if "순위" in (x.name or "")), None)
        return (f.value or "").split("\n") if f else []

    print("=== 1. 30명을 10명씩 세 쪽으로 ===")
    await seed(30)
    it = Inter(uid=9000)
    embed, total = await CK.build_rank_page(it, 0)
    note(total == 30, "30명을 읽어옴", str(total))
    note(len(lines_of(embed)) == 10, "한 쪽은 10명", str(len(lines_of(embed))))
    note("TOP 30" in (embed.title or ""), "제목이 30등까지임을 밝힘", embed.title)
    note("1~10위" in (embed.description or ""), "몇 위 구간인지 보임",
         embed.description)
    note("1/3쪽" in (embed.description or ""), "쪽 번호가 보임")

    print("")
    print("  ⚠️ 등수가 쪽을 넘어가도 **이어져야** 합니다")
    for page, want_first, want_last in ((0, "1", "10"), (1, "11", "20"),
                                        (2, "21", "30")):
        embed, _ = await CK.build_rank_page(it, page)
        ls = lines_of(embed)
        note(f"{want_first}위" in ls[0] or (page == 0 and "🥇" in ls[0]),
             f"{page + 1}쪽 첫 줄이 {want_first}위", ls[0][:40])
        note(f"{want_last}위" in ls[-1], f"{page + 1}쪽 끝 줄이 {want_last}위",
             ls[-1][:40])

    print("")
    print("  메달은 1·2·3위에만 붙어야 합니다")
    embed, _ = await CK.build_rank_page(it, 0)
    ls = lines_of(embed)
    note(ls[0].startswith("🥇") and ls[1].startswith("🥈")
         and ls[2].startswith("🥉"), "1~3위는 메달", ls[0][:20])
    note(not any(m in ls[3] for m in ("🥇", "🥈", "🥉")), "4위부터는 숫자")
    embed2, _ = await CK.build_rank_page(it, 1)
    note(not any(m in l for l in lines_of(embed2)
                 for m in ("🥇", "🥈", "🥉")), "2쪽에는 메달이 없음")

    print("")
    print("  내 랭킹은 **어느 쪽에서나** 보여야 합니다")
    for page in range(3):
        embed, _ = await CK.build_rank_page(it, page)
        blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
        note("내 랭킹 정보" in blob, f"{page + 1}쪽에도 내 순위가 있음")

    print("")
    print("=== 2. 버튼 상태 ===")
    v = CK.RankView(0, 30)
    ids = [b.custom_id for b in v.children]
    note(v.timeout is None, "영구 View (timeout=None)")
    note(all(ids) and len(set(ids)) == 3, "custom_id 가 고정·고유", str(ids))
    note([b.disabled for b in v.children] == [True, False, False],
         "보고 있는 쪽만 잠김", str([b.disabled for b in v.children]))
    note([b.label for b in v.children] == ["1~10위", "11~20위", "21~30위"],
         "버튼에 구간이 적혀 있음", str([b.label for b in v.children]))

    for page in range(3):
        st = [b.disabled for b in CK.RankView(page, 30).children]
        note(st.count(True) == 1 and st[page] is True,
             f"{page + 1}쪽을 보면 그 버튼만 잠김", str(st))

    print("")
    print("  ⚠️ 사람이 없는 쪽은 눌리면 안 됩니다 (빈 화면 = 고장으로 보임)")
    st = [b.disabled for b in CK.RankView(0, 12).children]
    note(st == [True, False, True], "12명이면 3쪽이 잠김", str(st))
    st = [b.disabled for b in CK.RankView(0, 5).children]
    note(st == [True, True, True], "5명이면 2·3쪽 모두 잠김", str(st))
    st = [b.disabled for b in CK.RankView(0, 21).children]
    note(st == [True, False, False], "21명이면 3쪽이 열림", str(st))

    print("")
    print("=== 3. 사람이 30명보다 적을 때 ===")
    await seed(12)
    embed, total = await CK.build_rank_page(it, 0)
    note(total == 12, "있는 만큼만 읽음", str(total))
    note(len(lines_of(embed)) == 10, "1쪽은 10명 그대로")
    embed, _ = await CK.build_rank_page(it, 1)
    note(len(lines_of(embed)) == 2, "2쪽은 남은 2명", str(len(lines_of(embed))))
    note("11~12위" in (embed.description or ""), "구간도 실제에 맞춤",
         embed.description)

    print("")
    print("  아무도 없으면 이유를 설명해야 합니다")
    db.users_col.docs.clear()
    embed, total = await CK.build_rank_page(it, 0)
    note(total == 0, "0명", str(total))
    note("없습니다" in (embed.description or ""), "빈 상태를 설명",
         embed.description)

    print("")
    print("=== 4. 버튼 콜백이 실제로 동작해야 ===")
    # cog 로드만으로는 NameError 를 잡지 못합니다.
    await seed(30)
    for page in range(3):
        btn = CK.RankPageButton(page)
        it2 = Inter(uid=9000)
        try:
            await btn.callback(it2)
            await drain()
            edited = it2.response.edited
            note(edited is not None, f"[{btn.label}] 이 화면을 고침",
                 F.outcome(it2))
            note(edited and edited.get("embed") is not None,
                 f"[{btn.label}] 임베드를 넘김")
            got = edited.get("embed") if edited else None
            if got:
                note(f"{page * 10 + 1}~" in (got.description or ""),
                     f"[{btn.label}] 이 맞는 구간을 보여줌", got.description[:44])
        except Exception as e:
            import traceback
            note(False, f"[{btn.label}] 예외", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=3)

    print("")
    print("  ⚠️ 새 메시지를 보내지 않아야 합니다 (쪽마다 화면이 쌓이면 안 됨)")
    btn = CK.RankPageButton(1)
    it3 = Inter(uid=9000)
    await btn.callback(it3)
    await drain()
    note(not it3.followup.sent and not it3.response.sent,
         "followup/새 메시지 없음", F.outcome(it3))
    note(it3.response.edited is not None, "제자리에서 고침")

    print("")
    print("=== 5. /포인트랭킹 명령어 ===")
    it4 = Inter(uid=9000)
    await cog.point_ranking.callback(cog, it4)
    await drain()
    sent = it4.followup.sent or it4.response.sent
    note(sent, "응답함", F.outcome(it4))
    note(sent and sent[0].get("ephemeral"), "ephemeral")
    note(sent and sent[0].get("view") is not None, "쪽 버튼이 함께 나감")
    emb = sent[0].get("embed")
    note(emb and "1~10위" in (emb.description or ""), "첫 쪽부터 보여줌")

    print("")
    print("=== 6. ⚠️ 캐시에 없는 멤버는 한꺼번에 받아와야 ===")
    # 순서대로 부르면 30명일 때 REST 왕복 30번이라 3초를 넘깁니다.
    await seed(30, in_guild=False)          # 전원 캐시에 없음
    calls = {"n": 0, "concurrent": 0, "live": 0}
    real_fetch = GUILD.fetch_member

    async def counting_fetch(uid):
        calls["n"] += 1
        calls["live"] += 1
        calls["concurrent"] = max(calls["concurrent"], calls["live"])
        await asyncio.sleep(0)              # 왕복 흉내
        calls["live"] -= 1
        raise discord.NotFound(
            type("R", (), {"status": 404, "reason": "x"})(), "없음")

    GUILD.fetch_member = counting_fetch
    try:
        embed, _ = await CK.build_rank_page(Inter(uid=9000), 0)
        note(calls["n"] == 10, "빠진 10명만 조회", str(calls["n"]))
        note(calls["concurrent"] > 1,
             "**순서대로가 아니라 동시에** 보냄", f"동시 {calls['concurrent']}건")
        note("서버 퇴장" in " ".join(lines_of(embed)),
             "못 찾은 사람은 퇴장으로 표시")
    finally:
        GUILD.fetch_member = real_fetch

    print("")
    print("  캐시에 있으면 REST 를 아예 안 불러야 합니다")
    await seed(30)
    calls["n"] = 0
    GUILD.fetch_member = counting_fetch
    try:
        await CK.build_rank_page(Inter(uid=9000), 0)
        note(calls["n"] == 0, "REST 호출 0회", str(calls["n"]))
    finally:
        GUILD.fetch_member = real_fetch

    GUILD.members[:] = list(base_members)
    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
