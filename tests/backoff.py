"""429 를 만나면 백오프가 실제로 걸리고 유지되는가.

예전에는 update_goal_message 가 아무것도 돌려주지 않아, 429 로 실패해도
호출부가 reset() 을 불러 방금 건 백오프를 지웠습니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakemongo as FM
import fakes as F
from fakes import GUILD, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


def make_429():
    r = type("R", (), {"status": 429, "reason": "Too Many Requests"})()
    return discord.HTTPException(r, {"message": "rate limited", "code": 0})


class Boom:
    """호출하면 무조건 429 를 내는 채널."""
    def __init__(self, real):
        self._r = real
        self.id, self.name = real.id, real.name
        self.mention = real.mention
        self.guild = real.guild
        self.hits = 0

    def permissions_for(self, m):
        return self._r.permissions_for(m)

    async def send(self, *a, **k):
        self.hits += 1
        raise make_429()

    async def fetch_message(self, mid):
        self.hits += 1
        raise make_429()

    def get_partial_message(self, mid):
        boom = self

        class _P:
            async def edit(self, **k):
                boom.hits += 1
                raise make_429()
        return _P()


async def main():
    import utils.database as db
    import utils.health as H
    FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    await bot.load_extension("cogs.quest")
    cog = bot.get_cog("Quest")

    # 주기 작업 본문을 직접 부르려면 로그인 대기와 길드 조회를 대역으로 막아야 합니다
    async def _ready():
        return None
    bot.wait_until_ready = _ready
    type(bot).guilds = property(lambda self: [GUILD])
    bot.get_guild = lambda gid: GUILD

    boom = Boom(GUILD.get_channel(333))
    GUILD._chans[333] = boom

    await db.save_quest_settings(GUILD.id, {
        "enabled": True, "goal_enabled": True, "admin_only": False,
        "goal_channel_id": "333", "goal_target": 1000, "goal_reward": 200})
    db._quest_settings_cache.pop(str(GUILD.id))

    print("=== 새 메시지 경로에서 429 ===")
    H.goal_backoff.reset()
    ok = await cog.update_goal_message(GUILD)
    note(ok is False, "실패를 알려줌 (bool 반환)", repr(ok))
    note(H.goal_backoff.active(), "백오프가 걸림",
         f"{H.goal_backoff.remaining():.0f}초 남음")
    note(H.snapshot().get("rate_limited_at") is not None
         or H.rate_limited_recently(), "health 에 429 로 기록됨")

    print("\n=== 수정 경로에서 429 ===")
    H.goal_backoff.reset()
    await db.save_quest_settings(GUILD.id, {"goal_message_id": "999"})
    db._quest_settings_cache.pop(str(GUILD.id))
    ok = await cog.update_goal_message(GUILD)
    note(ok is False, "실패를 알려줌", repr(ok))
    note(H.goal_backoff.active(), "백오프가 걸림",
         f"{H.goal_backoff.remaining():.0f}초 남음")

    print("\n=== 핵심: 호출부가 백오프를 지우지 않는가 ===")
    H.goal_backoff.reset()
    cog._goal_dirty[GUILD.id] = True
    cog._goal_last_update[GUILD.id] = 0
    await cog._goal_realtime.coro(cog)
    note(H.goal_backoff.active(),
         "_goal_realtime 실행 후에도 백오프가 살아있음",
         f"{H.goal_backoff.remaining():.0f}초 남음  (0이면 예전 버그 재현)")

    H.goal_backoff.reset()
    await cog.goal_refresh.coro(cog)
    note(H.goal_backoff.active(),
         "goal_refresh 실행 후에도 백오프가 살아있음",
         f"{H.goal_backoff.remaining():.0f}초 남음")

    print("\n=== 백오프 중에는 요청을 아예 안 보내는가 ===")
    before = boom.hits
    cog._goal_dirty[GUILD.id] = True
    cog._goal_last_update[GUILD.id] = 0
    await cog._goal_realtime.coro(cog)
    await cog.goal_refresh.coro(cog)
    note(boom.hits == before, "요청 0건 (차단을 연장하지 않음)",
         f"{boom.hits - before}건 시도")

    print("\n=== 성공하면 백오프가 풀리는가 ===")
    GUILD._chans[333] = boom._r      # 정상 채널로 되돌림
    boom._r.sent.clear()
    H.goal_backoff.reset()
    ok = await cog.update_goal_message(GUILD)
    note(ok is True, "성공을 알려줌", repr(ok))
    cog._goal_dirty[GUILD.id] = True
    cog._goal_last_update[GUILD.id] = 0
    await cog._goal_realtime.coro(cog)
    note(not H.goal_backoff.active(), "정상일 때는 백오프가 안 걸림")

    print("\n=== 지수 증가 ===")
    H.goal_backoff.reset()
    ds = []
    for _ in range(4):
        H.goal_backoff.hit()
        ds.append(int(H.goal_backoff.delay))
    note(ds == sorted(ds) and ds[0] < ds[-1] and ds[-1] <= 1800,
         "5분 → 두 배씩 → 30분 상한", str(ds))

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
