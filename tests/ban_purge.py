"""밴한 유저의 **7일보다 오래된** 메시지까지 지우기 (메시지 검색 API).

요청(2026-10-01). 밴 API 는 최근 7일만 지우므로, 그보다 오래된 것은
GET /guilds/{id}/messages/search?author_id=… 로 찾아 하나씩 지웁니다.
실측: 운영 서버에서 봇 권한으로 검색이 됩니다(9/29 에 밴한 유저 626건).

지켜야 할 것 — 지운 메시지는 **되돌릴 수 없습니다**:
  1. 그 유저 것만 지운다 (다른 사람 것은 한 건도 안 건드림)
  2. 25개씩 넘겨 가며 **반드시 끝난다** (색인이 늦어 지운 게 또 나와도)
  3. **밴을 풀면 멈춘다** — 실수로 밴했다 푸는 경우
  4. 재시작해도 **이어서** 한다 (처음부터 다시 하지 않음)
  5. /밴 에 기간을 직접 적으면 그 선택을 따른다
"""
import sys, os, tempfile, asyncio, io, contextlib

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import note, drain, GUILD, Inter, Role, Member, Perms
import utils.mod_db as mdb
import cogs.moderation as M

MOD_ROLE = Role(6001, "운영진", 50)
LOW_ROLE = Role(6002, "일반", 10)
BASE = 200000000000000000
LOG = GUILD.get_channel(333)


def _r(status):
    return type("R", (), {"status": status, "reason": ""})()


class FakeHTTP:
    """검색 · 삭제만 흉내 냅니다. 실제처럼 25개씩, 202, 늦은 색인."""

    def __init__(self):
        self.msgs = {}            # id -> {"id","channel_id","author"}
        self.index_wait = 0       # 이 횟수만큼 202 '색인 준비 중'
        self.stale = set()        # 지웠는데 색인에 남아 있는 것 (늦은 색인)
        self.forbidden_ch = set()
        self.searches, self.deletes = [], []
        self.explode_after = None  # 이만큼 지운 뒤 예외 (재시작 흉내)

    def add(self, author, n, channel=10, start=None):
        base = start or (10 ** 15 + len(self.msgs) * 10)
        for i in range(n):
            mid = base + i
            self.msgs[mid] = {"id": str(mid), "channel_id": str(channel + i % 5),
                              "author": author}

    def left(self, author):
        return sum(1 for m in self.msgs.values() if m["author"] == author)

    async def request(self, route, params=None, **kw):
        assert "/messages/search" in route.url, route.url
        self.searches.append(dict(params or {}))
        if self.index_wait > 0:
            self.index_wait -= 1
            return {"message": "Index not yet available. Try again later",
                    "code": 110000, "documents_indexed": 0, "retry_after": 0}
        author = str(params["author_id"])
        before = int(params["max_id"]) if params.get("max_id") else None
        pool = [m for mid, m in self.msgs.items() if str(m["author"]) == author]
        pool += [{"id": str(s), "channel_id": "10", "author": author} for s in self.stale]
        pool = [m for m in pool if before is None or int(m["id"]) < before]
        pool.sort(key=lambda m: -int(m["id"]))
        page = pool[:int(params.get("limit", 25))]
        # 실제 응답처럼 한 겹 더 감쌉니다
        return {"total_results": len(pool), "doing_deep_historical_index": False,
                "messages": [[{"id": m["id"], "channel_id": m["channel_id"]}] for m in page]}

    async def delete_message(self, channel_id, message_id, reason=None):
        if self.explode_after is not None and len(self.deletes) >= self.explode_after:
            self.explode_after = None
            raise RuntimeError("재시작 흉내")
        self.deletes.append(int(message_id))
        if int(channel_id) in self.forbidden_ch:
            raise discord.Forbidden(_r(403), {"code": 50013, "message": "Missing Permissions"})
        if int(message_id) in self.stale:
            self.stale.discard(int(message_id))
            raise discord.NotFound(_r(404), {"code": 10008, "message": "Unknown Message"})
        if int(message_id) not in self.msgs:
            raise discord.NotFound(_r(404), {"code": 10008, "message": "Unknown Message"})
        self.msgs.pop(int(message_id))


def member(uid, name):
    m = Member(BASE + uid, name, False, roles=[LOW_ROLE], guild=GUILD)
    GUILD.members.append(m)
    return m


