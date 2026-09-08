"""모든 cog 의 설정 메뉴 · 버튼 콜백을 실제로 호출합니다."""
import sys, os, tempfile, asyncio, inspect

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F
from fakes import Inter, run, note, GUILD


async def sweep(label, make_select):
    """Select 의 모든 옵션을 하나씩 골라 콜백을 실행합니다."""
    probe = make_select()
    for opt in list(probe.options):
        sel = make_select()
        sel._values = [opt.value]
        v = discord.ui.View(timeout=None)
        try:
            v.add_item(sel)
        except Exception:
            pass
        await run(f"{label} / {opt.value}", sel.callback)


async def buttons(label, view, names=None):
    for item in view.children:
        if not isinstance(item, discord.ui.Button):
            continue
        cb = getattr(item, "callback", None)
        if cb is None:
            continue
        nm = item.label or item.custom_id or "btn"
        if names and nm not in names:
            continue
        await run(f"{label} / [{nm}]", lambda it, c=cb: c(it))


async def main():
    import utils.database as db
    import utils.verify_db as vdb

    print("=== /출석설정 ===")
    import cogs.checkin as C
    await sweep("출석설정",
                lambda: C.CheckinSettingsSelect(
                    asyncio.get_event_loop().run_until_complete
                    if False else SETTINGS["checkin"]))

    print("\n=== /포인트얻는법 ===")
    await sweep("포인트얻는법", lambda: C.PointGuideSelect())

    print("\n=== /퀘스트설정 ===")
    import cogs.quest as Q
    st = await db.get_quest_settings(1)
    await sweep("퀘스트설정", lambda: Q.QuestSettingsSelect(st, GUILD))
    await sweep("  일일", lambda: Q.DailySettingsSelect(st, GUILD))
    await sweep("  주간", lambda: Q.WeeklySettingsSelect(st))
    await sweep("  공동", lambda: Q.GoalSettingsSelect(st, GUILD))
    await sweep("  이벤트", lambda: Q.EventMenuSelect([]))

    print("\n=== /상점설정 ===")
    import cogs.shop as S
    ss = await db.get_shop_settings(1)
    await sweep("상점설정", lambda: S.ShopSettingsSelect(ss))

    print("\n=== /뽑기설정 ===")
    import cogs.gacha as G
    gs = await db.get_gacha_settings(1)
    await sweep("뽑기설정", lambda: G.GachaSettingsSelect(gs))

    print("\n=== /제보설정 ===")
    import cogs.report as R
    rs = await db.get_report_settings(1)
    await sweep("제보설정", lambda: R.ReportSettingsSelect(rs))

    print("\n=== /채널부스트 ===")
    import cogs.chat_reward as CR
    await sweep("채널부스트", lambda: CR.ChannelBoostSettingsSelect(0))

    print("\n=== /로그 ===")
    import cogs.logs as L
    await sweep("로그", lambda: L.LogCategorySelect())

    print("\n=== /백업설정 ===")
    import cogs.backup as B
    await sweep("백업설정", lambda: B.BackupMenu())

    print("\n=== /인증설정 ===")
    import cogs.verify as V
    vs = await vdb.get_verify_settings(1)
    await sweep("인증설정", lambda: V.VerifySettingsSelect(vs, GUILD))

    print("\n=== /링크보호설정 ===")
    import cogs.link_guard as LG
    ls = await vdb.get_linkguard_settings(1)
    await sweep("링크보호설정", lambda: LG.GuardSettingsSelect(ls))

    print("\n=== 버튼 ===")
    await buttons("블랙리스트", __import__("cogs.blacklist", fromlist=["x"]).BlacklistMainView())
    await buttons("인증패널", V.VerifyPanelView())
    await buttons("전원", V.SystemPowerView(vs, ls))
    await buttons("제보열기", R.TicketLaunchView())
    await buttons("링크버튼", LG.ProtectedLinkView())
    await buttons("사용통계", LG.UsageStatsView())
    await buttons("뽑기실행", G.GachaPlayView())
    await buttons("백업채널", B.BackupChannelView())
    await buttons("상태채널", B.StatusChannelView())
    await buttons("퀘스트채널", Q.QuestChannelView("quest_channel_ids", "랜덤"))
    await buttons("자동삭제", LG.AutoDeleteToggleView(ls))
    await buttons("로그채널", LG.LogChannelView())
    await buttons("필수역할", V.RequiredRoleView([902], GUILD))

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for f in F.FAILS:
        print("   -", f)


SETTINGS = {}


async def boot():
    import utils.database as db
    SETTINGS["checkin"] = await db.get_checkin_settings(1)
    await main()


asyncio.run(boot())
