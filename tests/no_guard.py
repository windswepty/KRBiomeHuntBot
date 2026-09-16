"""보호 제외 채널 — 원본 임베드를 그대로 두는 관리자 전용 채널.

**이건 보호를 일부러 끄는 기능입니다.** 그래서 검증이 지켜야 할 것도
'되는가' 가 아니라 **'끄면서 무엇을 잃지 않는가'** 입니다.

  1. 제외 채널의 링크도 **유출 감지 색인에는 들어가야** 합니다.
     안 넣으면 그 채널에만 올라온 링크는 다른 데로 새도 못 알아봅니다.
  2. **제외가 카테고리보다 우선**해야 합니다. 카테고리로 통째로 감시하면서
     그 안의 채널 하나만 빼는 것이 이 기능의 쓰임새입니다.
  3. `@everyone` 이 볼 수 있으면 **빨간 경고**가 떠야 합니다.
     보호를 끈 채널이 공개면 링크 보호 전체가 무의미합니다.
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

ROBLOX = "https://www.roblox.com/games/123/T?privateServerLinkCode=abc987xyz"
_mid = [4000]


def macro(channel, url=ROBLOX, title="Biome Started - DREAMSPACE"):
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content = GUILD, 12345, ""
    e = discord.Embed(title=title, description=url)
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

    # ⚠️ load_extension 은 sys.modules 를 새 인스턴스로 덮어씁니다.
    LG = sys.modules["cogs.link_guard"]
    note(LG.__dict__ is lg._guard_message.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    guarded_ch = GUILD.channels[0]      # 보호되는 채널
    admin_ch = GUILD.channels[1]        # 보호를 끌 채널
    other_ch = GUILD.channels[2]

    async def setup(**over):
        base = {"enabled": True, "channel_ids": [guarded_ch.id, admin_ch.id],
                "category_ids": [], "exception_channel_ids": [],
                "no_guard_channel_ids": [], "roblox_only": True,
                "relay_links": False, "log_channel_id": None,
                "notify_rules": [], "carry_embed_mentions": True,
                "notify_exclude": ["Biome End"]}
        base.update(over)
        await vdb.save_linkguard_settings(GUILD.id, base)
        for c in GUILD.channels:
            c.sent.clear()
        if getattr(vdb.guardkey_col, "docs", None) is not None:
            vdb.guardkey_col.docs.clear()
        if hasattr(LG, "_guarded_keys_cache"):
            try:
                LG._guarded_keys_cache.clear()
            except Exception:
                pass
        return await vdb.get_linkguard_settings(GUILD.id)

    async def run(msg):
        await lg.on_message(msg)
        await drain()

    print("=== 1. 제외하지 않으면 평소대로 보호됩니다 ===")
    await setup()
    m = macro(admin_ch)
    await run(m)
    note(admin_ch.sent, "봇이 재게시함", str(len(admin_ch.sent)))
    note(m.deleted, "원본이 지워짐", str(getattr(m, "deleted", None)))

    print("")
    print("=== 2. ⚠️ 제외하면 원본이 그대로 남아야 ===")
    await setup(no_guard_channel_ids=[admin_ch.id])
    m = macro(admin_ch)
    await run(m)
    note(not admin_ch.sent, "재게시하지 않음", str(len(admin_ch.sent)))
    note(not getattr(m, "deleted", False), "원본을 지우지 않음")

    print("")
    print("  같은 설정에서 **다른 채널은 여전히 보호**돼야 합니다")
    m2 = macro(guarded_ch)
    await run(m2)
    note(guarded_ch.sent, "보호 채널은 재게시됨", str(len(guarded_ch.sent)))
    note(getattr(m2, "deleted", False), "원본도 지워짐")

    print("")
    print("=== 3. ⚠️ 보호는 껐어도 유출 감지 색인에는 들어가야 ===")
    # 안 넣으면 이 채널에만 올라온 링크는 다른 데로 새도 못 알아봅니다.
    await setup(no_guard_channel_ids=[admin_ch.id])
    await run(macro(admin_ch))
    keys = await vdb.get_guarded_keys(GUILD.id)
    want = LG.link_key(ROBLOX)
    note(want in keys, "제외 채널의 링크도 색인에 있음", f"{len(keys)}개")

    print("")
    print("  그래서 **여기서 복사해 다른 데 뿌리면 잡혀야** 합니다")
    leak = Msg(7777, other_ch, Member(55, "유출자", guild=GUILD))
    leak.guild, leak.webhook_id = GUILD, None
    leak.content = f"자 여기 링크 {ROBLOX}"
    leak.embeds = []
    # ⚠️ 관측 지점은 **실제로 존재하는 것**이어야 합니다. 없는 이름을
    #    가로채고 `or real is None` 으로 넘기면 헛도는 검증이 됩니다.
    reported = []
    real_log = LG.send_guard_log

    async def catch(guild, settings, embed, *a, **k):
        reported.append(embed)

    assert callable(real_log), "send_guard_log 가 없습니다 — 관측 지점 확인 필요"
    LG.send_guard_log = catch
    try:
        await lg._detect_leak(leak)
        await drain()
        note(len(reported) == 1, "유출로 보고됨", f"{len(reported)}건")
        if reported:
            blob = (reported[0].description or "") + " ".join(
                (f.name or "") + (f.value or "") for f in reported[0].fields)
            note("유출자" in blob or "55" in blob, "누가 올렸는지 담김", blob[:60])
    finally:
        LG.send_guard_log = real_log

    print("")
    print("  ⚠️ 제외 채널의 **원본 자체**는 유출로 잡히면 안 됩니다")
    # 웹훅 메시지라 `_detect_leak` 이 애초에 보지 않습니다.
    reported.clear()
    LG.send_guard_log = catch
    try:
        await lg._detect_leak(macro(admin_ch))
        await drain()
        note(not reported, "웹훅 원본은 유출이 아님", f"{len(reported)}건")
    finally:
        LG.send_guard_log = real_log

    print("")
    print("=== 4. ⚠️ 제외가 카테고리보다 우선해야 ===")
    # 카테고리로 통째로 감시하면서 그 안의 채널 하나만 빼는 것이 쓰임새입니다.
    cat = GUILD.categories[0]
    admin_ch.category_id = cat.id
    guarded_ch.category_id = cat.id
    try:
        s = await setup(channel_ids=[], category_ids=[cat.id],
                        no_guard_channel_ids=[admin_ch.id])
        note(LG.is_guarded_channel(guarded_ch, s),
             "같은 카테고리의 다른 채널은 보호 대상")
        note(not LG.is_guarded_channel(admin_ch, s),
             "**제외한 채널은 카테고리에 먹히지 않음**")
        note(LG.is_no_guard_channel(admin_ch, s), "제외로 판정됨")

        await setup(channel_ids=[], category_ids=[cat.id],
                    no_guard_channel_ids=[admin_ch.id])
        await run(macro(admin_ch))
        note(not admin_ch.sent, "실제로도 재게시하지 않음")
        await run(macro(guarded_ch))
        note(guarded_ch.sent, "같은 카테고리의 다른 채널은 보호됨")

        print("")
        print("  카테고리 자체를 제외할 수도 있어야 합니다")
        s = await setup(channel_ids=[], category_ids=[cat.id],
                        no_guard_channel_ids=[cat.id])
        note(not LG.is_guarded_channel(admin_ch, s), "카테고리 제외가 먹힘")
        note(not LG.is_guarded_channel(guarded_ch, s), "안의 채널 전부 제외")
    finally:
        admin_ch.category_id = None
        guarded_ch.category_id = None

    print("")
    print("=== 5. 해제하면 다시 보호돼야 ===")
    await setup(no_guard_channel_ids=[])
    m = macro(admin_ch)
    await run(m)
    note(admin_ch.sent, "다시 재게시함")
    note(getattr(m, "deleted", False), "원본도 다시 지워짐")

    print("")
    print("=== 6. ⚠️ 전체 공개면 빨간 경고가 떠야 ===")
    # 보호를 끈 채널이 공개면 링크 보호 전체가 무의미합니다.
    embed = LG.build_no_guard_embed(GUILD, [admin_ch.id])
    blob = (embed.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in embed.fields)
    note(embed.color == discord.Color.red(), "빨간색", str(embed.color))
    note("전체 공개" in blob, "공개라고 분명히 말함", blob[:70])
    note("모두에게 그대로 보입니다" in blob, "무엇이 위험한지 설명")
    note("채널 보기" in blob, "어떻게 고치는지 알려줌")

    print("")
    print("  관리자만 볼 수 있으면 초록으로 확인해 줍니다")
    real_perm = type(admin_ch).permissions_for
    try:
        type(admin_ch).permissions_for = lambda self, m: F.Perms(
            read_messages=False, view_channel=False)
        embed = LG.build_no_guard_embed(GUILD, [admin_ch.id])
        blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
        note(embed.color != discord.Color.red(), "빨간색이 아님", str(embed.color))
        note("관리자만 볼 수 있는" in blob, "안전하다고 확인해 줌", blob[:60])
    finally:
        type(admin_ch).permissions_for = real_perm

    print("")
    print("  목록이 비어 있어도 화면이 떠야 합니다")
    embed = LG.build_no_guard_embed(GUILD, [])
    note("지정된 채널이 없습니다" in (embed.description or ""), "빈 상태 안내")
    note("유출 감지는 계속" in " ".join((f.name or "") for f in embed.fields),
         "색인은 계속된다는 것을 밝힘")

    print("")
    print("=== 7. 설정 UI 가 실제로 동작해야 ===")
    # cog 로드만으로는 NameError 를 잡지 못합니다.
    await setup()
    sel = LG.AddNoGuardChannelSelect()
    sel._values = [admin_ch]
    it = Inter()
    await sel.callback(it)
    await drain()
    got = it.response.sent or it.followup.sent
    note(got, "추가 선택이 응답함", F.outcome(it))
    note(got and got[0].get("ephemeral"), "ephemeral")
    s = await vdb.get_linkguard_settings(GUILD.id)
    note(admin_ch.id in (s.get("no_guard_channel_ids") or []),
         "설정에 저장됨", str(s.get("no_guard_channel_ids")))

    rm = LG.RemoveNoGuardChannelSelect([admin_ch.id], GUILD)
    rm._values = [str(admin_ch.id)]
    it = Inter()
    await rm.callback(it)
    await drain()
    note(it.response.sent or it.followup.sent, "해제 선택이 응답함", F.outcome(it))
    s = await vdb.get_linkguard_settings(GUILD.id)
    note(not (s.get("no_guard_channel_ids") or []), "설정에서 빠짐",
         str(s.get("no_guard_channel_ids")))

    print("")
    print("  설정 메뉴의 두 항목이 예외 없이 열려야 합니다")
    for value in ("addnoguard", "removenoguard"):
        menu = LG.GuardSettingsSelect(await vdb.get_linkguard_settings(GUILD.id))
        menu._values = [value]
        it = Inter()
        try:
            await menu.callback(it)
            await drain()
            note(bool(it.response.sent or it.followup.sent),
                 f"[{value}] 가 응답함", F.outcome(it))
        except Exception as e:
            import traceback
            note(False, f"[{value}] 예외", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=3)

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
