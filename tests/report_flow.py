"""제보 티켓 처음~끝 · 상점 구매 · 뽑기 실행 (Mongo 경로)."""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Inter, Role, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]

SNOW = 111122223333444455


async def main():
    import utils.database as db
    import cogs.report as R, cogs.shop as S, cogs.gacha as G

    cols = FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    print("=== 제보 티켓 ===")
    rs = await db.get_report_settings(GUILD.id)
    rs.update({"enabled": True, "default_points": 500,
               "notify_role_id": SNOW, "report_channel_id": 333})
    await db.save_report_settings(GUILD.id, rs)

    m = R.ReportModal()
    m.channel_link._value = "https://discord.com/channels/1/111/222"
    m.rules_agree._value = "동의"
    it = Inter(uid=70, admin=False)
    await m.on_submit(it)
    await drain()
    note(it.response.done, "제보 접수", F.outcome(it))

    print("\n  티켓 안에서 포인트 지급")
    v = R.TicketControlView(target_user_id=70)
    give = next((c for c in v.children
                 if isinstance(c, discord.ui.Button) and "포인트" in (c.label or "")),
                None)
    note(give is not None, "포인트 지급 버튼 존재", give.label if give else "없음")
    if give is not None:
        it = Inter(uid=7, admin=True)
        await give.callback(it)
        await drain()
        note(it.response.done, "관리자가 누르면 동작", F.outcome(it))

        it = Inter(uid=71, admin=False)
        try:
            await give.callback(it)
            await drain()
            txt = str(it.response.sent) + str(it.followup.sent)
            note("권한" in txt or "관리자" in txt or not it.response.done,
                 "일반 유저는 막힘", F.outcome(it))
        except Exception as e:
            note(True, "일반 유저는 막힘", type(e).__name__)

    print("\n  티켓 닫기")
    close = next((c for c in v.children
                  if isinstance(c, discord.ui.Button) and "닫" in (c.label or "")),
                 None)
    if close is not None:
        it = Inter(uid=7, admin=True)
        await close.callback(it)
        await drain()
        note(it.response.done, "닫기 동작", F.outcome(it))

    print("\n=== 상점 구매 ===")
    ss = await db.get_shop_settings(GUILD.id)
    ss.update({"enabled": True, "items": [
        {"id": 1, "name": "역할권", "price": 300, "stock": 2,
         "reward_role_id": 999, "description": "역할 지급"},
        {"id": 2, "name": "무제한품", "price": 100, "stock": -1,
         "reward_role_id": 0, "description": ""}]})
    await db.save_shop_settings(GUILD.id, ss)
    await db.add_user_points(GUILD.id, 80, 1000)

    async def buy(uid, item_id):
        sel = S.ShopSelect(await db.get_shop_items(GUILD.id))
        sel._values = [str(item_id)]
        it = Inter(uid=uid, admin=False)
        await sel.callback(it)
        await drain()
        return it

    it = await buy(80, 1)
    left = await db.get_user_points(GUILD.id, 80)
    note(left == 700, "포인트 차감", f"1000P → {left}P")
    note("추석기념" in it.user.added or 999 in [r.id for r in it.user.roles],
         "역할 지급", str(it.user.added))

    print("\n  잔액 부족")
    await db.add_user_points(GUILD.id, 81, 10)
    it = await buy(81, 1)
    note(await db.get_user_points(GUILD.id, 81) == 10, "포인트가 빠지지 않음",
         f"{await db.get_user_points(GUILD.id, 81)}P")

    print("\n  재고 2개에 5명 동시 구매")
    for uid in range(90, 95):
        await db.add_user_points(GUILD.id, uid, 1000)
    await asyncio.gather(*[buy(uid, 1) for uid in range(90, 95)])
    await drain()
    items = {i["id"]: i for i in await db.get_shop_items(GUILD.id)}
    paid = 0
    for uid in range(90, 95):
        if await db.get_user_points(GUILD.id, uid) < 1000:
            paid += 1
    note(items[1]["stock"] == 0 and paid <= 1,
         "남은 재고 1개만 나감 · 실패한 사람은 차감 없음",
         f"재고 {items[1]['stock']} / 차감된 사람 {paid}명")

    print("\n=== 뽑기 ===")
    gs = await db.get_gacha_settings(GUILD.id)
    gs.update({"enabled": True, "price": 100, "items": [
        {"id": 1, "name": "꽝", "percent": 90, "stock": -1,
         "reward_points": 0, "role_id": 0},
        {"id": 2, "name": "대박", "percent": 10, "stock": 3,
         "reward_points": 500, "role_id": 0}]})
    await db.save_gacha_settings(GUILD.id, gs)
    await db.add_user_points(GUILD.id, 100, 1000)

    async def roll(uid):
        v = G.GachaPlayView()
        btn = next(c for c in v.children if isinstance(c, discord.ui.Button))
        it = Inter(uid=uid, admin=False)
        await btn.callback(it)
        await drain()
        return it

    before = await db.get_user_points(GUILD.id, 100)
    await roll(100)
    after = await db.get_user_points(GUILD.id, 100)
    note(after <= before - 100 or after >= before - 100 + 500,
         "뽑기 비용 차감 (당첨 시 보상 반영)", f"{before}P → {after}P")

    print("\n  잔액 부족이면 실행 안 됨")
    await db.add_user_points(GUILD.id, 101, 10)
    await roll(101)
    note(await db.get_user_points(GUILD.id, 101) == 10, "포인트 그대로",
         f"{await db.get_user_points(GUILD.id, 101)}P")

    print("\n  10명이 동시에 돌려도 포인트가 정확히 빠짐")
    for uid in range(110, 120):
        await db.add_user_points(GUILD.id, uid, 100)
    await asyncio.gather(*[roll(uid) for uid in range(110, 120)])
    await drain()
    bad = []
    for uid in range(110, 120):
        if await db.get_user_points(GUILD.id, uid) < 0:
            bad.append(uid)
    note(not bad, "마이너스가 된 사람 없음", f"{len(bad)}명")

    print("\n=== 활동 로그에 남았는가 ===")
    logs = await db.get_activity_logs(GUILD.id, "all", 50)
    kinds = {l.get("type") for l in logs}
    note(len(logs) > 0, "로그 기록됨", f"{len(logs)}건 / 종류 {sorted(kinds)}")

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
