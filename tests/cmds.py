"""27개 슬래시 명령어 본문을 전부 호출합니다."""
import sys, os, tempfile, asyncio, inspect

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakes as F
from fakes import Inter, note, GUILD, drain

# 인자가 필요한 명령어에 넣을 값
ARGS = {
    "유저": lambda: GUILD.members[1],
    "user": lambda: GUILD.members[1],
    "member": lambda: GUILD.members[1],
    "포인트": lambda: 100,
    "amount": lambda: 100,
    "points": lambda: 100,
    "사유": lambda: "테스트",
    "개수": lambda: 10,
    "reason": lambda: "테스트",
    "검색어": lambda: "8",
    "query": lambda: "8",
    "페이지": lambda: 1,
    "page": lambda: 1,
}


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    files = [f for f in sorted(os.listdir(os.path.join(PROJ, "cogs")))
             if f.endswith(".py") and not f.startswith("__")]
    for f in files:
        await bot.load_extension(f"cogs.{f[:-3]}")

    cmds = sorted(bot.tree.get_commands(), key=lambda c: c.name)
    print(f"=== 슬래시 명령어 {len(cmds)}개 ===")
    for c in cmds:
        it = Inter()
        kwargs = {}
        skip = False
        for p in getattr(c, "parameters", []):
            if p.name in ARGS:
                kwargs[p.name] = ARGS[p.name]()
            elif not p.required:
                continue
            else:
                skip = True
                print(f"  ??   /{c.name}  (모르는 필수 인자 {p.name})")
        if skip:
            continue
        try:
            await c.callback(c.binding, it, **kwargs)
            await drain()
            note(True, f"/{c.name:12}", F.outcome(it)
                 + ("  " + str(list(kwargs)) if kwargs else ""))
        except Exception as e:
            import traceback
            note(False, f"/{c.name:12}", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=4)

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
