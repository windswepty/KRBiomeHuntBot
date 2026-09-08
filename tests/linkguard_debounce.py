"""링크 보호 — 로그 임베드 '사용 인원' 갱신 (몰림 처리).

곧 6,500명 서버에서 켤 기능인데 이 경로가 커버리지에서 비어 있었습니다.
개인서버 자리 경쟁은 수십 명이 **같은 순간에** 버튼을 누릅니다.

CLAUDE.md 에 기록된 과거 사고가 여기입니다:
  "동시 완료 순서는 값의 크기 순이 아닙니다. 디바운스에 '마지막에 도착한
   값'을 쓰면 20명이 눌렀는데 13명으로 남습니다."
"""
import sys, os, tempfile, asyncio, time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import discord
import fakes as F
from fakes import note
import cogs.link_guard as LG


class LogChannel(F.Channel):
    """로그 임베드를 들고 있는 채널 대역."""

    def __init__(self):
        super().__init__(777, "로그", F.GUILD)
        self.edits = []
        self.fetches = 0
        self.raise_notfound = False

    async def fetch_message(self, mid):
        self.fetches += 1
        if self.raise_notfound:
            raise discord.NotFound(
                type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")
        m = F.Msg(mid, self)
        e = discord.Embed(title="링크 보호")
        e.add_field(name="🔗 링크", value="x", inline=False)
        e.add_field(name="👥 사용 인원", value="**0명**이 이 링크를 사용했습니다.",
                    inline=False)
        m.embeds = [e]

        async def _edit(**kw):
            self.edits.append(kw)
            return m
        m.edit = _edit
        return m


class Guild(F.Guild):
    def __init__(self, log):
        super().__init__()
        self._log = log

    def get_channel(self, cid):
        if int(cid) == 777:
            return self._log
        return super().get_channel(cid)


def reset():
    LG._pending_total.clear()
    LG._last_log_refresh.clear()
    for t in list(LG._trailing_tasks.values()):
        t.cancel()
    LG._trailing_tasks.clear()
    LG._LOG_MESSAGE_BY_PROTECTED.clear()


def used_count(edits):
    """마지막 수정에서 '사용 인원' 숫자를 읽어옵니다."""
    if not edits:
        return None
    e = edits[-1]["embed"].to_dict()
    for f in e.get("fields", []):
        if f["name"].startswith("👥"):
            return int(f["value"].split("**")[1].replace("명", ""))
    return None


async def main():
    LG.LOG_REFRESH_INTERVAL = 0.15      # 시험을 빠르게
    MID = 12345
    SET = {"log_channel_id": 777}

    print("=== 첫 클릭은 즉시 반영 ===")
    reset()
    log = LogChannel(); g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999      # 로그 메시지 위치를 안다고 가정
    await LG.refresh_usage_log(g, SET, MID, 1, "u")
    note(used_count(log.edits) == 1, "1명 반영", str(used_count(log.edits)))

    print("")
    print("=== 창이 열려 있는 동안은 API 를 두드리지 않아야 ===")
    before = len(log.edits)
    for n in range(2, 21):
        await LG.refresh_usage_log(g, SET, MID, n, "u")
    note(len(log.edits) == before, "디바운스 중 즉시 수정 없음",
         f"수정 {len(log.edits) - before}회")
    note(MID in LG._trailing_tasks, "창이 닫히면 반영하도록 예약됨")

    print("")
    print("=== 창이 닫히면 마지막 값으로 한 번 더 (트레일링) ===")
    await asyncio.sleep(0.35)
    note(used_count(log.edits) == 20, "20명 반영", str(used_count(log.edits)))
    note(MID not in LG._trailing_tasks, "예약 태스크 정리됨")

    print("")
    print("=== 완료 순서가 뒤섞여도 최댓값이 남아야 합니다 ===")
    # 20명이 눌렀는데 13명으로 남던 사고가 이 지점이었습니다.
    reset()
    log = LogChannel(); g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG.refresh_usage_log(g, SET, MID, 1, "u")      # 창 열기
    for n in (7, 20, 13, 11, 4):                         # 뒤죽박죽 도착
        await LG.refresh_usage_log(g, SET, MID, n, "u")
    await asyncio.sleep(0.35)
    note(used_count(log.edits) == 20,
         "마지막에 온 4가 아니라 최댓값 20 이 남음", str(used_count(log.edits)))

    print("")
    print("=== 동시에 80명이 눌러도 예약은 1개만 ===")
    reset()
    log = LogChannel(); g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG.refresh_usage_log(g, SET, MID, 1, "u")
    await asyncio.gather(*[LG.refresh_usage_log(g, SET, MID, n, "u")
                           for n in range(2, 82)])
    note(len(LG._trailing_tasks) == 1, "트레일링 태스크 1개",
         str(len(LG._trailing_tasks)))
    await asyncio.sleep(0.35)
    note(used_count(log.edits) == 81, "81명 반영", str(used_count(log.edits)))
    note(len(log.edits) <= 3, "API 수정 횟수가 몇 번으로 억제됨",
         f"{len(log.edits)}회 (80명 클릭)")

    print("")
    print("=== 로그 메시지가 지워졌으면 다시 조회하지 않아야 ===")
    # 매 클릭마다 재조회하면 지워진 메시지 하나로 DB 조회가 폭증합니다.
    reset()
    log = LogChannel(); log.raise_notfound = True; g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG._apply_usage_refresh(g, SET, MID, 5, "u")
    note(LG._LOG_MESSAGE_BY_PROTECTED.get(MID) == 0,
         "삭제됨을 0 으로 확정 캐시", str(LG._LOG_MESSAGE_BY_PROTECTED.get(MID)))
    f0 = log.fetches
    await LG._apply_usage_refresh(g, SET, MID, 6, "u")
    note(log.fetches == f0, "그 뒤로는 조회하지 않음", f"추가 조회 {log.fetches - f0}회")

    print("")
    print("=== 설정이 빠져 있으면 조용히 넘어가야 ===")
    reset()
    log = LogChannel(); g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG._apply_usage_refresh(g, {}, MID, 5, "u")
    note(len(log.edits) == 0, "로그 채널 미설정 → 아무것도 안 함")

    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG._apply_usage_refresh(g, {"log_channel_id": 999999}, MID, 5, "u")
    note(len(log.edits) == 0, "채널이 사라졌으면 아무것도 안 함")

    print("")
    print("=== 수정은 '사용 인원' 칸만 건드려야 ===")
    reset()
    log = LogChannel(); g = Guild(log)
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    await LG._apply_usage_refresh(g, SET, MID, 9, "u")
    d = log.edits[-1]["embed"].to_dict()
    link_field = next(f for f in d["fields"] if f["name"].startswith("🔗"))
    note(link_field["value"] == "x", "다른 칸은 그대로")
    note(d.get("title") == "링크 보호", "제목 유지")
    note(used_count(log.edits) == 9, "사용 인원만 바뀜")

    print("")
    print("=== 갱신 중 예외가 나도 클릭 처리가 죽으면 안 됩니다 ===")
    reset()
    log = LogChannel(); g = Guild(log)

    async def boom(mid):
        raise RuntimeError("디스코드 장애")
    log.fetch_message = boom
    LG._LOG_MESSAGE_BY_PROTECTED[MID] = 999
    try:
        await LG._apply_usage_refresh(g, SET, MID, 5, "u")
        note(True, "예외를 삼키고 계속 진행")
    except Exception as e:
        note(False, "예외가 샜습니다", f"{type(e).__name__}: {e}")

    reset()

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
