"""지문이 디스코드에 등록되는 변경을 빠짐없이 감지하는가.

빠뜨린 항목은 "바뀌었는데 안 바뀐 것"이 되어 동기화를 건너뜁니다.
특히 default_permissions 는 누가 그 명령어를 쓸 수 있는지를 정합니다.
"""
import sys, os, tempfile, importlib.util

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import discord
from discord import app_commands
from discord.ext import commands
import fakes as F
from fakes import note

spec = importlib.util.spec_from_file_location(
    "mainmod", os.path.join(PROJ, "main.py"))
mainmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mainmod)
fp = mainmod._command_fingerprint


def tree(perms=True, desc="설명", pdesc="대상", optional=False, name="테스트"):
    b = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    if optional:
        @b.tree.command(name=name, description=desc)
        @app_commands.default_permissions(administrator=perms)
        @app_commands.describe(유저=pdesc)
        async def _c(interaction: discord.Interaction,
                     유저: discord.Member = None):
            pass
    else:
        @b.tree.command(name=name, description=desc)
        @app_commands.default_permissions(administrator=perms)
        @app_commands.describe(유저=pdesc)
        async def _c(interaction: discord.Interaction, 유저: discord.Member):
            pass
    return b.tree


base = fp(tree())
print("=== 안 바뀌면 같아야 합니다 ===")
note(fp(tree()) == base, "같은 구성 → 같은 지문", base)

print("\n=== 바뀌면 반드시 달라야 합니다 ===")
cases = [
    ("기본권한 (누가 쓸 수 있는가)", dict(perms=False)),
    ("명령어 설명", dict(desc="다른 설명")),
    ("옵션 설명", dict(pdesc="다른 대상")),
    ("옵션 필수 여부", dict(optional=True)),
    ("명령어 이름", dict(name="다른이름")),
]
for label, kw in cases:
    got = fp(tree(**kw))
    note(got != base, f"{label} 변경 감지", f"{base} → {got}")

print("\n=== 실제 봇 27개 명령어로 ===")
import asyncio


async def real():
    b = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await b.load_extension(f"cogs.{f[:-3]}")
    h1 = fp(b.tree)
    h2 = fp(b.tree)
    note(h1 == h2, "같은 트리를 두 번 → 같은 값 (안정적)", h1)
    note(len(h1) == 16, "16자 해시", h1)
    perms = sum(1 for c in b.tree.get_commands()
                if getattr(c, "default_permissions", None))
    note(perms > 0, "기본권한이 지문에 실제로 반영됨",
         f"권한 지정 명령어 {perms}개")
    await b.close()

asyncio.run(real())

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
