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


def macro(channel, title, url, author="Macro", footer="MultiScope v2.0.5",
          body=None):
    """매크로 임베드 한 건. `body` 를 주면 **본문 앞에** 붙습니다.

    Coteab v2.2.0 · maxstellar v2.3 은 제목에 시각을 넣고 본문에
    `> ## Biome Started - SNOWY` 를 쓰기 때문에, 그 형태를 흉내 낼 수
    있어야 합니다.
    """
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    e = discord.Embed(title=title,
                      description=(body + "\n" + url) if body else url)
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
        ("BIOME  STARTED :: glitched_biome", "GLITCHED_BIOME"),
        ("Biome Started - Cyber Space", "CYBER SPACE"),
        ("biome started - snowy", "SNOWY"),
        ("Biome Ended - DREAMSPACE", None),      # 종료는 '등장' 이 아닙니다
        ("BIOME ENDED - HELL Found", None),
        ("오늘의 공지", None),
        ("", None),
        # ── 형식 ②: 이름이 **앞**에 오는 매크로 (MultiScope v0.9.8) ──
        # 이 형식을 못 읽던 동안 `BLOOD RAIN` 은 통계에 아예 없었습니다.
        ("🌀 WINDY Biome Started", "WINDY"),
        ("🩸 BLOOD RAIN Biome Started", "BLOOD RAIN"),
        ("🎃 PUMPKIN MOON Biome Started", "PUMPKIN MOON"),
        ("🪦 GRAVEYARD Biome Started", "GRAVEYARD"),
        ("SNOWY Biome Started", "SNOWY"),
        ("🔥 HELL Biome Ended", None),   # 이 형식도 종료는 세지 않습니다
        ("❄️ SNOWY Biome Ended", None),
        # 매크로의 다른 알림이 바이옴으로 둔갑하면 안 됩니다
        ("MultiScope Started", None),
        ("MultiScope Stopped", None),
        (":green_circle: Macro Started!", None),
        (":joystick: Biome Randomizer was used.", None),
        ("Aura Equipped - Raft", None),
        ("MARI HAS ARRIVED! Found", None),
        ("== 🌟 Macro Status 🌟 ==", None),
    ]
    for title, want in cases:
        m = type("M", (), {"embeds": [discord.Embed(title=title)]})()
        got = LG.detect_biome_name(m)
        note(got == want, f"{title[:32]!r:36} -> {want!r}", repr(got))

    print("")
    print("  ⚠️ 대소문자가 달라도 **같은 바이옴**이어야 합니다")
    # 매크로마다 제목 대소문자가 다릅니다. 실측으로 16종 중 12종이
    # 갈라져 54,975회 중 1,167회가 별도 줄로 새어 나갔습니다.
    for a_, b_ in [("Biome Started - Rainy", "Biome Started - RAINY"),
                   ("Biome Started - Blazing Sun", "BIOME STARTED - BLAZING SUN"),
                   ("Biome Started - Windy", "biome started - windy")]:
        ma = type("M", (), {"embeds": [discord.Embed(title=a_)]})()
        mb = type("M", (), {"embeds": [discord.Embed(title=b_)]})()
        ga, gb = LG.detect_biome_name(ma), LG.detect_biome_name(mb)
        note(ga == gb and ga is not None,
             f"{a_[16:30]!r} 와 {b_[16:30]!r} 가 같은 이름", f"{ga!r} / {gb!r}")
    m = type("M", (), {"embeds": [discord.Embed(title="Biome Started - Blazing Sun")]})()
    note(LG.detect_biome_name(m) == "BLAZING SUN",
         "**공백은 남깁니다** (한 단어가 되면 안 됩니다)",
         repr(LG.detect_biome_name(m)))

    print("")
    print("  ⚠️ 이미 쌓인 소문자 기록도 읽을 때 합쳐야 합니다")
    # 판정만 고치면 90일 TTL 이 지날 때까지 화면이 갈라져 보입니다.
    mixed = [
        {"biome": "RAINY", "day": "2026-09-16", "count": 100,
         "notified_count": 0, "links": ["a"]},
        {"biome": "Rainy", "day": "2026-09-16", "count": 30,
         "notified_count": 0, "links": ["b"]},
        {"biome": "rainy", "day": "2026-09-15", "count": 7,
         "notified_count": 0, "links": ["c"]},
    ]
    agg = LG.summarize_biomes(mixed, 30)
    note(list(agg) == ["RAINY"], "한 줄로 합쳐짐", str(list(agg)))
    note(agg["RAINY"]["total"] == 137, "누적이 100+30+7", str(agg["RAINY"]["total"]))
    note(len(agg["RAINY"]["servers"]) == 3, "서버 수도 합산",
         str(len(agg["RAINY"]["servers"])))

    print("")
    print("  ⚠️ 같은 바이옴이 **형식 차이로 갈라지면** 안 됩니다")
    # 한 서버에 매크로가 여러 종류 돌아갑니다. `WINDY` 와 `🌀 WINDY` 가
    # 서로 다른 칸으로 세어지면 숫자가 절반씩 쪼개져 둘 다 틀립니다.
    for a, b in [("Biome Started - WINDY", "🌀 WINDY Biome Started"),
                 ("BIOME STARTED - HELL Found", "🔥 HELL Biome Started"),
                 ("Biome Started - SNOWY", "❄️ SNOWY Biome Started")]:
        ma = type("M", (), {"embeds": [discord.Embed(title=a)]})()
        mb = type("M", (), {"embeds": [discord.Embed(title=b)]})()
        ga, gb = LG.detect_biome_name(ma), LG.detect_biome_name(mb)
        note(ga == gb and ga is not None,
             f"{a[:28]!r} 와 {b[:28]!r} 가 같은 이름", f"{ga!r} / {gb!r}")

    print("")
    print("  ⚠️ 작성자·바닥글은 **보지 않습니다** (매크로 이름이 들어갑니다)")
    # 섞으면 이름에 바이옴이 든 매크로의 모든 메시지가 바이옴이 됩니다.
    e = discord.Embed(title="공지입니다")
    e.set_author(name="Biome Started - CYBERSPACE")
    e.set_footer(text="Biome Started - GLITCHED")
    m = type("M", (), {"embeds": [e]})()
    note(LG.detect_biome_name(m) is None,
         "작성자·바닥글의 문구로는 기록되지 않음", repr(LG.detect_biome_name(m)))

    print("")
    print("  ── 형식 ③: 제목은 시각, **본문**에 이름 (Coteab · maxstellar) ──")
    # 이 형식을 못 읽던 동안 두 매크로의 바이옴은 한 건도 세어지지 않았습니다.
    body_cases = [
        ("[12:10:40]", "> ## Biome Started - SNOWY", "SNOWY"),
        ("<t:1788524808:F> (<t:1788524808:R>)",
         "> ## Biome Started - WINDY \n> ### **[Join Server](https://x)**", "WINDY"),
        ("[17:56:05]", "> ## Biome Ended - WINDY\n`🔒 인증된 유저만`", None),
        ("[16:41:59]", "> ## Biome Started - BLAZING SUN\n`🔒 인증된 유저만`",
         "BLAZING SUN"),
        # 본문에 바이옴 얘기가 없으면 여전히 None
        ("[09:00:00]", "**Account:** `Someone`\n**Status:** Active ✅", None),
    ]
    for title, body, want in body_cases:
        e = discord.Embed(title=title, description=body)
        e.set_footer(text="maxstellar's Biome Macro | v2.3")
        m = type("M", (), {"embeds": [e]})()
        got = LG.detect_biome_name(m)
        note(got == want, f"{title[:22]!r:26} + 본문 -> {want!r}", repr(got))

    print("")
    print("  ⚠️ 제목이 우선입니다 (본문에 다른 문장이 섞일 수 있습니다)")
    e = discord.Embed(title="Biome Started - DREAMSPACE",
                      description="> ## Biome Started - SNOWY")
    m = type("M", (), {"embeds": [e]})()
    note(LG.detect_biome_name(m) == "DREAMSPACE",
         "제목의 이름을 씁니다", repr(LG.detect_biome_name(m)))

    print("")
    print("=== 2. 보호할 때 기록이 남아야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("s1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    note(len(rows) == 1, "버킷 한 건", str(len(rows)))
    note(rows and rows[0].get("biome") == "DREAMSPACE", "바이옴 이름이 맞음",
         str(rows and rows[0].get("biome")))
    note(rows and rows[0].get("count") == 1, "1회로 셈", str(rows and rows[0].get("count")))
    note(rows and rows[0].get("notified_count") == 1,
         "알림 나간 횟수도 셈", str(rows and rows[0].get("notified_count")))

    print("")
    print("=== 3. ⚠️ 같은 링크라도 시작 임베드마다 세야 ===")
    # 개인서버 링크는 **매 임베드마다 바꿀 수 없습니다.** 링크로 묶으면
    # 같은 서버에서 나중에 다시 뜬 바이옴이 통째로 묻힙니다.
    # (실측: 같은 링크+바이옴이 최대 175번까지 다시 올라옵니다)
    for _ in range(5):
        await run(macro(ch, "Biome Started - DREAMSPACE", link("s1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    same = [r for r in rows if r.get("biome") == "DREAMSPACE"]
    note(len(same) == 1, "버킷은 바이옴×날짜로 하나", str(len(same)))
    note(same and same[0].get("count") == 6, "여섯 번 올라오면 6회",
         str(same and same[0].get("count")))

    print("")
    print("  고유 서버 수는 따로 셉니다")
    await run(macro(ch, "Biome Started - DREAMSPACE", link("s2")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    same = [r for r in rows if r.get("biome") == "DREAMSPACE"]
    note(same and same[0].get("count") == 7, "7회", str(same and same[0].get("count")))
    note(same and len(same[0].get("links") or []) == 2, "서버는 2곳",
         str(same and same[0].get("links")))

    print("")
    print("=== 4. 종료 임베드는 기록하지 않아야 ===")
    await setup()
    ended = macro(ch, "Biome Ended - DREAMSPACE", link("e1"))
    await run(ended)
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    note(not rows, "종료는 등장으로 세지 않음", str(len(rows)))

    print("")
    print("=== 5. 흔한 바이옴도 기록되지만 알림은 안 나감 ===")
    await setup()
    await run(macro(ch, "Biome Started - SNOWY", link("c1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    snowy = [r for r in rows if r.get("biome") == "SNOWY"]
    note(len(snowy) == 1, "흔한 바이옴도 기록됨")
    note(snowy and snowy[0].get("notified_count") == 0,
         "알림 나간 횟수는 0", str(snowy and snowy[0].get("notified_count")))

    print("")
    print("=== 6. ⚠️ 떴는데 알림이 안 나간 희귀 바이옴을 잡아야 ===")
    # 이 통계의 존재 이유입니다. 규칙 범위 밖 채널에서 희귀 바이옴이 뜨면
    # 보호는 되지만 아무도 모릅니다 — 화면이 그걸 짚어 줘야 합니다.
    await setup()
    await run(macro(other, "Biome Started - CYBERSPACE", link("x1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    settings = await vdb.get_linkguard_settings(GUILD.id)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    blob = (embed.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in embed.fields)
    note("알림이 안 나간" in blob, "경고 칸이 뜸")
    note("CYBERSPACE" in blob, "어느 바이옴인지 지목함")
    note("권한" in blob or "규칙 범위" in blob, "무엇을 확인해야 하는지 알려줌")

    print("")
    print("  전부 알림이 나갔으면 초록으로 확인해 줍니다")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("y1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("✅" in blob and "알림이 안 나간" not in blob,
         "모두 정상이면 경고 대신 확인 표시")

    print("")
    print("=== 7. 희귀 바이옴은 별도 칸으로 구분되어야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("r1")))
    await run(macro(ch, "Biome Started - SNOWY", link("r2")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    rare_f = next((f.value for f in embed.fields if "희귀" in (f.name or "")), "")
    other_f = next((f.value for f in embed.fields if "그 밖" in (f.name or "")), "")
    note("DREAMSPACE" in rare_f, "희귀는 희귀 칸에", rare_f[:50])
    note("SNOWY" in other_f, "흔한 것은 그 밖의 칸에", other_f[:50])
    note("SNOWY" not in rare_f, "흔한 것이 희귀 칸에 섞이지 않음")

    print("")
    print("  ⚠️ 규칙에 있는 희귀 바이옴은 0회여도 보여야 합니다")
    # 빈칸이면 안 뜬 건지 고장난 건지 판단할 수 없습니다.
    note("CYBERSPACE" in rare_f, "0회인 희귀 바이옴도 표시", rare_f[:70])

    print("")
    print("  ⚠️ '외 N종' 으로 접지 않고 전부 보여야 합니다")
    await setup()
    for i in range(14):
        await run(macro(ch, f"Biome Started - BIOME{i:02}", link(f"m{i}")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    missing = [f"BIOME{i:02}" for i in range(14) if f"BIOME{i:02}" not in blob]
    note(not missing, "14종 전부 표시됨", f"누락 {missing}")
    note("외 " not in blob, "'외 N종' 으로 접지 않음")

    print("")
    print("  ⚠️ 칸 하나가 1024자를 넘으면 나눠 담아야 합니다 (넘으면 전송 실패)")
    await setup()
    for i in range(40):
        await run(macro(ch, f"Biome Started - VERYLONGBIOMENAME{i:03}", link(f"L{i}")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    over = [f.name for f in embed.fields if len(f.value or "") > 1024]
    note(not over, "1024자를 넘는 칸이 없음", str(over))
    note(len(embed) <= 6000, "임베드 전체가 6000자 이내", str(len(embed)))
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("VERYLONGBIOMENAME039" in blob, "마지막 바이옴까지 살아 있음")

    print("")
    print("  누적(종합) 등장 횟수가 보여야 합니다")
    await setup()
    for _ in range(3):
        await run(macro(ch, "Biome Started - DREAMSPACE", link("t1")))
    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    blob = (embed.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in embed.fields)
    note("누적" in blob, "누적 표시가 있음")
    note("3회" in blob or "**3**" in blob, "같은 링크 3번이 3회로 셈", blob[:90])

    print("=== 7-2. ⚠️ 카운트 대상이 스스로 유지되어야 ===")
    # 게임에 바이옴이 추가·삭제돼도 사람이 목록을 손볼 일이 없어야 합니다.
    await setup()

    async def put(biome, days_ago, count=1, notified=False):
        day = (datetime.datetime.now(vdb.KST)
               - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")
        await vdb._db(vdb.biome_col.update_one,
                      {"_id": vdb._biome_bucket_id(GUILD.id, biome, day)},
                      {"$set": {"guild_id": str(GUILD.id), "kind": "day",
                                "biome": biome, "day": day,
                                "updated": datetime.datetime.now(datetime.timezone.utc)},
                       "$inc": {"count": count,
                                "notified_count": count if notified else 0}},
                      upsert=True)

    await put("현역바이옴", 2, 5)
    await put("죽은바이옴", 10, 9)        # 10일 전 이후로 소식 없음
    await put("경계바이옴", 6, 3)         # 아직 7일 안쪽
    await put("새바이옴", 0, 2)           # 오늘 처음

    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    agg = LG.summarize_biomes(rows, 30)
    live, dropped = LG.active_biomes(agg)
    note("현역바이옴" in live, "최근에 뜬 것은 대상에 남음")
    note("경계바이옴" in live, "6일 전은 아직 대상 (경계)")
    note("죽은바이옴" in dropped, "7일 넘게 안 뜬 것은 자동 제외", str(dropped))
    note("새바이옴" in live, "새로 감지된 바이옴은 자동 추가됨")

    print("")
    print("  화면에서도 빠지고, **왜 빠졌는지** 알려야 합니다")
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("현역바이옴" in blob, "현역은 표시됨")
    note("새바이옴" in blob, "신규도 표시됨")
    drop_field = next((f.value for f in embed.fields if "빠진" in (f.name or "")), "")
    note(bool(drop_field), "빠진 바이옴 칸이 있음 (조용히 지우지 않음)")
    note("죽은바이옴" in drop_field, "무엇이 빠졌는지 이름을 밝힘", drop_field[:70])
    note("다시 뜨면" in drop_field, "돌아온다는 것을 알려줌")

    print("")
    print("  화면에 NEW 같은 표기가 붙지 않아야 합니다")
    # 기록이 이제 막 쌓이기 시작한 시점에는 **전부** 신규라 표시가 도배됩니다.
    note("🆕" not in blob and "NEW" not in blob, "신규 표기가 없음")

    print("")
    print("  ⚠️ 규칙에 적어 둔 희귀 바이옴은 오래 쉬어도 남아야 합니다")
    # 설정해 둔 것이 안 뜨고 있다는 사실 자체가 봐야 할 정보입니다.
    await setup()
    await put("DREAMSPACE", 60, 1)        # 두 달 전 이후 소식 없음
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1)
    rare_f = next((f.value for f in embed.fields if "희귀" in (f.name or "")), "")
    for name in RARE:
        note(name in rare_f, f"{name} 은 활동이 없어도 계속 표시", rare_f[:60])

    print("")
    print("  다시 뜨면 자동으로 돌아와야 합니다")
    await setup()
    await put("부활바이옴", 20, 3)
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    live, dropped = LG.active_biomes(LG.summarize_biomes(rows, 30))
    note("부활바이옴" in dropped, "일단 빠져 있음")
    await put("부활바이옴", 0, 1)
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    agg2 = LG.summarize_biomes(rows, 30)
    live, dropped = LG.active_biomes(agg2)
    note("부활바이옴" in live, "다시 뜨자 자동으로 돌아옴")
    note(agg2["부활바이옴"]["total"] == 4, "누적은 옛 기록까지 합산",
         str(agg2["부활바이옴"]["total"]))

    print("")
    print("=== 7-3. ⚠️ 못 읽은 제목은 **드러나야** 합니다 ===")
    # 매크로는 판올림하면서 제목 배치를 바꿉니다. 못 읽은 것을 조용히 버리면
    # 그 매크로의 바이옴이 통계에서 통째로 빠지는데, **빠졌다는 사실조차
    # 아무도 모릅니다.** 그래서 개수를 세고 표본 제목을 남깁니다.
    await setup()
    LG._unparsed_seen.clear()

    await run(macro(ch, "Biome Started - DREAMSPACE", link("u1")))
    await run(macro(ch, "🌀 WINDY Biome Started", link("u2")))
    await run(macro(ch, "[12:10:40]", link("u3"),
                    body="> ## Biome Started - SNOWY"))
    up = await vdb.get_biome_unparsed(GUILD.id, 7)
    note(up["count"] == 0, "읽히는 형식은 미인식으로 세지 않음", str(up))

    print("")
    print("  ⚠️ **종료 알림은 못 읽은 것이 아닙니다** (읽고 나서 안 세는 것)")
    # 이걸 세면 바이옴이 뜨고 끝날 때마다 경고가 올라 늘 켜져 있게 됩니다.
    await run(macro(ch, "[17:56:05]", link("u4"),
                    body="> ## Biome Ended - WINDY"))
    await run(macro(ch, "🔥 HELL Biome Ended", link("u5")))
    up = await vdb.get_biome_unparsed(GUILD.id, 7)
    note(up["count"] == 0, "종료 알림은 경고하지 않음", str(up))

    print("")
    print("  ⚠️ 바이옴 알림이 **아닌 것**도 세지 않습니다")
    # 매크로 시작/정지·NPC 등장까지 세면 경고가 소음이 됩니다(실측 25%).
    for t in ("MultiScope Started", "MultiScope Stopped",
              "MARI HAS ARRIVED! Found", "Aura Equipped - Raft"):
        await run(macro(ch, t, link("n" + t[:3])))
    up = await vdb.get_biome_unparsed(GUILD.id, 7)
    note(up["count"] == 0, "바이옴 얘기가 없는 알림은 조용함", str(up))

    print("")
    print("  모르는 형식이 들어오면 **그때는 반드시** 알려야 합니다")
    await run(macro(ch, "[06:24:02]", link("u6"),
                    body="> ## Biome Spotted!! WINDY"))
    await run(macro(ch, "[06:24:02]", link("u7"),
                    body="> ## Biome Spotted!! WINDY"))
    await run(macro(ch, "🌟 새 형식 Biome Appeared", link("u8")))
    up = await vdb.get_biome_unparsed(GUILD.id, 7)
    note(up["count"] == 3, "못 읽은 것은 빠짐없이 셈", str(up["count"]))
    note("[06:24:02]" in up["titles"], "표본 제목이 남음", str(up["titles"]))
    note(len(up["titles"]) == 2, "같은 제목은 한 번만", str(up["titles"]))

    print("")
    print("  집계에 섞이면 안 됩니다 (`kind` 가 다릅니다)")
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    note(all(r.get("kind") == "day" for r in rows),
         "버킷 조회에 미인식 문서가 섞이지 않음", str([r.get("kind") for r in rows]))
    agg = LG.summarize_biomes(rows, 30)
    note("?" not in agg, "이름 모르는 칸이 통계에 생기지 않음", str(list(agg)))

    print("")
    print("  화면이 **그만큼 빠졌다**고 말해야 합니다")
    embed = LG.build_biome_stats_embed(GUILD, rows, settings, 1, up)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("못 읽은" in blob, "미인식 칸이 보임")
    note("3" in blob and "빠져" in blob, "몇 건이 빠졌는지 밝힘")
    note("[06:24:02]" in blob, "어떤 제목인지 보여 줌")

    print("")
    print("  미인식이 없으면 그 칸도 없어야 합니다 (늘 켜진 경고는 무시됩니다)")
    embed2 = LG.build_biome_stats_embed(GUILD, rows, settings, 1,
                                        {"count": 0, "titles": []})
    blob2 = " ".join((f.name or "") + (f.value or "") for f in embed2.fields)
    note("못 읽은" not in blob2, "0건이면 칸이 뜨지 않음")

    print("")
    print("  표본은 프로세스마다 상한이 있어야 합니다 (배열 무한 증가 방지)")
    LG._unparsed_seen.clear()
    taken = sum(1 for i in range(50)
                if LG._take_unparsed_sample(GUILD.id, f"제목{i}"))
    note(taken == LG.UNPARSED_SAMPLE_CAP,
         f"표본은 {LG.UNPARSED_SAMPLE_CAP}개까지만", str(taken))
    note(not LG._take_unparsed_sample(GUILD.id, "제목0"), "같은 제목은 다시 안 보냄")

    print("")
    print("=== 8. 기록이 없으면 '없음'을 설명해야 ===")
    # 빈 화면에 0 만 띄우면 "고장난 건가" 를 판단할 수 없습니다.
    embed = LG.build_biome_stats_embed(GUILD, [], settings, 1)
    note("기록된 바이옴이 없습니다" in (embed.description or ""), "빈 상태 안내")
    note("보호가 동작한 시점부터" in (embed.description or ""),
         "왜 비어 있을 수 있는지 설명함")

    print("")
    print("=== 9. 기간이 지난 기록은 빠져야 ===")
    await setup()
    await run(macro(ch, "Biome Started - DREAMSPACE", link("old1")))
    note(len(await vdb.get_biome_buckets(GUILD.id, 1)) >= 1, "오늘 것은 1일 조회에 들어옴")

    old_day = (datetime.datetime.now(vdb.KST) - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
    old_key = vdb._biome_bucket_id(GUILD.id, "OLDBIOME", old_day)
    await vdb._db(vdb.biome_col.update_one, {"_id": old_key},
                  {"$set": {"guild_id": str(GUILD.id), "kind": "day",
                            "biome": "OLDBIOME", "day": old_day, "count": 1,
                            "notified_count": 1,
                            "updated": datetime.datetime.now(datetime.timezone.utc)}},
                  upsert=True)
    rows_7d = await vdb.get_biome_buckets(GUILD.id, 7)
    rows_90d = await vdb.get_biome_buckets(GUILD.id, 90)
    note(not [r for r in rows_7d if r.get("biome") == "OLDBIOME"],
         "40일 전 버킷은 7일 조회에 안 나옴")
    note([r for r in rows_90d if r.get("biome") == "OLDBIOME"],
         "90일 조회에는 나옴")

    print("")
    print("  옛 형식(링크별 문서)이 섞여 있어도 집계에 끼면 안 됩니다")
    # 기준이 바뀌었으므로 옛 문서를 세면 숫자가 조용히 틀어집니다.
    await vdb._db(vdb.biome_col.update_one, {"_id": f"{GUILD.id}:LEGACY:ps:old"},
                  {"$set": {"guild_id": str(GUILD.id), "biome": "LEGACY",
                            "reposts": 99,
                            "updated": datetime.datetime.now(datetime.timezone.utc)}},
                  upsert=True)
    rows = await vdb.get_biome_buckets(GUILD.id, 90)
    note(not [r for r in rows if r.get("biome") == "LEGACY"],
         "kind 가 없는 옛 문서는 제외됨")

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

    rows = await vdb.get_biome_buckets(GUILD.id, 30)
    note(not rows, "실패한 기록은 남지 않음 (있는 척하지 않습니다)", str(len(rows)))

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

    for label, days in LG.BIOME_PERIODS:
        it = Inter()
        btn = LG.BiomeStatsButton(label, days)
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

    view = LG.BiomeStatsView(1)
    actives = [b for b in view.children if getattr(b, "disabled", False)]
    note(len(actives) == 1, "보고 있는 기간 버튼만 비활성", str(len(actives)))

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
