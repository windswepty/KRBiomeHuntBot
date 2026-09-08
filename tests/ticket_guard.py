"""같은 사람이 제보 티켓을 여러 개 만들 수 있는가."""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Inter, Channel, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


async def main():
    import utils.database as db
    # report.py 가 isinstance(x, discord.CategoryChannel) 로 카테고리를
    # 확인하므로, 대역 채널이 그 검사를 통과하도록 갈아끼웁니다.
    discord.CategoryChannel = Channel
    import cogs.report as R
    FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    cat = GUILD.categories[0]           # id 444
    cat.channels = []
    made = []

    async def make(name, category=None, overwrites=None):
        ch = Channel(9000 + len(made), name, GUILD)
        ch.category = category
        for target, ow in (overwrites or {}).items():
            ch._ow[getattr(target, "id", target)] = ow
        (category or GUILD).channels.append(ch)
        GUILD._chans[ch.id] = ch
        made.append(ch)
        return ch

    cat.create_text_channel = lambda *a, **k: make(k.get("name") or a[0],
                                                   cat, k.get("overwrites"))
    GUILD.create_text_channel = lambda *a, **k: make(
        k.get("name") or a[0], k.get("category"), k.get("overwrites"))

    rs = await db.get_report_settings(GUILD.id)
    rs.update({"enabled": True, "default_points": 500,
               "report_channel_id": 444})
    await db.save_report_settings(GUILD.id, rs)

    async def submit(uid):
        m = R.ReportModal()
        m.channel_link._value = "https://discord.com/channels/1/111/222"
        m.rules_agree._value = "동의"
        it = Inter(uid=uid, admin=False)
        await m.on_submit(it)
        await drain()
        return it

    print("=== 첫 제보 ===")
    it = await submit(70)
    note(len(made) == 1, "티켓 채널이 생김", f"{len(made)}개")

    print("\n=== 같은 사람이 다시 제출 ===")
    it = await submit(70)
    txt = str(it.response.sent) + str(it.followup.sent)
    note(len(made) == 1, "새 채널이 생기지 않음", f"총 {len(made)}개")
    note("이미 열려 있는" in txt, "기존 티켓으로 안내", txt[:60])

    print("\n=== 연타 10회 ===")
    for _ in range(10):
        await submit(70)
    note(len(made) == 1, "여전히 1개", f"총 {len(made)}개")

    print("\n=== 다른 사람은 따로 열 수 있어야 합니다 ===")
    await submit(71)
    note(len(made) == 2, "새 사람은 새 티켓", f"총 {len(made)}개")
    await submit(72)
    note(len(made) == 3, "또 다른 사람도", f"총 {len(made)}개")

    print("\n=== 티켓이 닫히면 다시 열 수 있는가 ===")
    first = made[0]
    cat.channels.remove(first)          # 닫힘 = 채널 삭제
    await submit(70)
    note(len(made) == 4, "다시 열림", f"총 {len(made)}개")

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
