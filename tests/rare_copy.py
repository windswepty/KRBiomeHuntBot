"""희귀 바이옴 바로 들어가기 채널.

희귀 바이옴이 뜨면 **원본 개인서버 주소를 그대로** 관리자 채널에 복사합니다.
매크로 채널은 보호된 버튼이라 한 번 더 눌러야 하는데, 희귀 바이옴은 길어야
10분이라 그 한 번이 아쉽습니다.

여기서 지켜야 할 것:

  1. **도배를 막아야 합니다.** 매크로는 같은 링크를 상태 갱신마다 다시
     올립니다(실측 평균 145번, 최다 527번). 그대로 두면 이 채널이 같은
     링크로 도배돼 정작 새로 뜬 바이옴이 묻힙니다.
  2. **키는 (링크 + 바이옴)** 입니다. 링크만으로 막으면 같은 서버에서
     DREAMSPACE 뒤에 CYBERSPACE 가 떠도 통째로 묻힙니다.
  3. **희귀한 것만** 갑니다. 흔한 바이옴까지 오면 이 채널을 안 보게 됩니다.
  4. **복사가 실패해도 보호는 계속돼야** 합니다.
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

RARE = ["DREAMSPACE", "CYBERSPACE", "GLITCHED"]
_mid = [5000]


def link(code):
    return f"https://www.roblox.com/games/1/T?privateServerLinkCode={code}"


def macro(channel, biome, url):
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    e = discord.Embed(title=f"Biome Started - {biome}", description=url)
    e.set_footer(text="MultiScope v2.0.5")
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

    LG = sys.modules["cogs.link_guard"]
    note(LG.__dict__ is lg._guard_message.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    macro_ch = GUILD.channels[0]
    copy_ch = GUILD.channels[1]

    async def setup(**over):
        base = {"enabled": True, "channel_ids": [macro_ch.id],
                "category_ids": [], "exception_channel_ids": [],
                "no_guard_channel_ids": [], "roblox_only": True,
                "relay_links": False, "log_channel_id": None,
                "rare_copy_channel_id": copy_ch.id,
                # ⚠️ `save_linkguard_settings` 는 **병합**이라, 안 적으면
                #    앞 구간에서 켜 둔 값이 그대로 남습니다.
                "rare_copy_dedup_minutes": 0,
                "notify_rules": [{"channel_ids": [macro_ch.id],
                                  "category_ids": [], "biomes": RARE,
                                  "mention": "everyone", "role_ids": []}],
                "carry_embed_mentions": True,
                "notify_exclude": ["Biome End"]}
        base.update(over)
        await vdb.save_linkguard_settings(GUILD.id, base)
        for c in GUILD.channels:
            c.sent.clear()
        LG._rare_copied.clear()
        return await vdb.get_linkguard_settings(GUILD.id)

    async def run(msg):
        await lg.on_message(msg)
        await drain()

    def copied():
        return [s for s in copy_ch.sent if s.get("embed") is not None]

    print("=== 1. 희귀 바이옴이면 원본 주소가 복사돼야 ===")
    await setup()
    await run(macro(macro_ch, "DREAMSPACE", link("aaa111")))
    got = copied()
    note(len(got) == 1, "복사 채널에 한 건", str(len(got)))
    emb = got[0]["embed"]
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields)
    note("DREAMSPACE" in (emb.title or ""), "바이옴 이름이 제목에", emb.title)
    note("aaa111" in blob, "**원본 주소가 그대로** 들어감")
    note("바로 들어가기" in blob, "바로 누를 수 있게 안내")
    note(macro_ch.mention in blob or "잡담" in blob, "어느 채널에서 왔는지")

    print("")
    print("  제목은 이모지 없이 '<이름> BIOME' 이어야 합니다")
    note(emb.title == "DREAMSPACE BIOME", "이름 뒤에 BIOME", emb.title)
    note(emb.title.isascii(), "제목에 이모지가 남지 않음", emb.title)
    # ⚠️ 이름에 이미 BIOME 이 붙어 오는 매크로가 있으면 두 번 붙습니다.
    note(LG.rare_copy_title("GLITCHED BIOME") == "GLITCHED BIOME",
         "BIOME 이 이미 있으면 두 번 붙이지 않음",
         LG.rare_copy_title("GLITCHED BIOME"))
    note("10분" not in (emb.footer.text or ""),
         "'길어야 10분' 문구가 바닥글에 없음", emb.footer.text)

    print("")
    print("  ⚠️ 이 채널은 희귀만 받으므로 @everyone 이 함께 가야 합니다")
    note("@everyone" in (got[0].get("content") or ""),
         "본문에 @everyone", repr(got[0].get("content")))
    am = got[0].get("allowed_mentions")
    note(am is not None, "allowed_mentions 를 명시함", str(am))
    note(am is not None and am.everyone, "전원 호출이 실제로 켜져 있음", str(am))
    # 원본 임베드의 제작자 유저 멘션까지 울리면 그 사람에게 매번 갑니다.
    note(am is not None and not am.roles and not am.users,
         "역할·유저는 울리지 않음", str(am))

    print("")
    print("  원래 채널의 보호는 그대로여야 합니다")
    note(macro_ch.sent, "매크로 채널에는 보호 메시지가 나감",
         str(len(macro_ch.sent)))

    print("")
    print("=== 2. ⚠️ 흔한 바이옴은 복사하지 않아야 ===")
    # 흔한 것까지 오면 이 채널을 안 보게 됩니다.
    await setup()
    for b in ("SNOWY", "WINDY", "BLAZING SUN", "NORMAL"):
        await run(macro(macro_ch, b, link("common" + b[:3])))
    note(not copied(), "흔한 바이옴 4종 모두 복사 안 됨", str(len(copied())))

    print("")
    print("  희귀 3종은 전부 가야 합니다")
    await setup()
    for i, b in enumerate(RARE):
        await run(macro(macro_ch, b, link(f"rare{i}")))
    note(len(copied()) == 3, "3건 전부 복사", str(len(copied())))

    print("")
    print("=== 3. 기본은 **전부 보내기** ===")
    # 실측(2026-09-16): 희귀 바이옴은 서버당 딱 1번만 올라옵니다
    # (합계 5건 / 고유 서버 5곳). 같은 링크가 수백 번 오는 것은 흔한
    # 바이옴 얘기였습니다(NORMAL 123.6번). 그래서 막지 않습니다.
    await setup()
    note(int((await vdb.get_linkguard_settings(GUILD.id))
             .get("rare_copy_dedup_minutes", -1)) == 0,
         "기본값이 0(끔)")
    for _ in range(5):
        await run(macro(macro_ch, "DREAMSPACE", link("same1")))
    note(len(copied()) == 5, "같은 링크라도 올라온 만큼 전부 감",
         str(len(copied())))

    print("")
    print("=== 3-2. 켜면 다시 막아야 (도배가 생겼을 때의 탈출구) ===")
    await setup(rare_copy_dedup_minutes=8)
    for _ in range(30):
        await run(macro(macro_ch, "DREAMSPACE", link("same2")))
    note(len(copied()) == 1, "30번 올라와도 한 줄만", str(len(copied())))

    print("")
    print("  ⚠️ **같은 서버라도 바이옴이 바뀌면 가야** 합니다")
    # 링크만으로 막으면 이게 묻힙니다 — 알림 쿨타임을 걷어낸 것과 같은 이유.
    await run(macro(macro_ch, "CYBERSPACE", link("same2")))
    note(len(copied()) == 2, "같은 링크 + 다른 바이옴은 감", str(len(copied())))
    await run(macro(macro_ch, "GLITCHED", link("same2")))
    note(len(copied()) == 3, "세 번째 바이옴도 감", str(len(copied())))

    print("")
    print("  다른 서버에서 뜬 같은 바이옴도 따로 가야 합니다")
    await setup()
    for i in range(4):
        await run(macro(macro_ch, "DREAMSPACE", link(f"server{i}")))
    note(len(copied()) == 4, "서버 4곳이면 4줄", str(len(copied())))

    print("")
    print("  창이 지나면 다시 보내야 합니다 (같은 서버에 또 뜬 것)")
    LG._rare_copied.clear()
    W = 8 * 60
    ok1 = LG.should_copy_rare(link("w"), "DREAMSPACE", W, now=0.0)
    ok2 = LG.should_copy_rare(link("w"), "DREAMSPACE", W, now=10.0)
    ok3 = LG.should_copy_rare(link("w"), "DREAMSPACE", W, now=W + 1.0)
    note(ok1 and not ok2 and ok3,
         f"0초 통과 · 10초 차단 · {W}초 뒤 통과", f"{ok1}/{ok2}/{ok3}")
    note(max(LG.RARE_COPY_WINDOW_CHOICES) * 60 < 10 * 60,
         "고를 수 있는 창이 전부 바이옴 수명(10분)보다 짧음",
         str(LG.RARE_COPY_WINDOW_CHOICES))

    print("")
    print("  창이 0 이면 무조건 보내야 합니다")
    LG._rare_copied.clear()
    always = [LG.should_copy_rare(link("w"), "DREAMSPACE", 0, now=float(i))
              for i in range(5)]
    note(all(always), "0 이면 5회 전부 통과", str(always))
    note(not LG._rare_copied, "기억조차 남기지 않음", str(len(LG._rare_copied)))

    print("")
    print("  대소문자·공백이 달라도 같은 바이옴으로 봐야 합니다")
    LG._rare_copied.clear()
    a = LG.should_copy_rare(link("z"), "DREAMSPACE", W, now=0.0)
    b = LG.should_copy_rare(link("z"), "dream space", W, now=1.0)
    note(a and not b, "`DREAMSPACE` 와 `dream space` 를 같게 봄", f"{a}/{b}")

    print("")
    print("  기억이 무한히 늘지 않아야 합니다")
    LG._rare_copied.clear()
    for i in range(2600):
        LG.should_copy_rare(link(f"x{i}"), "DREAMSPACE", W,
                            now=float(i) * (W + 1))
    note(len(LG._rare_copied) < 2600, "오래된 것은 버려짐",
         f"{len(LG._rare_copied)}개")

    print("")
    print("=== 4. 채널을 안 정했으면 아무 일도 없어야 ===")
    await setup(rare_copy_channel_id=None)
    await run(macro(macro_ch, "DREAMSPACE", link("off1")))
    note(not copied(), "복사 안 함", str(len(copied())))
    note(macro_ch.sent, "보호는 그대로")

    print("")
    print("  알림 규칙이 없으면 '희귀'를 알 수 없으니 복사하지 않습니다")
    await setup(notify_rules=[])
    await run(macro(macro_ch, "DREAMSPACE", link("norule")))
    note(not copied(), "규칙이 없으면 복사 안 함", str(len(copied())))

    print("")
    print("=== 5. ⚠️ 복사가 실패해도 보호는 계속돼야 ===")
    # 편의 기능이고, 보호는 없으면 안 되는 것입니다.
    await setup()
    # ⚠️ **클래스가 아니라 인스턴스만** 바꿉니다. 두 채널이 같은 클래스라
    #    `type(...).send` 를 건드리면 매크로 채널의 전송까지 망가져서,
    #    '복사 실패' 가 아니라 '전부 실패' 를 시험하게 됩니다.
    async def boom(*a, **k):
        raise RuntimeError("일부러 낸 전송 실패")

    copy_ch.send = boom
    try:
        m = macro(macro_ch, "DREAMSPACE", link("boom1"))
        await run(m)
        note(macro_ch.sent, "복사가 터져도 보호 메시지는 나감",
             str(len(macro_ch.sent)))
        note(getattr(m, "deleted", False), "원본도 정상적으로 지워짐")
        note(not copied(), "복사는 실제로 실패했음", str(len(copied())))
    finally:
        del copy_ch.send

    print("")
    print("  채널이 지워졌으면 조용히 넘어가야 합니다")
    await setup(rare_copy_channel_id=999999999)
    m = macro(macro_ch, "DREAMSPACE", link("gone1"))
    await run(m)
    note(macro_ch.sent, "보호는 계속됨", str(len(macro_ch.sent)))

    print("")
    print("=== 6. 종료 임베드는 복사하지 않아야 ===")
    await setup()
    await run(macro(macro_ch, "DREAMSPACE", link("end1")))
    before = len(copied())
    m = macro(macro_ch, "DREAMSPACE", link("end2"))
    m.embeds[0].title = "Biome Ended - DREAMSPACE"
    await run(m)
    note(len(copied()) == before, "종료는 복사 안 함", str(len(copied())))

    print("")
    print("=== 7. 설정 UI ===")
    await setup(rare_copy_channel_id=None)
    sel = LG.RareCopyChannelSelect()
    sel._values = [copy_ch]
    it = Inter()
    await sel.callback(it)
    await drain()
    got = it.response.sent or it.followup.sent
    note(got, "채널 선택이 응답함", F.outcome(it))
    note(got and got[0].get("ephemeral"), "ephemeral")
    s = await vdb.get_linkguard_settings(GUILD.id)
    note(s.get("rare_copy_channel_id") == copy_ch.id, "설정에 저장됨",
         str(s.get("rare_copy_channel_id")))

    off = LG.RareCopyOffButton()
    it = Inter()
    await off.callback(it)
    await drain()
    s = await vdb.get_linkguard_settings(GUILD.id)
    note(s.get("rare_copy_channel_id") is None, "끄기가 동작함",
         str(s.get("rare_copy_channel_id")))

    print("")
    print("  화면이 **어떤 바이옴이 갈지** 알려줘야 합니다")
    s = await setup()
    emb = LG.build_rare_copy_embed_settings(GUILD, s)
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields)
    for b in RARE:
        note(b in blob, f"{b} 가 목록에 보임")
    note("전부 보냅니다" in blob, "기본이 '전부 보내기' 임을 밝힘", blob[:70])

    print("")
    print("  켜 두면 몇 분인지 보여줘야 합니다")
    s2 = await setup(rare_copy_dedup_minutes=3)
    emb2 = LG.build_rare_copy_embed_settings(GUILD, s2)
    blob2 = " ".join((f.name or "") + (f.value or "") for f in emb2.fields)
    note("3분" in blob2, "창이 화면에 보임", blob2[:70])

    print("")
    print("  ⚠️ 끄고 켜는 버튼이 실제로 동작해야 합니다 (유일한 탈출구)")
    for minutes in LG.RARE_COPY_WINDOW_CHOICES:
        btn = LG.RareCopyDedupButton(minutes)
        it = Inter()
        await btn.callback(it)
        await drain()
        cur = (await vdb.get_linkguard_settings(GUILD.id)
               ).get("rare_copy_dedup_minutes")
        note(int(cur or 0) == minutes, f"{minutes}분으로 바뀜", str(cur))

    print("")
    print("  규칙이 없으면 **아무것도 안 간다고** 말해야 합니다")
    s = await setup(notify_rules=[])
    emb = LG.build_rare_copy_embed_settings(GUILD, s)
    blob = " ".join((f.name or "") + (f.value or "") for f in emb.fields)
    note("아무것도 복사되지 않습니다" in blob, "빈 규칙을 경고", blob[:70])

    print("")
    print("  설정 메뉴 항목이 예외 없이 열려야 합니다")
    menu = LG.GuardSettingsSelect(await vdb.get_linkguard_settings(GUILD.id))
    menu._values = ["rarecopy"]
    it = Inter()
    try:
        await menu.callback(it)
        await drain()
        note(bool(it.response.sent or it.followup.sent), "[rarecopy] 가 응답함",
             F.outcome(it))
    except Exception as e:
        import traceback
        note(False, "[rarecopy] 예외", f"{type(e).__name__}: {e}")
        traceback.print_exc(limit=3)

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