async def settle(timeout=15.0):
    """떠 있는 작업이 **실제로** 다 끝날 때까지 (최대 15초).

    예전에는 루프를 60번 돌리고 말았는데, 가짜 DB 가 스레드(`_db`)에서 돌아
    전체 검증처럼 바쁠 때는 작업이 끝나기 전에 결과를 봤습니다(가끔 실패).
    밴 해제처럼 따로 뜬 작업도 기다립니다.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    me = asyncio.current_task()
    while loop.time() < end:
        await asyncio.sleep(0.01)
        await drain()
        if all(t.done() for t in asyncio.all_tasks() if t is not me):
            return


async def main():
    GUILD.me.guild_permissions = Perms()
    mdb.mod_settings_col = FM.FakeCollection("mod_settings")     # Mongo 경로
    mdb.mod_cases_col = FM.FakeCollection("mod_cases")
    mdb.mod_users_col = FM.FakeCollection("mod_users")
    mdb._AFTER = FM.AFTER          # 안 맞추면 케이스 번호가 JSON 폴백으로 샙니다
    http = FakeHTTP()
    GUILD._state = type("S", (), {"http": http})()
    M.PURGE_UNKNOWN_WAIT = 0
    await mdb.save_mod_settings(GUILD.id, {"log_channel_id": LOG.id})
    moder = Member(7, "운영자", False, roles=[MOD_ROLE], guild=GUILD)

    print("=== 1. 검색 한 장 ===")
    http.index_wait = 2
    http.add("u1", 3)
    rows, total = await M.search_user_messages(http, GUILD.id, "u1")
    note(len(rows) == 3 and total == 3, "색인 준비 중(202) 두 번 뒤 결과를 받음", f"{len(rows)}/{total}")
    p = http.searches[-1]
    note(p["author_id"] == "u1" and p["include_nsfw"] == "true" and p["sort_order"] == "desc",
         "그 유저 · 연령 제한 채널 포함 · 최신순으로 검색")
    http.index_wait = 99
    try:
        await M.search_user_messages(http, GUILD.id, "u1")
        note(False, "색인이 끝내 안 되면 멈춤")
    except RuntimeError:
        note(True, "색인이 끝내 안 되면 무한히 기다리지 않고 멈춤")
    http.index_wait = 0
    http.msgs.clear()

    print("\n=== 2. /밴 → 7일은 밴이, 그보다 오래된 것은 검색으로 ===")
    t = member(1, "도배꾼")
    other = member(2, "무고한사람")
    http.add(str(t.id), 137, channel=10)          # 25개씩 6장이 넘게
    http.add(str(other.id), 40, channel=10)
    http.stale = {10 ** 14 + 1, 10 ** 14 + 2}      # 이미 지웠는데 색인에 남은 것
    it = F.Inter(uid=7, roles=[MOD_ROLE])
    await M.Moderation.ban.callback(M.Moderation(None), it, t, "도배")
    emb = it.followup.sent[-1]["embed"]
    text = (emb.description or "") + " ".join(f.value for f in emb.fields)
    note("오래된 메시지" in text and "밴을 풀면 멈춥니다" in text,
         "밴 결과에 '오래된 메시지도 지우는 중 · 풀면 멈춤'")
    note(GUILD.ban_delete.get(t.id) == 7 * 86400, "최근 7일은 밴과 함께 (디스코드가)")
    await settle()
    note(http.left(str(t.id)) == 0, "**그 유저 메시지 137건 전부 삭제**", f"남음 {http.left(str(t.id))}")
    note(http.left(str(other.id)) == 40, "**다른 사람 메시지는 한 건도 안 건드림**",
         f"{http.left(str(other.id))}/40")
    note(not await mdb.get_purge_jobs(GUILD.id), "끝나면 작업 기록을 지움")
    logs = [s["embed"] for s in LOG.sent if s.get("embed") is not None
            and "정리 완료" in (s["embed"].title or "")]
    note(logs and "137" in logs[-1].description and "이미 없던 것 2" in logs[-1].fields[0].value,
         "로그 채널에 '정리 완료 137건 · 이미 없던 것 2건'",
         logs[-1].description if logs else "없음")
    maxes = [int(s["max_id"]) for s in http.searches if s.get("max_id")]
    note(maxes == sorted(maxes, reverse=True) and len(set(maxes)) == len(maxes),
         "검색 위치가 매번 더 오래된 쪽으로만 감 (반드시 끝남)")

    print("\n=== 3. /밴 에 기간을 직접 적으면 그 선택을 따름 ===")
    t3 = member(3, "기간지정")
    http.add(str(t3.id), 10)
    it = F.Inter(uid=7, roles=[MOD_ROLE])
    await M.Moderation.ban.callback(M.Moderation(None), it, t3, "x", 0)
    await settle()
    note(http.left(str(t3.id)) == 10 and not M._purge_tasks,
         "메시지삭제:0 → 오래된 것도 안 지움")

    print("\n=== 4. 실수로 밴했다 풀면 바로 멈춤 ===")
    t4 = member(4, "억울한사람")
    http.add(str(t4.id), 100)
    await mdb.save_mod_settings(GUILD.id, {"ban_delete_all": True})
    orig_delete = http.delete_message
    unbanned = {"done": False}

    async def slow_delete(channel_id, message_id, reason=None):
        # 30건쯤 지웠을 때 운영진이 **따로** 밴을 풉니다 (실제로는 다른 상호작용)
        if len(http.deletes) == 30 + base_n and not unbanned["done"]:
            unbanned["done"] = True
            it = F.Inter(uid=7, roles=[MOD_ROLE])
            asyncio.get_running_loop().create_task(M.do_unban(it, t4.id, "실수"))
        await asyncio.sleep(0)
        return await orig_delete(channel_id, message_id, reason)

    base_n = len(http.deletes)
    http.delete_message = slow_delete
    await M.do_ban(GUILD, moder, t4, "x")
    await settle()
    http.delete_message = orig_delete
    left = http.left(str(t4.id))
    note(unbanned["done"] and 50 <= left <= 70,
         "**밴을 풀면 멈춤** (그 장까지만 지우고 나머지는 남음)", f"남음 {left}/100")
    note(not await mdb.get_purge_jobs(GUILD.id), "푼 뒤 작업 기록이 남지 않음 (재시작해도 다시 안 함)")

    print("\n  봇 밖에서(디스코드 우클릭 · Dyno) 풀어도 멈춰야 합니다")
    # 이때는 stop_purge 가 불리지 않습니다. 멈추게 하는 것은 **장마다 하는
    # 밴 상태 확인**뿐이라, 이 경우를 따로 봅니다(없던 동안 변이가 안 잡혔습니다).
    t4b = member(44, "우클릭해제")
    http.add(str(t4b.id), 100)
    outside = {"done": False}

    async def outside_unban(channel_id, message_id, reason=None):
        if len(http.deletes) == 30 + base_n2 and not outside["done"]:
            outside["done"] = True
            GUILD.bans.pop(t4b.id, None)          # 봇을 거치지 않은 해제
        await asyncio.sleep(0)
        return await orig_delete(channel_id, message_id, reason)

    base_n2 = len(http.deletes)
    http.delete_message = outside_unban
    await M.do_ban(GUILD, moder, t4b, "x")
    await settle()
    http.delete_message = orig_delete
    left = http.left(str(t4b.id))
    note(outside["done"] and 50 <= left <= 70,
         "**봇 밖에서 풀어도** 다음 장에서 멈춤", f"남음 {left}/100")
    stopped = [s["embed"] for s in LOG.sent if s.get("embed") is not None
               and "멈췄습니다" in (s["embed"].title or "")]
    note(bool(stopped), "로그 채널에 '밴이 풀려 멈췄습니다'")

    # 경쟁: 밴 해제가 기록을 지운 **직후** 작업의 진행 쓰기가 도착하는 경우
    await mdb.set_purge_job(GUILD.id, 4242, {"before": "5"})
    await mdb.set_purge_job(GUILD.id, 4242, None)                       # 해제
    await mdb.set_purge_job(GUILD.id, 4242, {"before": "3"}, only_if_exists=True)  # 늦은 진행 쓰기
    note("4242" not in await mdb.get_purge_jobs(GUILD.id),
         "**해제 뒤 늦게 도착한 진행 쓰기가 작업을 되살리지 않음**")
    await mdb.set_purge_job(GUILD.id, 4243, {"before": "5"})
    await mdb.set_purge_job(GUILD.id, 4243, {"before": "3"}, only_if_exists=True)
    note((await mdb.get_purge_jobs(GUILD.id)).get("4243", {}).get("before") == "3",
         "작업이 살아 있으면 진행 위치는 정상적으로 갱신")
    await mdb.set_purge_job(GUILD.id, 4243, None)

    print("\n=== 5. 도중에 재시작해도 이어서 ===")
    t5 = member(5, "재시작중")
    http.add(str(t5.id), 80)
    http.explode_after = len(http.deletes) + 40      # 40건 지운 뒤 죽음
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        await M.do_ban(GUILD, moder, t5, "x")
        await settle()
    jobs = await mdb.get_purge_jobs(GUILD.id)
    job = jobs.get(str(t5.id))
    note(job is not None and job.get("before") and 25 <= int(job.get("deleted", 0)) <= 40,
         "죽어도 작업과 진행 위치가 DB 에 남음", str(job and job.get("deleted")))
    left_before = http.left(str(t5.id))
    deletes_before = len(http.deletes)
    cog = M.Moderation(type("B", (), {"guilds": [GUILD]})())
    await cog.on_ready()                             # 재시작 = on_ready
    await settle()
    note(http.left(str(t5.id)) == 0, "**재시작하면 남은 것을 이어서 지움**",
         f"남음 {http.left(str(t5.id))} (재시작 전 {left_before})")
    redo = len(http.deletes) - deletes_before
    note(redo <= left_before + 25, "처음부터 다시 하지 않음 (많아야 한 장 겹침)",
         f"재시작 뒤 삭제 시도 {redo}건")
    note(not await mdb.get_purge_jobs(GUILD.id), "끝나면 기록 정리")

    print("\n=== 6. 못 지우는 것 · 중복 시작 · 밴 상태 확인 실패 ===")
    t6 = member(6, "권한없는채널")
    http.add(str(t6.id), 10, channel=90)
    http.forbidden_ch = {90, 91, 92, 93, 94}
    with contextlib.redirect_stdout(io.StringIO()):
        await M.do_ban(GUILD, moder, t6, "x")
        await settle()
    logs = [s["embed"] for s in LOG.sent if s.get("embed") is not None]
    note(logs and "못 지운 것 10건" in " ".join(f.value for f in logs[-1].fields),
         "권한 없는 채널은 '못 지운 것' 으로 세고 **끝까지 감** (멈추지 않음)")
    http.forbidden_ch = set()

    t7 = member(7777, "중복")
    GUILD.bans[t7.id] = "x"
    http.add(str(t7.id), 30)
    first = await M.start_purge(GUILD, t7.id)
    second = await M.start_purge(GUILD, t7.id)
    note(first and not second, "같은 사람을 두 번 시작하지 않음")
    await settle()

    t8 = member(8888, "확인불가")
    GUILD.bans[t8.id] = "x"
    http.add(str(t8.id), 5)
    real_fetch = GUILD.fetch_ban

    async def broken(user):
        raise discord.HTTPException(_r(500), "server error")

    GUILD.fetch_ban = broken
    with contextlib.redirect_stdout(io.StringIO()):
        await M.start_purge(GUILD, t8.id)
        await settle()
    GUILD.fetch_ban = real_fetch
    note(http.left(str(t8.id)) == 5 and str(t8.id) in await mdb.get_purge_jobs(GUILD.id),
         "밴 상태를 확인 못 하면 **지우지 않고** 멈춤 · 다음 재시작 때 이어서")
    await mdb.set_purge_job(GUILD.id, t8.id, None)

    print("\n=== 7. 설정에서 끄면 ===")
    await mdb.save_mod_settings(GUILD.id, {"ban_delete_all": False})
    t9 = member(9, "끈상태")
    http.add(str(t9.id), 10)
    await M.do_ban(GUILD, moder, t9, "x")
    await settle()
    note(http.left(str(t9.id)) == 10, "'전부' 가 꺼져 있으면 오래된 것은 안 지움")
    await mdb.save_mod_settings(GUILD.id, {"ban_delete_all": True})

    leaks = [c for c in ("mod_settings_col",) if getattr(mdb, c) is None]
    note(not leaks, "작업 기록은 Mongo 경로(가짜 드라이버)로")


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
