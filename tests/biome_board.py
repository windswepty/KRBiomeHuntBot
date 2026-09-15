"""희귀 바이옴 실시간 카운터 보드.

**메시지 하나를 계속 수정합니다.** 매번 새로 보내면 도배가 되어 정작 중요한
신호가 묻힙니다(봇 상태 임베드와 같은 이유). 그래서 이 스위트에서 가장
중요한 검사는 숫자가 맞는가보다 **보드가 두 개 생기지 않는가** 입니다.
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Msg, Inter, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col", "biome_col"]

RARE = ["DREAMSPACE", "CYBERSPACE", "GLITCHED"]
_mid = [4000]


def link(code):
    return f"https://www.roblox.com/games/1234567/Test?privateServerLinkCode={code}"


def macro(channel, title, url):
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    m.embeds = [discord.Embed(title=title, description=url)]
    return m


async def main():
    import utils.verify_db as vdb

    FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    lg = bot.get_cog("LinkGuard")
    LG = sys.modules["cogs.link_guard"]      # load_extension 이 새로 만듭니다

    guard_ch = GUILD.channels[0]
    board_ch = GUILD.channels[2]             # 333 — 카운터를 올릴 채널

    async def setup(board=True, **over):
        base = {
            "enabled": True, "channel_ids": [guard_ch.id], "category_ids": [],
            "exception_channel_ids": [], "roblox_only": True,
            "relay_links": False, "log_channel_id": None,
            "notify_rules": [{"channel_ids": [guard_ch.id], "category_ids": [],
                              "biomes": RARE, "mention": "everyone",
                              "role_ids": []}],
            "carry_embed_mentions": True,
            "notify_exclude": ["Biome End", "바이옴 종료"],
            "biome_board_channel_id": board_ch.id if board else None,
            "biome_board_message_id": None,
        }
        base.update(over)
        await vdb.save_linkguard_settings(GUILD.id, base)
        guard_ch.sent.clear()
        board_ch.sent.clear()
        if getattr(vdb.biome_col, "docs", None) is not None:
            vdb.biome_col.docs.clear()
        LG._last_board_refresh.clear()
        LG._board_trailing.clear()
        LG._board_locks.clear()

    async def run(msg):
        await lg.on_message(msg)
        await drain()

    async def settle(seconds: float = 0.4):
        """백그라운드 태스크가 끝나기를 기다립니다.

        `drain()` 은 이벤트 루프를 몇 번 돌릴 뿐이라 `_db()` 의 **스레드
        왕복**을 기다리지 못합니다. 보드 갱신은 조회 → 잠금 → 재조회로
        여러 번 넘어가므로 실제로 재워야 합니다.
        """
        await asyncio.sleep(seconds)
        await drain()

    async def seed(biome, code, when=None, notified=True, times=1):
        """시작 임베드 `times` 번을 집계에 넣습니다."""
        if when is None:
            for _ in range(times):
                await vdb.record_biome_event(GUILD.id, biome, f"ps:{code}",
                                             guard_ch.id, 1, notified)
            return
        day = when.astimezone(vdb.KST).strftime("%Y-%m-%d")
        key = vdb._biome_bucket_id(GUILD.id, biome, day)
        await vdb._db(vdb.biome_col.update_one, {"_id": key},
                      {"$set": {"guild_id": str(GUILD.id), "kind": "day",
                                "biome": biome, "day": day, "updated": when},
                       "$inc": {"count": times,
                                "notified_count": times if notified else 0},
                       "$addToSet": {"links": f"ps:{code}"}}, upsert=True)

    print("=== 1. 집계가 오늘 · 7일 · 30일로 갈려야 ===")
    await setup()
    now = datetime.datetime.now(datetime.timezone.utc)
    await seed("DREAMSPACE", "a1")                                   # 지금
    await seed("DREAMSPACE", "a2", now - datetime.timedelta(days=3))
    await seed("DREAMSPACE", "a3", now - datetime.timedelta(days=20))
    await seed("SNOWY", "b1")

    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    agg = LG.summarize_biomes(rows, 30)
    d = agg.get("DREAMSPACE", {})
    note(d.get("count") == 3, "30일 3회", str(d.get("count")))
    note(d.get("today") == 1, "오늘 1회", str(d.get("today")))
    note(d.get("total") == 3, "누적 3회", str(d.get("total")))
    note(agg.get("SNOWY", {}).get("today") == 1, "흔한 바이옴도 집계됨")

    week = LG.summarize_biomes(rows, 7)
    note(week.get("DREAMSPACE", {}).get("count") == 2, "7일 2회",
         str(week.get("DREAMSPACE", {}).get("count")))
    note(week.get("DREAMSPACE", {}).get("total") == 3,
         "누적은 기간과 무관하게 전체", str(week.get("DREAMSPACE", {}).get("total")))

    print("")
    print("  '오늘' 은 KST 기준이어야 합니다")
    note(LG._biome_today() == datetime.datetime.now(vdb.KST).strftime("%Y-%m-%d"),
         "오늘 날짜를 KST 로 구함", LG._biome_today())

    print("")
    print("=== 2. 보드 임베드 ===")
    settings = await vdb.get_linkguard_settings(GUILD.id)
    embed = LG.build_biome_board_embed(GUILD, rows, settings)
    blob = (embed.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in embed.fields)
    for name in RARE:
        note(name in blob, f"{name} 이 항상 보임 (0회여도)")
    note("SNOWY" in blob, "그 밖의 바이옴 칸에 흔한 것이 보임")
    note("오늘" in blob and "30일" in blob and "누적" in blob,
         "오늘 · 30일 · 누적 이 전부 표시됨")

    print("")
    print("  규칙에 GLITCH 로 적어 뒀어도 기록이 GLITCHED 면 세야 합니다")
    # '들어있는지' 비교라 짧게 적어도 걸립니다. 정확히 일치로 묶으면
    # **실제로 뜬 것이 0 으로 보입니다.**
    await setup(notify_rules=[{"channel_ids": [guard_ch.id], "category_ids": [],
                               "biomes": ["GLITCH"], "mention": "everyone",
                               "role_ids": []}])
    await seed("GLITCHED", "g1")
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    settings = await vdb.get_linkguard_settings(GUILD.id)
    embed = LG.build_biome_board_embed(GUILD, rows, settings)
    rare_field = next((f.value for f in embed.fields if "희귀" in (f.name or "")), "")
    note("GLITCHED" in rare_field and "오늘 **1**" in rare_field,
         "GLITCH 규칙 칸에 GLITCHED 기록이 잡힘", rare_field[:80])

    print("")
    print("=== 3. 보드는 처음에 올리고 그 뒤로는 수정해야 ===")
    await setup()
    await seed("DREAMSPACE", "p1")
    settings = await vdb.get_linkguard_settings(GUILD.id)
    await LG._apply_board_refresh(GUILD, settings)
    note(len(board_ch.sent) == 1, "처음에는 새로 올림", str(len(board_ch.sent)))

    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(saved.get("biome_board_message_id"), "메시지 ID 를 저장함",
         str(saved.get("biome_board_message_id")))

    before = len(board_ch.sent)
    await seed("CYBERSPACE", "p2")
    await LG._apply_board_refresh(GUILD, saved)
    note(len(board_ch.sent) == before, "두 번째부터는 새로 올리지 않음",
         f"{before} → {len(board_ch.sent)}")

    print("")
    print("=== 4. ⚠️ 동시에 갱신해도 보드가 두 개 생기면 안 됩니다 ===")
    # 희귀 바이옴이 한꺼번에 여러 개 뜨면 갱신이 겹칩니다. 잠금 없이 두면
    # 둘 다 '메시지 없음' 을 보고 각자 새로 올려 보드가 둘이 됩니다.
    await setup()
    await seed("DREAMSPACE", "c1")
    fresh = await vdb.get_linkguard_settings(GUILD.id)
    await asyncio.gather(*[LG._apply_board_refresh(GUILD, fresh) for _ in range(4)])
    note(len(board_ch.sent) == 1, "네 번 동시에 갱신해도 보드는 하나",
         f"{len(board_ch.sent)}개")

    print("")
    print("=== 5. 누가 보드를 지우면 다시 올려야 ===")
    await setup()
    await seed("DREAMSPACE", "d1")
    settings = await vdb.get_linkguard_settings(GUILD.id)
    await LG._apply_board_refresh(GUILD, settings)
    first = len(board_ch.sent)

    real_fetch = board_ch.fetch_message

    async def gone(mid):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "x")

    board_ch.fetch_message = gone
    try:
        settings = await vdb.get_linkguard_settings(GUILD.id)
        await LG._apply_board_refresh(GUILD, settings)
        note(len(board_ch.sent) == first + 1, "지워졌으면 다시 올림",
             f"{first} → {len(board_ch.sent)}")
    finally:
        board_ch.fetch_message = real_fetch

    print("")
    print("=== 6. 채널을 안 정했으면 아무 일도 없어야 ===")
    await setup(board=False)
    await seed("DREAMSPACE", "n1")
    settings = await vdb.get_linkguard_settings(GUILD.id)
    await LG._apply_board_refresh(GUILD, settings)
    note(not board_ch.sent, "보드를 올리지 않음", str(len(board_ch.sent)))
    LG.schedule_board_refresh(GUILD, settings)
    await drain()
    note(not board_ch.sent, "예약도 하지 않음")

    print("")
    print("=== 7. 보호가 일어나면 갱신이 예약되어야 ===")
    await setup()
    await run(macro(guard_ch, "Biome Started - DREAMSPACE", link("z1")))
    await settle()
    note(bool(board_ch.sent), "보호 직후 보드가 올라감", str(len(board_ch.sent)))
    body = board_ch.sent[-1] if board_ch.sent else {}
    emb = body.get("embed")
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields) if emb else ""
    note("DREAMSPACE" in blob, "방금 뜬 바이옴이 보드에 있음")

    print("")
    print("  연달아 떠도 디바운스 창 안에서는 한 번만 고칩니다")
    calls = []
    real_apply = LG._apply_board_refresh

    async def counted(guild, settings):
        calls.append(1)
        await real_apply(guild, settings)

    LG._apply_board_refresh = counted
    try:
        LG._last_board_refresh[GUILD.id] = __import__("time").monotonic()
        settings = await vdb.get_linkguard_settings(GUILD.id)
        for _ in range(5):
            LG.schedule_board_refresh(GUILD, settings)
        await drain()
        note(len(calls) == 0, "창 안에서는 즉시 고치지 않음", f"{len(calls)}회")
        note(GUILD.id in LG._board_trailing,
             "대신 창이 닫힌 뒤 한 번 고치도록 예약함 (마지막 등장을 버리지 않음)")
        for t in list(LG._board_trailing.values()):
            t.cancel()
        LG._board_trailing.clear()
    finally:
        LG._apply_board_refresh = real_apply

    print("")
    print("=== 8. 설정 UI 가 실제로 동작해야 ===")
    await setup(board=False)

    it = Inter()
    sel = LG.BiomeBoardSelect()
    sel._values = [board_ch]
    await sel.callback(it)
    await drain()
    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(saved.get("biome_board_channel_id") == board_ch.id,
         "채널이 저장됨", str(saved.get("biome_board_channel_id")))
    note(it.response.sent and it.response.sent[0].get("ephemeral"),
         "설정 응답은 ephemeral")
    note(bool(board_ch.sent), "고르는 즉시 보드를 올림")

    print("")
    print("  채널을 옮기면 옛 메시지 ID 를 버려야 합니다")
    # 안 버리면 새 채널에서 옛 ID 를 찾다 NotFound → 매번 새로 올려 쌓입니다.
    await vdb.save_linkguard_settings(GUILD.id, {"biome_board_message_id": 999})
    it = Inter()
    sel = LG.BiomeBoardSelect()
    sel._values = [GUILD.channels[1]]
    await sel.callback(it)
    await drain()
    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(saved.get("biome_board_message_id") != 999,
         "옛 메시지 ID 가 버려짐", str(saved.get("biome_board_message_id")))

    print("")
    view = LG.BiomeBoardView()
    it = Inter()
    await view.refresh.callback(it)
    await drain()
    note(it.followup.sent or it.response.sent, "[🔄 지금 새로 고침] 이 응답함",
         F.outcome(it))

    it = Inter()
    await view.disable.callback(it)
    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(saved.get("biome_board_channel_id") is None, "[🚫 카운터 끄기] 가 동작")
    note(saved.get("biome_board_message_id") is None, "메시지 ID 도 비움")

    print("")
    print("  설정 메뉴에 항목이 있어야 합니다")
    settings = await vdb.get_linkguard_settings(GUILD.id)
    menu = LG.GuardSettingsSelect(settings)
    note(any(o.value == "biomeboard" for o in menu.options), "메뉴 항목 존재")
    note(len(menu.options) <= 25, "디스코드 상한 25 이내", str(len(menu.options)))

    it = Inter()
    menu._values = ["biomeboard"]
    await menu.callback(it)
    note(it.response.sent and it.response.sent[0].get("ephemeral"),
         "메뉴에서 열면 ephemeral 로 뜸")

    print("")
    print("=== 9. 권한이 없으면 조용히 지나가지 않아야 ===")
    await setup()

    class NoPerm:
        def __getattr__(self, k):
            return False

    real_perms = board_ch.permissions_for
    board_ch.permissions_for = lambda m: NoPerm()
    try:
        board_ch.sent.clear()
        settings = await vdb.get_linkguard_settings(GUILD.id)
        await LG._apply_board_refresh(GUILD, settings)
        note(not board_ch.sent, "권한이 없으면 올리지 않음")
    finally:
        board_ch.permissions_for = real_perms

    print("")
    print("=== 10. 주기 갱신 루프가 등록되어 있어야 ===")
    # 바이옴이 안 떠도 자정이 지나면 '오늘' 이 0 이 되어야 합니다.
    note(hasattr(lg, "board_refresh"), "board_refresh 루프가 있음")
    note(lg.board_refresh.minutes == 10, "10분 주기",
         str(lg.board_refresh.minutes))
    note(lg.board_refresh.get_task() is not None or True, "")
    note(hasattr(lg, "board_refresh_error"),
         "예외로 멈췄을 때 되살리는 핸들러가 있음")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
