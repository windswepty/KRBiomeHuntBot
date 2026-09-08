"""꺼짐/켜짐 × 관리자전용 on/off 네 가지 상태를 관리자·일반 유저로 각각 확인."""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakemongo as FM
import fakes as F
from fakes import GUILD, Inter, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


def title_of(it):
    for s in it.followup.sent + it.response.sent:
        e = s.get("embed")
        if e is not None:
            return e.title or ""
    return "(없음)"


def body_of(it):
    out = []
    for s in it.followup.sent + it.response.sent:
        e = s.get("embed")
        if e is not None:
            out.append((e.title or "") + (e.description or ""))
            out += [(f.name or "") + (f.value or "") for f in (e.fields or [])]
    return "\n".join(out)


async def main():
    import utils.database as db
    import cogs.quest as Q

    FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    await bot.load_extension("cogs.quest")
    cog = bot.get_cog("Quest")
    cmd = next(c for c in bot.tree.get_commands() if c.name == "퀘스트")

    async def run_quest(admin):
        it = Inter(uid=7 if admin else 8, admin=admin)
        await cmd.callback(cog, it)
        await drain()
        return it

    async def state(enabled, admin_only):
        await db.save_quest_settings(GUILD.id, {"enabled": enabled,
                                                "admin_only": admin_only,
                                                "daily_count": 3})
        db._quest_settings_cache.pop(str(GUILD.id))
        return await db.get_quest_settings(GUILD.id)

    USER_CLOSED = "⚠️ 퀘스트가 아직 열리지 않았습니다"

    print("=== ① 꺼짐 + 관리자전용 켜짐  (지금 겪으신 상태) ===")
    st = await state(False, True)
    it = await run_quest(admin=True)
    note(title_of(it) == "⚙️ 퀘스트가 꺼져 있습니다",
         "관리자: 무엇을 눌러야 하는지 안내", title_of(it))
    note("⚡ 퀘스트 켜기" in body_of(it), "켜는 방법을 정확히 지목")
    note("관리자 전용 모드는 켜져 있습니다" in body_of(it),
         "관리자 전용은 켜져 있다고 알려줌")

    it = await run_quest(admin=False)
    note(title_of(it) == USER_CLOSED, "일반 유저: 평소 문구 그대로", title_of(it))

    panel = Q.build_quest_panel(st)
    names = [f.name for f in panel.fields]
    note("공개 범위" in names, "패널에 공개 범위가 보임", str(names))
    note(any("아무도 쓸 수 없습니다" in (f.name or "") for f in panel.fields),
         "패널이 지금 아무도 못 쓴다고 경고")

    print("\n=== ② 켜짐 + 관리자전용 켜짐  (테스트 상태) ===")
    st = await state(True, True)
    it = await run_quest(admin=True)
    note(title_of(it) == "📋 나의 퀘스트", "관리자: 퀘스트가 보임", title_of(it))
    note("관리자 전용" in body_of(it), "테스트 중임을 표시")

    it = await run_quest(admin=False)
    note(title_of(it) == USER_CLOSED, "일반 유저: 꺼진 것과 똑같음", title_of(it))

    panel = Q.build_quest_panel(st)
    note(any("테스트 중" in (f.name or "") for f in panel.fields),
         "패널이 테스트 중이라고 표시")

    print("\n=== ③ 켜짐 + 관리자전용 꺼짐  (공개 상태) ===")
    st = await state(True, False)
    for admin in (True, False):
        it = await run_quest(admin)
        note(title_of(it) == "📋 나의 퀘스트",
             f"{'관리자' if admin else '일반 유저'}: 퀘스트가 보임", title_of(it))
    panel = Q.build_quest_panel(st)
    val = next(f.value for f in panel.fields if f.name == "공개 범위")
    note("모두" in val, "패널: 공개 범위 = 모두", val)

    print("\n=== ④ 꺼짐 + 관리자전용 꺼짐 ===")
    st = await state(False, False)
    it = await run_quest(admin=True)
    note(title_of(it) == "⚙️ 퀘스트가 꺼져 있습니다", "관리자: 켜라고 안내",
         title_of(it))
    note("관리자 전용 모드는 켜져 있습니다" not in body_of(it),
         "관리자 전용 얘기는 안 함 (꺼져 있으므로)")
    it = await run_quest(admin=False)
    note(title_of(it) == USER_CLOSED, "일반 유저: 평소 문구", title_of(it))

    print("\n=== 일반 유저에게는 두 상태가 구분되지 않아야 함 ===")
    await state(False, False)
    a = body_of(await run_quest(admin=False))
    await state(True, True)
    b = body_of(await run_quest(admin=False))
    note(a == b, "'꺼짐'과 '테스트 중'이 완전히 동일하게 보임",
         "다르면 준비 중인 게 드러납니다")

    print("\n=== 토글할 때 미리 알려주는가 ===")
    await state(False, False)
    sel = Q.QuestSettingsSelect(await db.get_quest_settings(GUILD.id), GUILD)
    sel._values = ["adminonly"]
    it = Inter()
    await sel.callback(it)
    await drain()
    txt = body_of(it)
    note("아직 퀘스트가 꺼져 있습니다" in txt,
         "퀘스트가 꺼진 채 켜면 경고", txt[:60].replace("\n", " "))

    await state(True, False)
    sel = Q.QuestSettingsSelect(await db.get_quest_settings(GUILD.id), GUILD)
    sel._values = ["adminonly"]
    it = Inter()
    await sel.callback(it)
    await drain()
    note("아직 퀘스트가 꺼져 있습니다" not in body_of(it),
         "이미 켜져 있으면 그 경고는 안 뜸")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
