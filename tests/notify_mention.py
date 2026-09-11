"""바이옴 알림 멘션.

매크로(MultiScope)가 보내는 원본에는 **멘션이 아예 없습니다** — 임베드와
링크 버튼뿐입니다. 그래서 링크를 보호해 다시 올려도 사람들은 희귀 바이옴이
뜬 것을 모릅니다. 봇이 직접 붙이는 경로를 검증합니다.

**가장 중요한 검사는 "울리는 것"이 아니라 "안 울리는 것"입니다.**
누군가 채팅에 `DREAMSPACE` 라고 치는 것만으로 6,500명에게 @everyone 이
가면 그건 되돌릴 수 없는 사고입니다. 바이옴 이름은 **매크로 임베드 안에서만**
읽어야 합니다.
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
from fakes import GUILD, Member, Msg, Role, Inter, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]

RARE = ["DREAMSPACE", "CYBERSPACE", "GLITCHED"]

# 링크마다 코드가 달라야 재알림 쿨타임을 제대로 시험할 수 있습니다.
def link(code: str) -> str:
    return ("https://www.roblox.com/games/1234567/Test"
            f"?privateServerLinkCode={code}")


_mid = [7000]


def macro_msg(channel, title, url, *, fields=None, content="", webhook=True):
    """매크로 웹훅이 보내는 모양 그대로 만듭니다 (임베드 + 링크)."""
    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "Macro", guild=GUILD))
    m.guild = GUILD
    m.webhook_id = 12345 if webhook else None
    m.content = content
    embed = discord.Embed(title=title, description=url)
    for name, value in (fields or []):
        embed.add_field(name=name, value=value)
    m.embeds = [embed]
    return m


def said(channel):
    """마지막으로 재게시된 메시지의 (본문, allowed_mentions)."""
    if not channel.sent:
        return "", None
    last = channel.sent[-1]
    return (last.get("content") or ""), last.get("allowed_mentions")


def pings_everyone(channel) -> bool:
    body, am = said(channel)
    return "@everyone" in body and bool(am and am.everyone)


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

    # ⚠️ **반드시 load_extension 뒤에 잡아야 합니다.**
    # discord.py 의 load_extension 은 sys.modules 를 확인하지 않고 모듈을
    # **새로 만들어 덮어씁니다.** 먼저 import 해 두면 그 이름은 버려진 옛
    # 인스턴스를 가리켜, 여기서 `_notified_at` 을 비워도 실제로 도는 쪽은
    # 그대로입니다. 그러면 쿨타임이 남아 **테스트만 조용히 틀립니다.**
    LG = sys.modules["cogs.link_guard"]
    note(LG.__dict__ is lg._guard_message.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지 (load_extension 이 새로 만듭니다)")

    macro_ch = GUILD.channels[0]      # 111 매크로 채널
    other_ch = GUILD.channels[1]      # 222 규칙 밖 채널

    async def setup(**over):
        """매번 깨끗한 설정에서 시작합니다."""
        base = {
            "enabled": True,
            "channel_ids": [macro_ch.id, other_ch.id],
            "category_ids": [],
            "exception_channel_ids": [],
            "roblox_only": True,
            "relay_links": False,
            "log_channel_id": None,
            "notify_rules": [{
                "channel_ids": [macro_ch.id], "category_ids": [],
                "biomes": RARE, "mention": "everyone", "role_ids": [],
            }],
            "notify_cooldown_minutes": 30,
            "carry_embed_mentions": True,
        }
        base.update(over)
        await vdb.save_linkguard_settings(GUILD.id, base)
        LG._notified_at.clear()
        macro_ch.sent.clear()
        other_ch.sent.clear()

    async def run(msg):
        await lg.on_message(msg)
        await drain()

    print("=== 1. 희귀 바이옴 매크로 임베드 → @everyone ===")
    # 제보된 그 화면입니다: "Biome Started - DREAMSPACE" 인데 멘션이 안 갔습니다.
    await setup()
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("aaa1"),
                        fields=[("Account", "RAY77GUN"), ("Uptime", "4m 4s")]))
    body, am = said(macro_ch)
    note(macro_ch.sent, "보호 메시지가 재게시됨", f"{len(macro_ch.sent)}건")
    note("@everyone" in body, "본문에 @everyone 글자가 들어감", repr(body[:40]))
    note(bool(am and am.everyone),
         "allowed_mentions 로 실제 알림이 허용됨 (글자만 남으면 소용없음)")
    note(bool(am and am.users is False), "유저 멘션은 다시 울리지 않음")
    note("roblox.com" not in body, "링크는 여전히 가려짐")

    for name in ("CYBERSPACE", "GLITCHED"):
        await setup()
        await run(macro_msg(macro_ch, f"Biome Started - {name}", link("aaa2")))
        note(pings_everyone(macro_ch), f"{name} 도 울림")

    print("")
    print("=== 2. 희귀하지 않은 바이옴은 조용히 ===")
    await setup()
    await run(macro_msg(macro_ch, "Biome Started - FOREST", link("bbb1")))
    body, am = said(macro_ch)
    note(macro_ch.sent, "보호는 그대로 동작함 (알림만 안 감)")
    note("@everyone" not in body, "FOREST 에는 @everyone 이 붙지 않음", repr(body[:40]))
    note(not (am and am.everyone), "알림도 허용되지 않음")

    print("")
    print("=== 3. 대소문자 · 공백 · 기호가 달라도 걸려야 ===")
    # 매크로마다 DREAMSPACE / Dream Space / dream_space 로 제각각입니다.
    for title in ("biome started - dreamspace",
                  "Biome Started - Cyber Space",
                  "BIOME  STARTED :: glitched_biome"):
        await setup()
        await run(macro_msg(macro_ch, title, link("ccc1")))
        note(pings_everyone(macro_ch), f"표기가 달라도 걸림: {title[:32]}")

    print("")
    print("  '들어있는지'로 보므로 더 긴 이름을 적으면 더 좁혀집니다")
    # 실제 바이옴 이름은 GLITCHED 입니다. GLITCH 로 적어 두면 GLITCHED 까지
    # 걸리지만, GLITCHED 로 적으면 GLITCH 만 있는 것에는 걸리지 않습니다.
    note(LG.biome_matches(["GLITCH"], "Biome Started - GLITCHED") == "GLITCH",
         "짧게 적으면 더 넓게 걸림 (GLITCH → GLITCHED 도 포함)")
    note(LG.biome_matches(["GLITCHED"], "Biome Started - GLITCH") is None,
         "길게 적으면 더 좁게 걸림 (GLITCHED 는 GLITCH 에 안 걸림)")
    note(LG.biome_matches(RARE, "Biome Ended - DREAMSPACE") == "DREAMSPACE",
         "이름만 적으면 Ended 도 걸림 — 싫으면 'Biome Started - …' 로 적습니다")
    note(LG.biome_matches(["Biome Started - DREAMSPACE"],
                          "Biome Ended - DREAMSPACE") is None,
         "'Biome Started - …' 로 적으면 Ended 는 안 걸림")

    print("")
    print("=== 4. ⚠️ 채팅으로는 절대 울리지 않아야 ===")
    # 여기가 이 기능에서 제일 위험한 곳입니다. 유저가 바이옴 이름을 치는
    # 것만으로 6,500명이 호출되면 되돌릴 수 없습니다.
    await setup()
    user_msg = macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("ddd1"),
                         webhook=False)
    user_msg.author = Member(77, "장난치는유저", guild=GUILD)
    await run(user_msg)
    note(not macro_ch.sent, "웹훅이 아닌 메시지는 재게시 자체를 하지 않음",
         f"{len(macro_ch.sent)}건")
    note(not user_msg.deleted, "유저 메시지를 지우지도 않음")

    await setup()
    plain = Msg(7777, macro_ch, Member(78, "일반유저", guild=GUILD))
    plain.guild, plain.content = GUILD, f"@everyone DREAMSPACE 떴어요 {link('ddd2')}"
    await run(plain)
    note(not macro_ch.sent, "본문에 바이옴 이름 + 링크를 적어도 아무 일 없음")

    print("")
    print("=== 5. ⚠️ 웹훅이어도 '임베드 밖'의 이름으로는 울리지 않아야 ===")
    # 임베드에만 바이옴 이름이 있어야 합니다. 본문은 보지 않습니다.
    await setup()
    m = macro_msg(macro_ch, "Biome Started - FOREST", link("eee1"),
                  content="DREAMSPACE 관련 공지입니다")
    await run(m)
    body, am = said(macro_ch)
    note(macro_ch.sent, "보호는 정상 동작")
    note("@everyone" not in body,
         "본문에만 있는 바이옴 이름으로는 안 울림", repr(body[:50]))
    note(not (am and am.everyone), "알림도 허용되지 않음")

    print("")
    print("=== 6. 같은 링크를 다시 보호할 때는 조용히 ===")
    # 매크로는 같은 링크를 상태 갱신마다 다시 올립니다(실측 평균 145번).
    # 매번 울리면 6,500명이 같은 바이옴 알림을 수백 번 받습니다.
    await setup()
    same = link("fff1")
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", same))
    note(pings_everyone(macro_ch), "첫 번째는 울림")
    first_count = len(macro_ch.sent)

    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", same,
                        fields=[("Uptime", "14m 2s")]))
    body, am = said(macro_ch)
    note(len(macro_ch.sent) > first_count, "두 번째도 보호는 그대로 함")
    note("@everyone" not in body, "두 번째는 멘션이 빠짐", repr(body[:40]))
    note(not (am and am.everyone), "두 번째는 알림도 허용되지 않음")

    print("")
    print("  다른 링크면 쿨타임과 무관하게 울려야 합니다")
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("fff2")))
    note(pings_everyone(macro_ch), "링크가 다르면 울림")

    print("")
    print("  간격을 0 으로 두면 매번 울립니다 (끄는 경로)")
    await setup(notify_cooldown_minutes=0)
    repeat = link("ggg1")
    await run(macro_msg(macro_ch, "Biome Started - GLITCHED", repeat))
    note(pings_everyone(macro_ch), "0분: 첫 번째 울림")
    await run(macro_msg(macro_ch, "Biome Started - GLITCHED", repeat))
    note(pings_everyone(macro_ch), "0분: 두 번째도 울림")

    print("")
    print("=== 7. 규칙 밖 채널은 건드리지 않아야 ===")
    await setup()
    await run(macro_msg(other_ch, "Biome Started - DREAMSPACE", link("hhh1")))
    body, am = said(other_ch)
    note(other_ch.sent, "그 채널에서도 보호는 동작함")
    note("@everyone" not in body, "규칙이 없는 채널에서는 안 울림", repr(body[:40]))

    print("")
    print("=== 8. 임베드 안에 적힌 역할 멘션을 본문으로 끌어올려야 ===")
    # 임베드 **안**의 <@&역할> 은 역할 이름으로 보이지만 알림이 가지 않습니다.
    biome_role = GUILD.roles[3]        # 999
    await setup(notify_rules=[])
    m = macro_msg(macro_ch, "Biome Started - HEAVEN", link("iii1"),
                  fields=[("알림", biome_role.mention)])
    await run(m)
    body, am = said(macro_ch)
    note(biome_role.mention in body,
         "임베드에 있던 역할이 본문으로 올라옴", repr(body[:40]))
    note(bool(am) and biome_role.id in [r.id for r in (am.roles or [])],
         "그 역할이 실제로 울리도록 허용됨", str(am and am.roles))

    print("")
    print("  꺼두면 끌어올리지 않습니다")
    await setup(notify_rules=[], carry_embed_mentions=False)
    await run(macro_msg(macro_ch, "Biome Started - HEAVEN", link("iii2"),
                        fields=[("알림", biome_role.mention)]))
    body, am = said(macro_ch)
    note(biome_role.mention not in body, "꺼두면 본문에 붙지 않음")

    print("")
    print("=== 9. 원본 본문의 멘션은 지금까지처럼 그대로 (회귀 방지) ===")
    await setup(notify_rules=[])
    starfall = Role(1442029067535646773, "Starfall", 5)
    _mid[0] += 1
    m = Msg(_mid[0], macro_ch, Member(3, "웹훅", guild=GUILD))
    m.guild, m.webhook_id = GUILD, 12345
    m.content = f"{starfall.mention}\n{link('jjj1')}"
    m.role_mentions = [starfall]
    await run(m)
    body, am = said(macro_ch)
    note(starfall.mention in body, "원본 본문 멘션이 재게시본에 남음")
    note(bool(am) and starfall.id in [r.id for r in (am.roles or [])],
         "그 역할이 울리도록 허용됨")
    note(body.count(starfall.mention) == 1,
         "같은 멘션을 두 번 적지 않음", repr(body[:40]))

    print("")
    print("=== 10. 규칙이 역할일 때 ===")
    await setup(notify_rules=[{
        "channel_ids": [macro_ch.id], "category_ids": [],
        "biomes": ["DREAMSPACE"], "mention": "roles",
        "role_ids": [biome_role.id],
    }])
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("kkk1")))
    body, am = said(macro_ch)
    note(biome_role.mention in body, "설정한 역할이 본문에 붙음")
    note(bool(am) and biome_role.id in [r.id for r in (am.roles or [])],
         "그 역할이 울리도록 허용됨")
    note(not (am and am.everyone), "역할 규칙이 @everyone 까지 울리지는 않음")

    print("")
    print("=== 10-2. ⚠️ 범위 없는 @everyone 규칙은 절대 울리지 않아야 ===")
    # 손으로 고친 설정이 들어와도 감시 중인 모든 채널에서 6,500명을
    # 부르는 일이 생기면 안 됩니다.
    await setup(notify_rules=[{
        "channel_ids": [], "category_ids": [],
        "biomes": ["DREAMSPACE"], "mention": "everyone", "role_ids": [],
    }])
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("kkk2")))
    body, am = said(macro_ch)
    note(macro_ch.sent, "보호는 그대로 동작함")
    note("@everyone" not in body, "채널을 안 적은 @everyone 규칙은 무시됨", repr(body[:40]))
    note(not (am and am.everyone), "알림도 허용되지 않음")

    it = Inter()
    view = LG.NotifyTargetView([], [], ["DREAMSPACE"])
    await view.everyone.callback(it)
    sent = it.response.sent[0] if it.response.sent else {}
    embed_out = sent.get("embed")
    note(embed_out is not None and "⛔" in (embed_out.title or ""),
         "UI 도 채널 없는 @everyone 규칙 저장을 거부함",
         str(embed_out and embed_out.title))
    after = await vdb.get_linkguard_settings(GUILD.id)
    note(len(after.get("notify_rules") or []) == 1, "거부된 규칙이 저장되지 않음")

    print("")
    print("=== 10-3. 카테고리로 지정하면 그 안의 채널 전부 ===")
    # 매크로 채널은 카테고리별로 나뉘어 있습니다. 채널을 하나씩 고르게 하면
    # 새 채널이 생길 때마다 규칙을 고쳐야 하고, 고치기 전까지 그 채널의
    # 희귀 바이옴은 조용히 지나갑니다.
    category = GUILD.categories[0]          # 444
    in_cat = GUILD.channels[1]              # 222 — 이 카테고리 안에 있다고 둡니다
    out_cat = GUILD.channels[0]             # 111 — 카테고리 밖
    in_cat.category_id = category.id
    out_cat.category_id = None

    await setup(channel_ids=[in_cat.id, out_cat.id], notify_rules=[{
        "channel_ids": [], "category_ids": [category.id],
        "biomes": RARE, "mention": "everyone", "role_ids": [],
    }])
    await run(macro_msg(in_cat, "Biome Started - DREAMSPACE", link("cat1")))
    note(pings_everyone(in_cat), "카테고리 안의 채널에서 울림")

    await run(macro_msg(out_cat, "Biome Started - DREAMSPACE", link("cat2")))
    body, am = said(out_cat)
    note(out_cat.sent, "카테고리 밖 채널도 보호는 그대로")
    note("@everyone" not in body, "카테고리 밖에서는 울리지 않음", repr(body[:40]))

    print("")
    print("  카테고리에 나중에 생긴 채널도 자동으로 포함됩니다")
    newborn = F.Channel(555001, "새매크로채널", GUILD)
    newborn.category_id = category.id
    GUILD._chans[newborn.id] = newborn
    await setup(channel_ids=[in_cat.id], category_ids=[category.id],
                notify_rules=[{
                    "channel_ids": [], "category_ids": [category.id],
                    "biomes": RARE, "mention": "everyone", "role_ids": [],
                }])
    newborn.sent.clear()
    await run(macro_msg(newborn, "Biome Started - GLITCHED", link("cat3")))
    note(pings_everyone(newborn), "규칙을 고치지 않아도 새 채널에서 울림")

    print("")
    print("  @everyone 은 카테고리만 지정해도 허용됩니다 (범위가 있으므로)")
    it = Inter()
    view = LG.NotifyTargetView([], [category.id], RARE)
    await view.everyone.callback(it)
    sent = it.response.sent[0] if it.response.sent else {}
    embed_out = sent.get("embed")
    note(embed_out is not None and "⛔" not in (embed_out.title or ""),
         "카테고리를 지정한 @everyone 은 저장됨", str(embed_out and embed_out.title))

    print("")
    print("  카테고리를 고르면 category_ids 로 들어가야 합니다")
    it = Inter()
    scope = LG.NotifyScopeSelect()
    scope._values = [category, in_cat]
    await scope.callback(it)
    note(it.response.modal == "NotifyBiomeModal", "채널+카테고리 혼합 선택도 진행됨")

    scope2 = LG.NotifyScopeSelect()
    scope2._values = [category]
    it2 = Inter()
    await scope2.callback(it2)
    note(discord.ChannelType.category in [c.type for c in scope2.values],
         "카테고리가 선택값으로 들어옴")

    print("")
    print("  설명 줄에 카테고리 이름이 보여야 합니다")
    desc = LG.describe_notify_rule({
        "channel_ids": [], "category_ids": [category.id],
        "biomes": RARE, "mention": "everyone", "role_ids": []}, GUILD)
    note(category.name in desc, "카테고리 이름이 보임", desc[:60])

    print("")
    print("  문자열로 저장된 카테고리 ID 도 정규화돼야 합니다")
    await setup(channel_ids=[in_cat.id], notify_rules=[{
        "channel_ids": [], "category_ids": [str(category.id)],
        "biomes": RARE, "mention": "everyone", "role_ids": [],
    }])
    loaded_cat = await vdb.get_linkguard_settings(GUILD.id)
    rule_cat = (loaded_cat.get("notify_rules") or [{}])[0]
    note(rule_cat.get("category_ids") == [category.id],
         "카테고리 ID 가 int 로 정규화됨", str(rule_cat.get("category_ids")))
    in_cat.sent.clear()
    await run(macro_msg(in_cat, "Biome Started - CYBERSPACE", link("cat4")))
    note(pings_everyone(in_cat), "문자열로 저장돼 있어도 카테고리 규칙이 동작")

    # 뒤 검사들이 카테고리에 영향받지 않도록 되돌립니다
    in_cat.category_id = None
    out_cat.category_id = None

    print("")
    print("=== 11. 채널을 지정하지 않은 규칙은 모든 감시 채널에 ===")
    await setup(notify_rules=[{
        "channel_ids": [], "category_ids": [],
        "biomes": ["DREAMSPACE"], "mention": "here", "role_ids": [],
    }])
    await run(macro_msg(other_ch, "Biome Started - DREAMSPACE", link("lll1")))
    body, am = said(other_ch)
    note("@here" in body, "채널 미지정 규칙은 다른 채널에도 적용됨", repr(body[:30]))
    note(bool(am and am.everyone),
         "@here 도 everyone 플래그로 허용해야 실제로 울림")

    print("")
    print("=== 12. 저장된 ID 가 문자열이어도 걸려야 (JSON 폴백 대비) ===")
    # 폴백을 거치면 숫자가 문자열로 돌아옵니다. 그대로 두면 채널 비교가
    # 조용히 어긋나 **규칙이 있는데 안 울립니다.**
    await setup(notify_rules=[{
        "channel_ids": [str(macro_ch.id)], "category_ids": [],
        "biomes": ["DREAMSPACE"], "mention": "everyone",
        "role_ids": [str(biome_role.id)],
    }])
    loaded = await vdb.get_linkguard_settings(GUILD.id)
    rule = (loaded.get("notify_rules") or [{}])[0]
    note(rule.get("channel_ids") == [macro_ch.id],
         "채널 ID 가 int 로 정규화됨", str(rule.get("channel_ids")))
    note(rule.get("role_ids") == [biome_role.id],
         "역할 ID 가 int 로 정규화됨", str(rule.get("role_ids")))
    await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("mmm1")))
    note(pings_everyone(macro_ch), "문자열로 저장돼 있어도 정상 동작")

    print("")
    print("=== 13. 이상한 규칙이 들어와도 죽지 않아야 ===")
    await setup(notify_rules=[
        "문자열",                                   # dict 가 아님
        {"mention": "없는값", "biomes": ["DREAMSPACE"]},
        {"channel_ids": ["abc"], "biomes": [""], "mention": "everyone"},
    ])
    try:
        await run(macro_msg(macro_ch, "Biome Started - DREAMSPACE", link("nnn1")))
        note(True, "깨진 규칙이 있어도 보호가 멈추지 않음")
    except Exception as e:
        note(False, "깨진 규칙에서 예외", f"{type(e).__name__}: {e}")

    print("")
    print("=== 14. 설정 UI 가 실제로 동작해야 ===")
    # cog 로드만으로는 NameError 를 잡지 못합니다. 콜백을 직접 부릅니다.
    await setup(notify_rules=[])

    it = Inter()
    view = LG.NotifyRuleView(await vdb.get_linkguard_settings(GUILD.id))
    await view.add.callback(it)
    note(it.response.sent, "[➕ 규칙 추가] 가 응답함")

    it = Inter()
    scope = LG.NotifyScopeSelect()
    scope._values = [macro_ch]
    await scope.callback(it)
    note(it.response.modal == "NotifyBiomeModal", "채널을 고르면 바이옴 모달이 뜸",
         str(it.response.modal))

    it = Inter()
    modal = LG.NotifyBiomeModal([macro_ch.id], [])
    modal.biomes._value = "DREAMSPACE, CYBERSPACE\nGLITCHED"
    await modal.on_submit(it)
    note(it.response.sent, "바이옴을 적으면 대상 선택이 뜸")

    it = Inter()
    target = LG.NotifyTargetView([macro_ch.id], [], RARE)
    await target.everyone.callback(it)
    saved = await vdb.get_linkguard_settings(GUILD.id)
    rules = saved.get("notify_rules") or []
    note(len(rules) == 1, "@everyone 을 고르면 규칙이 저장됨", str(len(rules)))
    note(rules and rules[0].get("mention") == "everyone", "멘션 종류가 everyone")
    note(rules and rules[0].get("biomes") == RARE, "바이옴 목록이 그대로 저장됨",
         str(rules and rules[0].get("biomes")))

    print("")
    print("  저장한 규칙이 실제로 울려야 합니다 (설정 → 동작 연결 확인)")
    LG._notified_at.clear()
    macro_ch.sent.clear()
    await run(macro_msg(macro_ch, "Biome Started - CYBERSPACE", link("ooo1")))
    note(pings_everyone(macro_ch), "UI 로 만든 규칙이 그대로 동작함")

    print("")
    it = Inter()
    remove = LG.NotifyRuleRemoveSelect(rules, GUILD)
    remove._values = ["0"]
    await remove.callback(it)
    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(not (saved.get("notify_rules") or []), "규칙 삭제가 동작함")

    print("")
    for raw, ok in (("15", True), ("0", True), ("공백", False),
                    ("   ", False), ("-5", False), ("99999", False)):
        it = Inter()
        cool = LG.NotifyCooldownModal()
        cool.minutes._value = raw
        await cool.on_submit(it)
        sent = it.response.sent[0] if it.response.sent else {}
        embed = sent.get("embed")
        accepted = embed is not None
        note(accepted == ok, f"재알림 간격 입력 {raw!r:10} "
                             f"{'통과기대' if ok else '거부기대'}",
             "통과" if accepted else "거부")

    print("")
    it = Inter()
    toggle = LG.NotifyRuleView(await vdb.get_linkguard_settings(GUILD.id))
    await toggle.carry.callback(it)
    saved = await vdb.get_linkguard_settings(GUILD.id)
    note(saved.get("carry_embed_mentions") is False, "임베드 멘션 살리기 토글 동작")

    print("")
    print("=== 15. 모든 응답이 ephemeral 이어야 ===")
    # 설정 화면이 공개로 뜨면 채널 ID·역할 구성이 그대로 노출됩니다.
    leaked = []
    for label, coro in (("규칙 추가", lambda i: LG.NotifyRuleView({}).add.callback(i)),
                        ("규칙 삭제", lambda i: LG.NotifyRuleView({}).remove.callback(i)),
                        ("간격 변경", None)):
        if coro is None:
            continue
        it = Inter()
        await coro(it)
        for s in it.response.sent:
            if not s.get("ephemeral"):
                leaked.append(label)
    note(not leaked, "설정 응답이 전부 ephemeral", str(leaked))

    print("")
    print("=== 16. 상태 임베드에 규칙이 보여야 ===")
    await setup()
    settings = await vdb.get_linkguard_settings(GUILD.id)
    vs = await vdb.get_verify_settings(GUILD.id)
    embed = LG.build_status_embed(GUILD, settings, vs)
    blob = " ".join((f.name or "") + (f.value or "") for f in embed.fields)
    note("바이옴 알림 멘션" in blob, "현재 설정에 알림 칸이 있음")
    note("DREAMSPACE" in blob, "설정한 바이옴이 보임")

    notify_embed = LG.build_notify_embed(GUILD, settings)
    note("DREAMSPACE" in (notify_embed.description or ""),
         "알림 설정 화면에 규칙이 보임")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
