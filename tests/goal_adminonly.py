"""관리자 전용 모드일 때 공동 목표 안내가 공개 채널에 올라가지 않는지."""
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
from fakes import GUILD, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


async def main():
    import utils.database as db
    FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    await bot.load_extension("cogs.quest")
    cog = bot.get_cog("Quest")
    ch = GUILD.get_channel(333)

    async def state(admin_only):
        await db.save_quest_settings(GUILD.id, {
            "enabled": True, "goal_enabled": True, "admin_only": admin_only,
            "goal_channel_id": "333", "goal_target": 3000, "goal_reward": 200})
        db._quest_settings_cache.pop(str(GUILD.id))
        ch.sent.clear()

    print("=== 관리자 전용 모드 ON — 공개 채널에 아무것도 올라가면 안 됨 ===")
    await state(True)
    await cog.update_goal_message(GUILD)
    await drain()
    note(len(ch.sent) == 0, "안내 임베드가 올라가지 않음", f"{len(ch.sent)}건 전송")

    print("\n  그래도 진행도는 평소처럼 쌓여야 합니다")
    await db.bump_server_goal(GUILD.id, 5)
    st = await db.get_server_goal(GUILD.id)
    note(st["progress"] == 5, "공동 목표 집계는 계속됨", str(st["progress"]))

    print("\n=== 공개 상태 (관리자 전용 OFF) — 정상적으로 올라가야 함 ===")
    await state(False)
    await cog.update_goal_message(GUILD)
    await drain()
    note(len(ch.sent) == 1, "안내 임베드가 올라감", f"{len(ch.sent)}건 전송")
    if ch.sent:
        e = ch.sent[0].get("embed")
        note(e is not None and "목표" in (e.title or ""), "내용 정상",
             (e.title if e else "없음"))

    print("\n=== 공동 목표 자체가 꺼져 있으면 (관리자 전용과 무관하게) ===")
    await db.save_quest_settings(GUILD.id, {"enabled": True,
                                            "goal_enabled": False,
                                            "admin_only": False,
                                            "goal_channel_id": "333"})
    db._quest_settings_cache.pop(str(GUILD.id))
    ch.sent.clear()
    await cog.update_goal_message(GUILD)
    await drain()
    note(len(ch.sent) == 0, "올라가지 않음", f"{len(ch.sent)}건")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
