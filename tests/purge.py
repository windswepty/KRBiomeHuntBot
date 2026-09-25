"""/청소 — 최근 메시지를 개수만큼 지웁니다.

삭제 자체는 **discord.py 의 진짜 `_purge_helper`** 로 돌립니다(채널만 가짜).
그래야 14일 넘은 메시지 처리 · 한꺼번에/하나씩 삭제 · 시스템 메시지
건너뛰기가 실제와 같게 검증됩니다.

지켜야 할 것:
  1. `/청소 10` → **정확히 최근 10개**
  2. 고정 메시지는 지우지 않되, **건너뛴 만큼 더 올라가 10개를 채운다**
  3. 봇 권한이 없으면 아무것도 지우지 않고 무엇이 없는지 알린다
  4. 응답은 본인에게만 · 로그 채널에 기록
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakes as F
from fakes import note, GUILD, Inter

import cogs.moderation as M
import utils.mod_db as mdb

NOW = datetime.datetime.now(datetime.timezone.utc)


class Msg:
    def __init__(self, age_minutes, pinned=False, kind=discord.MessageType.default):
        when = NOW - datetime.timedelta(minutes=age_minutes)
        self.id = discord.utils.time_snowflake(when) + age_minutes % 4096
        self.pinned, self.type, self.deleted = pinned, kind, False

    async def delete(self, **kw):
        self.deleted = True


class Chan:
    """history · delete_messages 만 흉내 내고 purge 는 진짜 라이브러리로."""
    def __init__(self, msgs, perms=None):
        self.msgs = msgs                    # 최신 순
        self.id, self.mention = 4242, "<#4242>"
        self.bulk_calls, self._perms = [], perms or discord.Permissions.all()
        self.events = []

    def permissions_for(self, m):
        return self._perms

    def history(self, limit=100, **kw):
        async def gen():
            for m in self.msgs[:limit]:
                yield m
        return gen()

    async def delete_messages(self, msgs, reason=None):
        self.bulk_calls.append(len(msgs))
        self.reason = reason
        for m in msgs:
            m.deleted = True

    async def purge(self, **kw):
        self.events.append("purge")
        return await discord.abc._purge_helper(self, **kw)


def text(e):
    return "\n".join([e.title or "", e.description or ""])


async def run(ch, n, admin=True):
    it = Inter()
    it.channel = ch
    order = []
    real_defer = it.response.defer

    async def defer(*a, **k):
        order.append("defer")
        return await real_defer(*a, **k)
    it.response.defer = defer
    ch.events = order
    await M.Moderation.purge.callback(M.Moderation(None), it, n)
    return it, order


async def main():
    GUILD.me.guild_permissions = F.Perms()
    log = GUILD.get_channel(333)
    await mdb.save_mod_settings(GUILD.id, {"log_channel_id": log.id})

    print("=== 1. /청소 10 → 정확히 최근 10개 ===")
    ch = Chan([Msg(i) for i in range(30)])
    it, order = await run(ch, 10)
    gone = [i for i, m in enumerate(ch.msgs) if m.deleted]
    note(gone == list(range(10)), "가장 최근 10개만 지움", str(gone))
    note(ch.bulk_calls == [10], "한 번에 묶어서 지움 (14일 안쪽)", str(ch.bulk_calls))
    sent = it.followup.sent[-1]
    note(sent.get("ephemeral") is True and "10개" in text(sent["embed"]),
         "결과는 본인에게만", text(sent["embed"])[:40])
    note(order[:2] == ["defer", "purge"], "지우기 전에 먼저 응답해 둠 (3초 제한)", str(order))
    note("관리자" in (ch.reason or "") and "/청소 10개" in ch.reason, "감사 로그에 처리자",
         ch.reason)
    note(log.sent and "청소" in log.sent[-1]["embed"].title
         and "10개" in str(log.sent[-1]["embed"].to_dict()), "로그 채널에 기록")

    print("\n=== 2. 고정 메시지는 남기고, 그만큼 더 올라가 개수를 채움 ===")
    msgs = [Msg(i, pinned=i in (1, 3)) for i in range(30)]
    ch = Chan(msgs)
    it, _ = await run(ch, 10)
    deleted = [m for m in msgs if m.deleted]
    note(len(deleted) == 10, "10개를 채움", str(len(deleted)))
    note(not msgs[1].deleted and not msgs[3].deleted, "고정 메시지는 그대로")
    note(msgs[11].deleted and not msgs[12].deleted, "건너뛴 2개만큼 더 올라감 (11번째까지)")
    note("고정 메시지 2개" in text(it.followup.sent[-1]["embed"]), "몇 개 남겼는지 알림")

    print("\n=== 3. 메시지가 모자라면 있는 만큼 ===")
    ch = Chan([Msg(i) for i in range(4)])
    it, _ = await run(ch, 10)
    e = it.followup.sent[-1]["embed"]
    note(all(m.deleted for m in ch.msgs) and "4개" in text(e) and "더 없었습니다" in text(e),
         "4개 지우고 더 없었다고 알림")

    print("\n=== 4. 14일 넘은 메시지가 섞여도 개수대로 (진짜 라이브러리 경로) ===")
    old = 15 * 24 * 60
    msgs = [Msg(i) for i in range(3)] + [Msg(old + i) for i in range(5)]
    ch = Chan(msgs)
    it, _ = await run(ch, 6)
    note([m.deleted for m in msgs] == [True] * 6 + [False] * 2, "최근 6개 (새 3 + 옛 3)",
         str([m.deleted for m in msgs]))
    note(ch.bulk_calls == [3], "새 것은 묶어서, 옛 것은 하나씩", str(ch.bulk_calls))

    print("\n=== 5. 지울 수 없는 시스템 메시지는 세지 않음 ===")
    msgs = [Msg(0), Msg(1, kind=discord.MessageType.recipient_add), Msg(2), Msg(3)]
    ch = Chan(msgs)
    await run(ch, 3)
    note([m.deleted for m in msgs] == [True, False, True, True], "시스템 메시지를 건너뛰고 3개",
         str([m.deleted for m in msgs]))

    print("\n=== 6. 봇 권한이 없으면 아무것도 안 지움 ===")
    ch = Chan([Msg(i) for i in range(5)], perms=discord.Permissions(read_message_history=True))
    it, order = await run(ch, 3)
    note(not any(m.deleted for m in ch.msgs) and "purge" not in order, "손대지 않음")
    note("메시지 관리" in text(it.followup.sent[-1]["embed"]), "무엇이 없는지 알림")

    class Boom(Chan):
        async def purge(self, **kw):
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": ""})(), "no")
    it, _ = await run(Boom([Msg(0)]), 3)
    note("청소 실패" in text(it.followup.sent[-1]["embed"]), "디스코드가 거부해도 명령어가 죽지 않음")

    it = Inter()
    it.channel = object()
    await M.Moderation.purge.callback(M.Moderation(None), it, 3)
    note("쓸 수 없" in text(it.followup.sent[-1]["embed"]) or
         "지울 수 없습니다" in text(it.followup.sent[-1]["embed"]),
         "청소가 안 되는 채널 종류면 안내")

    print("\n=== 7. 명령어 등록 (권한 · 범위) ===")
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    await bot.load_extension("cogs.moderation")
    cmd = next(c for c in bot.tree.get_commands() if c.name == "청소")
    d = cmd.to_dict(bot.tree)
    opt = d["options"][0]
    note(opt["name"] == "개수" and opt.get("min_value") == 1 and opt.get("max_value") == 100,
         "개수는 1~100 (디스코드가 입력 단계에서 막음)", str(opt))
    note(int(d.get("default_member_permissions") or 0)
         == discord.Permissions(manage_messages=True).value,
         "'메시지 관리' 권한이 없는 사람에게는 명령어가 안 보임")
    note(d.get("contexts") == [0] or d.get("dm_permission") is False, "DM 에서는 안 보임")
    await bot.close()

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
sys.exit(1 if F.FAILS else 0)
