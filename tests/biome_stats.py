"""바이옴 등장 통계.

지금까지 **어디에도 바이옴 이름이 남지 않았습니다.** `link_usage` 는 누가
눌렀는지만, `protected_links` 는 어떤 링크였는지만 압니다. 그래서
"이번 주에 DREAMSPACE 가 몇 번 떴나" 를 아무도 답할 수 없었습니다.

이 통계의 존재 이유는 숫자 구경이 아니라 **"희귀 바이옴이 떴는데 알림이
안 나간 경우"를 잡는 것**입니다. 그 검사가 이 스위트의 핵심입니다.
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

_mid = [9000]


def link(code):
    return f"https://www.roblox.com/games/1234567/Test?privateServerLinkCode={code}"


def macro(channel, title, url, author="Macro", footer="MultiScope v2.0.5"):
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    e = discord.Embed(title=title, description=url)
    e.set_author(name=author)
    e.set_footer(text=footer)
    m.embeds = [e]
    return m


async def main():
    import utils.verify_db as vdb

    cols = FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER
    print(f"가짜 컬렉션 장착: {len(cols)}개")

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    lg = bot.get_cog("LinkGuard")

    # ⚠️ load_extension 은 sys.modules 를 새 인스턴스로 덮어씁니다.
    LG = sys.modules["cogs.link_guard"]
    note(LG.__dict__ is lg._guard_message.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    ch = GUILD.channels[0]
    other = GUILD.channels[1]

    async def setup(**over):
        base = {
            "enabled": True, "channel_ids": [ch.id, other.id],
            "category_ids": [], "exception_channel_ids": [],
            "roblox_only": True, "relay_links": False, "log_channel_id": None,
            "notify_rules": [{"channel_ids": [ch.id], "category_ids": [],
                              "biomes": RARE, "mention": "everyone",
                              "role_ids": []}],
            "carry_embed_mentions": True,
            "notify_exclude": ["Biome End", "바이옴 종료"],
        }
        base.update(over)
        await vdb.save_linkguard_settings(GUILD.id, base)
        ch.sent.clear()
        other.sent.clear()
        # 구간마다 기록을 비웁니다. 안 비우면 앞 구간에서 남긴 '알림 안 나간'
        # 기록이 뒤 구간 판정에 섞여 **테스트가 이유 없이 실패**합니다.
        if getattr(vdb.biome_col, "docs", None) is not None:
            vdb.biome_col.docs.clear()

    async def run(msg):
        await lg.on_message(msg)
        await drain()

    print("=== 1. 제목에서 바이옴 이름 뽑기 (매크로 7종) ===")
    cases = [
        ("Biome Started - DREAMSPACE", "DREAMSPACE"),
        ("BIOME STARTED - HELL Found", "HELL"),
        ("BIOME  STARTED :: glitched_biome", "glitched_biome"),
        ("Biome Started - Cyber Space", "Cyber Space"),
        ("biome started - snowy", "snowy"),
        ("Biome Ended - DREAMSPACE", None),      # 종료는 '등장' 이 아닙니다
        ("BIOME ENDED - HELL Found", None),
        ("오늘의 공지", None),
        ("", None),
    ]
    for title, want in cases:
        m = type("M", (), {"embeds": [discord.Embed(title=title)]})()
        got = LG.detect_biome_name(m)
        note(got == want, f"{title[:32]!r:36} -> {want!r}", repr(got))

    print("")
    print("  ⚠️ 제목만 봅니다 — 본문·작성자·바닥글은 보지 않습니다")
    e = discord.Embed(title="공지입니다", description="Biome Started - DREAMSPACE")
    e.set_author(name="Biome Started - CYBERSPACE")
    e.set_footer(text="Biome Started - GLITCHED")
    m = type("M", (), {"embeds": [e]})()
    note(LG.detect_biome_name(m) is None,
         "제목 밖의 문구로는 기록되지 않음", repr(LG.detect_biome_name(m)))

    print("")
    print("=== 2. 보호할 때 기록이 남아야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("s1")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    note(len(rows) == 1, "한 건 기록됨", str(len(rows)))
    note(rows and rows[0].get("biome") == "DREAMSPACE", "바이옴 이름이 맞음",
         str(rows and rows[0].get("biome")))
    note(rows and rows[0].get("notified") is True,
         "알림이 나갔다고 기록됨", str(rows and rows[0].get("notified")))

    print("")
    print("=== 3. 같은 링크를 다시 올려도 1회로 세야 ===")
    # 매크로는 같은 링크를 상태 갱신마다 다시 올립니다(평균 145번).
    # 재게시마다 한 건씩 쌓으면 통계가 '등장 횟수' 가 아니라 '재게시 횟수'가 됩니다.
    for _ in range(5):
        await run(macro(ch, "Biome Started - DREAMSPACE", link("s1")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    same = [r for r in rows if r.get("biome") == "DREAMSPACE"]
    note(len(same) == 1, "여섯 번 올라와도 한 건", str(len(same)))
    note(same and same[0].get("reposts") == 6, "재게시 횟수는 따로 셈",
         str(same and same[0].get("reposts")))

    print("")
    print("  링크가 다르면 별개의 등장입니다")
    await run(macro(ch, "Biome Started - DREAMSPACE", link("s2")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    note(len([r for r in rows if r.get("biome") == "DREAMSPACE"]) == 2,
         "다른 개인서버는 2회로 셈")

    print("")
    print("=== 4. 종료 임베드는 기록하지 않아야 ===")
    await setup()
    ended = macro(ch, "Biome Ended - DREAMSPACE", link("e1"))
    await run(ended)
    rows = await vdb.get_biome_events(GUILD.id, 24)
    note(not [r for r in rows if "e1" in str(r.get("_id", ""))],
         "종료는 등장으로 세지 않음")

    print("")
    print("=== 5. 흔한 바이옴도 기록되지만 알림은 안 나감 ===")
    await setup()
    await run(macro(ch, "Biome Started - SNOWY", link("c1")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    snowy = [r for r in rows if r.get("biome") == "SNOWY"]
    note(len(snowy) == 1, "흔한 바이옴도 기록됨")
    note(snowy and snowy[0].get("notified") is False,
         "알림은 안 나갔다고 기록", str(snowy and snowy[0].get("notified")))

    print("")
    print("=== 6. ⚠️ 떴는데 알림이 안 나간 희귀 바이옴을 잡아야 ===")
    # 이 통계의 존재 이유입니다. 규칙 범위 밖 채널에서 희귀 바이옴이 뜨면
    # 보호는 되지만 아무도 모릅니다 — 화면이 그걸 짚어 줘야 합니다.
    await setup()
    await run(macro(other, "Biome Started - CYBERSPACE", link("x1")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    settings = await vdb.get_linkguard_settings(GUILD.id)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 24)
    blob = (embed.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in embed.fields)
    note("알림이 안 나간" in blob, "경고 칸이 뜸")
    note("CYBERSPACE" in blob, "어느 바이옴인지 지목함")
    note("권한" in blob or "규칙 범위" in blob, "무엇을 확인해야 하는지 알려줌")

    print("")
    print("  전부 알림이 나갔으면 초록으로 확인해 줍니다")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("y1")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 24)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("✅" in blob and "알림이 안 나간" not in blob,
         "모두 정상이면 경고 대신 확인 표시")

    print("")
    print("=== 7. 희귀 바이옴은 별표로 구분되어야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("r1")))
    await run(macro(ch, "Biome Started - SNOWY", link("r2")))
    rows = await vdb.get_biome_events(GUILD.id, 24)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 24)
    body = next((f.value for f in embed.fields if "등장" in (f.name or "")), "")
    note("⭐ **DREAMSPACE**" in body, "희귀는 ⭐", body[:60])
    note("• **SNOWY**" in body, "흔한 것은 •", body[:60])

    print("")
    print("=== 8. 기록이 없으면 '없음'을 설명해야 ===")
    # 빈 화면에 0 만 띄우면 "고장난 건가" 를 판단할 수 없습니다.
    embed = LG.build_biome_stats_embed(GUILD, [], settings, 24)
    note("기록된 바이옴이 없습니다" in (embed.description or ""), "빈 상태 안내")
    note("보호가 동작한 시점부터" in (embed.description or ""),
         "왜 비어 있을 수 있는지 설명함")

    print("")
    print("=== 9. 기간이 지난 기록은 빠져야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("old1")))
    rows_1h = await vdb.get_biome_events(GUILD.id, 1)
    note(len(rows_1h) >= 1, "방금 것은 1시간 안에 들어옴")

    old_key = vdb._biome_event_id(GUILD.id, "OLDBIOME", "ps:zzz")
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)
    await vdb._db(vdb.biome_col.update_one, {"_id": old_key},
                  {"$set": {"guild_id": str(GUILD.id), "biome": "OLDBIOME",
                            "updated": long_ago, "reposts": 1, "notified": True}},
                  upsert=True)
    rows_7d = await vdb.get_biome_events(GUILD.id, 24 * 7)
    rows_90d = await vdb.get_biome_events(GUILD.id, 24 * 90)
    note(not [r for r in rows_7d if r.get("biome") == "OLDBIOME"],
         "40일 전 기록은 7일 조회에 안 나옴")
    note([r for r in rows_90d if r.get("biome") == "OLDBIOME"],
         "90일 조회에는 나옴")

    print("")
    print("=== 10. 기록 실패가 보호를 멈추면 안 됩니다 ===")
    # 통계는 있으면 좋은 것이고 보호는 없으면 안 되는 것입니다.
    await setup()
    real = LG.record_biome_event

    async def boom(*a, **k):
        raise RuntimeError("일부러 낸 오류")

    # `_guard_message` 가 기록을 감싸 둔 try/except 를 직접 찌릅니다.
    # (Mongo 컬렉션을 깨뜨리면 데이터 계층이 JSON 폴백으로 넘어가면서
    #  "using local file" 을 찍는데, 실행기가 그걸 **폴백누출**로 셉니다.
    #  일부러 낸 것과 진짜 누출이 구분되지 않으므로 그 방법은 쓰지 않습니다.)
    LG.record_biome_event = boom
    try:
        await run(macro(ch, "Biome Started - DREAMSPACE", link("boom")))
        body = (ch.sent[-1].get("content") or "") if ch.sent else ""
        note(bool(ch.sent), "기록이 깨져도 보호 메시지는 나감")
        note("@everyone" in body, "알림도 그대로 나감", repr(body[:30]))
    finally:
        LG.record_biome_event = real

    rows = await vdb.get_biome_events(GUILD.id, 24)
    note(not [r for r in rows if "boom" in str(r.get("_id", ""))],
         "실패한 기록은 남지 않음 (있는 척하지 않습니다)")

    print("")
    print("=== 11. 명령어와 버튼이 실제로 동작해야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("ui1")))

    it = Inter()
    await lg.biome_stats.callback(lg, it)
    await drain()
    sent = (it.followup.sent or it.response.sent)
    note(sent, "/바이옴통계 가 응답함", F.outcome(it))
    note(sent and sent[0].get("ephemeral"), "ephemeral 로 응답")
    note(sent and sent[0].get("embed") is not None, "임베드를 보냄")

    for label, hours in LG.BIOME_PERIODS:
        it = Inter()
        btn = LG.BiomeStatsButton(label, hours)
        try:
            await btn.callback(it)
            await drain()
            got = it.followup.sent
            note(bool(got), f"[최근 {label}] 버튼이 응답함")
            note(got and got[0].get("ephemeral"), f"[최근 {label}] ephemeral")
        except Exception as e:
            import traceback
            note(False, f"[최근 {label}] 버튼 예외", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=3)

    view = LG.BiomeStatsView(24)
    actives = [b for b in view.children if getattr(b, "disabled", False)]
    note(len(actives) == 1, "보고 있는 기간 버튼만 비활성", str(len(actives)))

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
