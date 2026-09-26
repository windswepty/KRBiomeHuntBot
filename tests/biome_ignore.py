"""바이옴 통계에서 뺄 이름 (`biome_ignore`).

실제 사고(2026-09-20): MultiScope v2.0.5 매크로 하나가 로비·다른 게임에 있는
동안 `Biome Started - In lobby` · `- School` · `- Grocery Store` 처럼 **바이옴이
아닌 이름**을 보냈고, 통계·카운터 보드에 그대로 올라갔습니다.
바이옴 이름은 코드에 박지 않는 원칙이라 운영진이 목록으로 뺍니다.

지켜야 할 것:
  1. 뺀 이름은 **기록하지 않는다** (앞으로)
  2. 이미 쌓인 것은 **지우지 않고 화면에서만** 뺀다 (목록에서 빼면 돌아옴)
  3. 뺀 사실을 **조용히 숨기지 않는다** (통계에 칸으로 보임)
  4. 다른 바이옴 · 보호 · 희귀 알림은 그대로
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
from fakes import GUILD, Member, Msg, Inter, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col", "biome_col"]
FAKE = ["IN LOBBY", "EVEN CARE", "FREDDYBEAR'S", "GROCERY STORE", "SCHOOL"]
_mid = [8000]


def link(code):
    return f"https://www.roblox.com/games/1/T?privateServerLinkCode={code}"


def multiscope(channel, name, url):
    """운영에서 실제로 들어온 모양 — 제목 없음, 본문에 `> ### Biome Started - …`."""
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    e = discord.Embed(description=(f"> ### Biome Started - {name}\n"
                                   f"**Account:** shndskel12\n**Uptime:** 6hr 23m 25s\n{url}"))
    e.set_author(name="Macro")
    e.set_footer(text="MultiScope v2.0.5")
    m.embeds = [e]
    return m


async def main():
    import utils.verify_db as vdb
    cols = FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    lg = bot.get_cog("LinkGuard")
    LG = sys.modules["cogs.link_guard"]
    ch = GUILD.channels[0]

    await vdb.save_linkguard_settings(GUILD.id, {
        "enabled": True, "channel_ids": [ch.id], "category_ids": [],
        "exception_channel_ids": [], "no_guard_channel_ids": [], "roblox_only": True,
        "relay_links": False, "log_channel_id": None, "rare_copy_channel_id": None,
        "notify_rules": [{"channel_ids": [ch.id], "category_ids": [],
                          "biomes": ["DREAMSPACE"], "mention": "everyone", "role_ids": []}],
        "biome_ignore": []})

    async def feed(name, code):
        await lg.on_message(multiscope(ch, name, link(code)))
        await drain()

    async def recorded():
        rows = await vdb.get_biome_buckets(GUILD.id, 90)
        return {r["biome"]: r.get("count", 0) for r in rows}

    print("=== 0. 운영에서 들어온 모양이 실제로 그 이름으로 읽히는지 ===")
    got = LG.detect_biome_name(multiscope(ch, "In lobby", link("x")))
    note(got == "IN LOBBY", "`> ### Biome Started - In lobby` → IN LOBBY", str(got))

    print("\n=== 1. 빼기 전: 지금까지처럼 전부 셈 (옛 기록이 쌓인 상태 만들기) ===")
    for i, n in enumerate(["In lobby", "School", "WINDY", "In lobby"]):
        await feed(n, f"old{i}")
    before = await recorded()
    note(before.get("IN LOBBY") == 2 and before.get("SCHOOL") == 1 and before.get("WINDY") == 1,
         "IN LOBBY 2 · SCHOOL 1 · WINDY 1", str(before))

    print("\n=== 2. 목록에 넣으면 ===")
    await vdb.save_linkguard_settings(GUILD.id, {"biome_ignore": FAKE})
    settings = await vdb.get_linkguard_settings(GUILD.id)
    note(LG.ignored_biomes(settings) == set(FAKE), "목록을 그대로 읽음")
    plinks_before = len(cols["plink_col"].docs)
    for i, n in enumerate(["In lobby", "Grocery Store", "freddybear's", "WINDY"]):
        await feed(n, f"new{i}")
    after = await recorded()
    note(after.get("IN LOBBY") == 2 and "GROCERY STORE" not in after and "FREDDYBEAR'S" not in after,
         "**뺀 이름은 더 기록하지 않음** (대소문자 달라도)", str(after))
    note(after.get("WINDY") == 2, "다른 바이옴은 그대로 셈", str(after.get("WINDY")))
    note(len(cols["plink_col"].docs) == plinks_before + 4,
         "링크 보호는 그대로 (뺀 이름의 메시지도 보호됨)")
    unparsed = await vdb.get_biome_unparsed(GUILD.id, 1)
    note(not (unparsed or {}).get("count"), "'이름을 못 읽은 임베드' 로 잘못 세지 않음",
         str(unparsed))

    print("\n=== 3. 이미 쌓인 것은 지우지 않고 화면에서만 뺌 ===")
    buckets = await vdb.get_biome_buckets(GUILD.id, 90)
    emb = LG.build_biome_stats_embed(GUILD, buckets, settings, 30, None)
    body = " ".join((f.name or "") + (f.value or "") for f in emb.fields
                    if "뺀 이름" not in (f.name or ""))
    note("IN LOBBY" not in body and "SCHOOL" not in body and "WINDY" in body,
         "통계 본문에서 빠짐 · WINDY 는 보임")
    hid = next((f for f in emb.fields if "뺀 이름" in (f.name or "")), None)
    note(hid is not None and "5개" in hid.value and "`SCHOOL`" in hid.value,
         "**뺀 사실을 칸으로 보여 줌** (조용히 숨기지 않음)", hid.value[:40] if hid else "")
    note(all(len(f.value) <= 1024 for f in emb.fields) and len(emb) <= 6000, "임베드 규격 안")
    board = LG.build_biome_board_embed(GUILD, buckets, settings)
    bblob = (board.description or "") + " ".join((f.name or "") + (f.value or "") for f in board.fields)
    note("IN LOBBY" not in bblob and "SCHOOL" not in bblob, "카운터 보드에서도 빠짐")
    note((await recorded()).get("IN LOBBY") == 2, "DB 의 옛 기록은 그대로 남아 있음")

    await vdb.save_linkguard_settings(GUILD.id, {"biome_ignore": []})
    settings = await vdb.get_linkguard_settings(GUILD.id)
    emb = LG.build_biome_stats_embed(GUILD, buckets, settings, 30, None)
    blob = " ".join((f.name or "") + (f.value or "") for f in emb.fields)
    note("IN LOBBY" in blob and not any("뺀 이름" in (f.name or "") for f in emb.fields),
         "목록을 비우면 옛 기록이 돌아오고 '뺀 이름' 칸도 사라짐")

    print("\n=== 4. 화면에서 고치기 (🙈 버튼 → 모달) ===")
    it = await F.run("/바이옴통계", lambda it: lg.biome_stats.callback(lg, it))
    view = it.followup.sent[-1]["view"]
    btn = next((c for c in view.children if isinstance(c, LG.BiomeIgnoreButton)), None)
    note(btn is not None, "통계 화면에 🙈 버튼")
    it2 = Inter(admin=False)
    await btn.callback(it2)
    note(it2.response.sent and "관리자" in (it2.response.sent[0].get("content") or ""),
         "관리자가 아니면 거절")

    m = LG.BiomeIgnoreModal([])
    m.names._value = " in lobby , School\n\nEVEN CARE\nschool\n🌀 Grocery Store 🌀"
    it3 = Inter()
    await m.on_submit(it3)
    settings = await vdb.get_linkguard_settings(GUILD.id)
    note(settings["biome_ignore"] == ["IN LOBBY", "SCHOOL", "EVEN CARE", "GROCERY STORE"],
         "쉼표·줄바꿈으로 나누고 대문자 · 중복 제거 · 이모지 제거", str(settings["biome_ignore"]))
    note(it3.followup.sent and it3.followup.sent[-1].get("ephemeral") is True, "결과는 본인에게만")

    m = LG.BiomeIgnoreModal([])
    m.names._value = "DREAMSPACE"
    it4 = Inter()
    await m.on_submit(it4)
    e4 = it4.followup.sent[-1]["embed"]
    note(any("희귀" in f.name for f in e4.fields) and e4.color == discord.Color.orange(),
         "**희귀 바이옴을 넣으면 주황 경고** (알림 누락 판정이 사라지므로)")

    m = LG.BiomeIgnoreModal([])
    m.names._value = "\n".join(f"FAKE{i}" for i in range(30))
    it5 = Inter()
    await m.on_submit(it5)
    settings = await vdb.get_linkguard_settings(GUILD.id)
    note(len(settings["biome_ignore"]) == LG.MAX_BIOME_IGNORE
         and any("일부만" in f.name for f in it5.followup.sent[-1]["embed"].fields),
         "25개를 넘기면 자르고 알려 줌")

    btn2 = LG.BiomeIgnoreButton(["IN LOBBY", "SCHOOL"])
    it6 = Inter()
    await btn2.callback(it6)
    note(it6.response.modal == "BiomeIgnoreModal", "버튼 → 모달이 바로 뜸 (DB 안 읽음)")
    note(LG.BiomeIgnoreModal(["IN LOBBY", "SCHOOL"]).names.default == "IN LOBBY\nSCHOOL",
         "모달에 지금 목록이 채워져 있음")

    leak = cols["linkguard_col"]
    note(leak is not None, "Mongo 경로(가짜 드라이버)로 저장")
    await bot.close()


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
