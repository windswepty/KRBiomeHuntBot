"""컴포넌트 V2 로 보내는 매크로 (SolRich v1.2.1).

실제 사고(2026-10-04): SolRich 는 임베드가 아니라 **컴포넌트 V2**(컨테이너 ·
글 칸 · 구분선 · 썸네일)로 보냅니다. 화면엔 임베드처럼 보이지만
`message.embeds` 가 비어 있어서
  ① 재게시하면 **[개인서버 입장하기] 버튼 하나만** 남고 내용이 사라졌고
  ② 바이옴 이름을 못 읽어 **희귀 바이옴인데 알림 멘션이 안 나갔습니다.**

지켜야 할 것:
  1. 글 칸을 임베드로 옮겨 재게시한다 (제목 · 본문 · 바닥글 · 썸네일 · 색)
  2. 바이옴 이름을 읽고 알림 규칙 · 통계가 그대로 동작한다
  3. 링크가 버튼 · 섹션 오른쪽 버튼 · 글 안 어디에 있든 보호한다
  4. 작은 글씨(-#, 계정 · 매크로 이름)는 바이옴 판정에 섞지 않는다
  5. 로블록스 링크가 없는 V2 메시지는 건드리지 않는다
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.components import _component_factory
import fakemongo as FM
from fakes import GUILD, Member, Msg, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col", "biome_col"]
_mid = [9100]
THUMB = "https://cdn.discordapp.com/attachments/1/2/controller.png"
SUPPORT = "https://discord.gg/solrich"
STAMP = 1759591500


def link(code):
    return f"https://www.roblox.com/share?code={code}&type=Server"


def text(s):
    return {"type": 10, "content": s}


def button(url, label="Join Server"):
    return {"type": 2, "style": 5, "label": label, "url": url}


def solrich(channel, title, *, account="leetaese", url=None, where="row"):
    """SolRich v1.2.1 모양: 컨테이너 안에 작은 글씨 머리줄 · 섹션(제목+썸네일) ·
    구분선 · 지원 서버 인용 · 바닥글. 개인서버 링크는 `where` 자리에 둡니다."""
    body = f"## {title}\n**Account:** {account}\n**Session Time:** 00:42:59"
    if url and where == "text":
        body += f"\n[Join Private Server]({url})"
    accessory = ({"type": 11, "media": {"url": THUMB}} if not (url and where == "accessory")
                 else button(url))
    children = [
        text(f"-# {account} · `3959176513`"),
        {"type": 9, "components": [text(body)], "accessory": accessory},
        {"type": 14, "divider": True, "spacing": 1},
        text(f"> ### [Support Server]({SUPPORT})\n"
             "> Macro made by [youtube.com/@Finnerich](https://youtube.com/@Finnerich)"),
        {"type": 14, "divider": True, "spacing": 1},
        # 실제 바닥글 모양(2026-10-04 화면) — 링크 · 굵게 · 코드가 섞여 있습니다
        text(f"-# **[{account}](https://www.roblox.com/users/3959176513/profile)** • "
             f"`3959176513` • **SolRich v1.2.1** • <t:{STAMP}:R>"),
    ]
    if url and where == "row":
        children.append({"type": 1, "components": [button(url)]})
    data = {"type": 17, "accent_color": 0x57F287, "components": children}

    _mid[0] += 1
    m = Msg(_mid[0], channel, Member(3, "staysung's Macro", guild=GUILD))
    m.guild, m.webhook_id, m.content, m.embeds = GUILD, 12345, "", []
    m.components = [_component_factory(data)]
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
                          "biomes": ["GLITCHED"], "mention": "everyone", "role_ids": []}],
        "biome_ignore": []})

    async def feed(m):
        before = len(ch.sent)
        await lg.on_message(m)
        await drain()
        return ch.sent[before:]

    print("=== 1. 글 칸을 임베드로 옮김 ===")
    m = solrich(ch, "Biome Started - GLITCHED", url=link("aaa"))
    e = LG.layout_embed(m)
    note(e is not None and e.title == "Biome Started - GLITCHED", "첫 제목 줄 → 임베드 제목",
         str(e and e.title))
    note(e is not None and "**Account:** leetaese" in (e.description or "")
         and "-#" not in (e.description or ""), "본문은 남고 작은 글씨는 본문에서 빠짐",
         repr(e and e.description))
    note(e is not None and "SolRich v1.2.1" in (e.footer.text or "")
         and "<t:" not in (e.footer.text or ""), "작은 글씨 → 바닥글 (<t:…> 는 떼어 냄)",
         repr(e and e.footer.text))
    foot = (e.footer.text or "") if e is not None else ""
    note(not any(x in foot for x in ("**", "`", "[", "](", "http")),
         "**바닥글에 마크다운 기호가 날것으로 남지 않음** (바닥글은 마크다운을 안 그림)", repr(foot))
    note("leetaese" in foot and "3959176513" in foot, "글자는 남음", repr(foot))
    note(e is not None and e.author.name == "leetaese · 3959176513" and not e.author.url,
         "제목 위 계정 줄 → 원본처럼 맨 위(작성자) · 링크 없음", repr(e and e.author.name))
    note(LG.layout_embed(solrich(ch, "Biome Started - X", url=link("q"))).author.name
         not in (None, ""), "재게시 때 웹훅 이름으로 덮이지 않을 자리")
    note("**[Support Server]" in (e.description or "") and "###" not in (e.description or ""),
         "본문 안 제목 줄은 굵은 글씨로 낮춤 (제목보다 커지지 않게)", repr(e and e.description))
    note(e is not None and e.timestamp is not None and int(e.timestamp.timestamp()) == STAMP,
         "<t:…> 는 임베드 시각으로")
    note(e is not None and e.thumbnail.url == THUMB, "썸네일 유지")
    note(e is not None and e.colour is not None and e.colour.value == 0x57F287, "테두리 색 유지")
    note(LG.detect_biome_name(m) == "GLITCHED", "바이옴 이름을 읽음", str(LG.detect_biome_name(m)))
    note(link("aaa") in LG.extract_component_links(m), "줄 버튼의 링크를 찾음")

    print("\n=== 2. 재게시: 내용이 남고 희귀 바이옴 알림이 나감 ===")
    out = await feed(m)
    note(len(out) == 1, "한 번 재게시", str(len(out)))
    sent = out[0] if out else {}
    embeds = sent.get("embeds") or []
    note(len(embeds) == 1 and embeds[0].title == "Biome Started - GLITCHED",
         "**버튼만 남지 않고 임베드가 함께 올라감**", str([x.title for x in embeds]))
    note("@everyone" in (sent.get("content") or ""), "희귀 바이옴 규칙의 멘션이 붙음",
         repr(sent.get("content")))
    blob = " ".join(((x.description or "") + (x.title or "")) for x in embeds)
    note("roblox.com" not in blob, "재게시 본문에 원문 링크 없음")
    note(SUPPORT in blob, "로블록스가 아닌 지원 서버 링크는 그대로 둠")
    note(any(link("aaa") in (d.get("links") or []) for d in cols["plink_col"].docs.values()),
         "보호 링크로 저장됨")
    rows = {r["biome"]: r for r in await vdb.get_biome_buckets(GUILD.id, 90)}
    note(rows.get("GLITCHED", {}).get("count") == 1, "통계에 기록됨", str(rows.keys()))
    note(rows.get("GLITCHED", {}).get("notified_count") == 1, "'알림 나감' 으로 기록됨",
         str(rows.get("GLITCHED")))

    print("\n=== 3. 링크가 다른 자리에 있어도 보호 ===")
    m = solrich(ch, "Biome Started - WINDY", url=link("bbb"), where="accessory")
    note(link("bbb") in LG.extract_component_links(m), "섹션 오른쪽 버튼(accessory)의 링크")
    out = await feed(m)
    note(len(out) == 1 and (out[0].get("embeds") or [None])[0] is not None,
         "오른쪽 버튼 링크도 보호 · 재게시")
    m = solrich(ch, "Biome Started - RAINY", url=link("ccc"), where="text")
    out = await feed(m)
    blob = " ".join((x.description or "") for x in (out[0].get("embeds") or [])) if out else ""
    note(len(out) == 1 and "roblox.com" not in blob, "글 안 링크도 지우고 보호", blob[-60:])
    note(any(link("ccc") in (d.get("links") or []) for d in cols["plink_col"].docs.values()),
         "글 안 링크도 저장됨")

    print("\n=== 4. 작은 글씨(계정 · 매크로 이름)는 바이옴 판정에 안 섞음 ===")
    m = solrich(ch, "Strange Controller - Used", account="glitchedfan", url=link("ddd"))
    note("GLITCHEDFAN" not in LG.biome_haystack(m).upper().replace("**ACCOUNT:** GLITCHEDFAN", ""),
         "머리줄 계정 이름은 판정 글에 없음")
    m2 = solrich(ch, "Strange Controller - Used", url=link("ddd"))
    m2.components[0].children[0] = _component_factory(text("-# glitched_hunter · `1`"))
    out = await feed(m2)
    note(len(out) == 1 and "@everyone" not in (out[0].get("content") or ""),
         "작은 글씨에 GLITCHED 가 있어도 전원 호출 안 함", repr(out and out[0].get("content")))

    print("\n=== 5. 로블록스 링크 없는 V2 · 종료 알림 ===")
    m = solrich(ch, "Strange Controller - Used")
    out = await feed(m)
    note(out == [], "개인서버 링크가 없으면 원본 그대로 (재게시 안 함)")
    m = solrich(ch, "Biome Ended - GLITCHED", url=link("eee"))
    out = await feed(m)
    note(len(out) == 1 and "@everyone" not in (out[0].get("content") or ""),
         "종료 알림에는 멘션을 안 붙임")

    print("\n=== 6. 컴포넌트가 없는 보통 메시지는 그대로 ===")
    plain = Msg(9999, ch, Member(4, "u", guild=GUILD))
    plain.components = []
    note(LG.layout_embed(plain) is None, "V2 가 아니면 None")
    note(LG.message_embeds(plain) == list(plain.embeds or []), "임베드 목록은 원래대로")


asyncio.run(main())
import fakes as F
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
