"""/연동조회 에 여러 이름을 한 번에 (2026-10-05 제보).

게임 안 목록을 `Real_Lovercat, lamZero_tim2, …` 처럼 통째로 붙여넣었더니
**한 덩어리 검색어로 찾아서 "검색 결과 없음"** 만 나왔습니다.

지켜야 할 것:
  1. 쉼표 · 줄바꿈 · 공백으로 나눠 하나씩 찾는다
  2. 이름마다 ✅ 연동됨 / ❌ 기록 없음 으로 갈라 보여 준다
  3. 짧은 이름(`Atr`)은 정확히 같은 유저명을 먼저 본다 — 부분 일치로 남의 계정을 끌고 오면 대조가 틀어진다
  4. 한 개짜리 검색은 예전 화면 그대로 (Mongo · JSON 두 경로)
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
from fakes import GUILD, Inter, Member, note

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]


async def run(label):
    import utils.verify_db as vdb
    import cogs.roblox_link as RL
    from discord.ext import commands

    print(f"\n=== {label} ===")
    for uid, rid, name in ((101, 9001, "Real_Lovercat"), (102, 9002, "Atreus"),
                           (103, 9003, "Atr"), (104, 9004, "Babywon_Gone")):
        if GUILD.get_member(uid) is None and uid != 104:      # 104 는 서버를 나간 사람
            GUILD.members.append(Member(uid, f"m{uid}", False, guild=GUILD))
        await vdb.save_roblox_link(GUILD.id, uid, rid, name, method="oauth", created_at="")

    note(RL.split_lookup_queries("Real_Lovercat, lamZero_tim2\nAtr  kingshue007;@Atr") ==
         ["Real_Lovercat", "lamZero_tim2", "Atr", "kingshue007"],
         "쉼표 · 줄바꿈 · 공백 · ; 로 나누고 @ · 중복은 정리")

    cog = RL.RobloxLink(commands.Bot(command_prefix="!", intents=discord.Intents.default()))
    it = Inter()
    await RL.RobloxLink.search.callback(
        cog, it, "Real_Lovercat, lamZero_tim2, level_639282, kingshue007, Babywon_Gone, Atr")
    sent = it.followup.sent[-1]
    e = sent["embed"]
    blob = " ".join(f.name + " " + f.value for f in e.fields)
    note(sent.get("ephemeral") is True, "본인에게만")
    note("일괄" in e.title and "6개" in e.description and "3개** 연동됨" in e.description,
         "**한 번에 6개 → 3개 연동됨 · 3개 없음**", e.description)
    note("✅ `Real_Lovercat` → **Real_Lovercat** <@101>" in blob, "찾은 이름은 누구인지 멘션")
    note("✅ `Atr` → **Atr** <@103>" in blob and "부분 일치" not in blob.split("`Atr`")[1].split("\n")[0],
         "**`Atr` 은 정확히 같은 Atr (Atreus 가 아님)**")
    note("⚠️ 서버에 없음" in blob, "서버를 나간 연동자는 따로 표시")
    for n in ("lamZero_tim2", "level_639282", "kingshue007"):
        note(f"❌ `{n}`" in blob, f"없는 이름 `{n}` 은 ❌")
    note(any("게임 안에서 본 이름" in f.name for f in e.fields), "없는 이름이 있으면 유출 안내")
    note(all(len(f.value) <= 1024 and f.name for f in e.fields) and len(e) <= 6000, "임베드 규격 안")

    it = Inter()
    await RL.RobloxLink.search.callback(cog, it, "Atre")
    e = it.followup.sent[-1]["embed"]
    note(e.title == "🔍 연동 검색 결과" and "Atreus" in " ".join(f.name for f in e.fields),
         "한 개짜리는 예전 화면 그대로 (부분 일치 검색)")

    many = ", ".join(f"nobody{i}" for i in range(40))
    it = Inter()
    await RL.RobloxLink.search.callback(cog, it, many)
    e = it.followup.sent[-1]["embed"]
    note("25개" in e.description and "15개는 빠졌습니다" in (e.footer.text or ""),
         "25개까지만 보고 빠진 개수를 알림", e.footer.text)
    note(all(len(f.value) <= 1024 for f in e.fields), "많아도 칸이 넘치지 않음")


async def main():
    import utils.verify_db as vdb
    await run("JSON 경로")
    FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER
    await run("Mongo 경로")


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
